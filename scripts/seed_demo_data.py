#!/usr/bin/env python3
"""scripts/seed_demo_data.py

Однократный демо-сидер: наполняет Postgres + ClickHouse + Redis
реалистичными данными, как будто продукт уже работает.

Зачем: для презентации / Adminer / скриншотов UI — чтобы все таблицы
показывали осмысленные числа без необходимости поднимать Kafka,
гонять симулятор транзакций и ждать Airflow DAG.

Не зависит ни от одного из микросервисов — пишет прямо в три
хранилища, используя те же SQL-определения, что и боевой код:
* RFM_QUERY (Listing 3.6, services/etl/app/feature_eng.py)
* Топ-50 MCC + их веса (services/tx_simulator/app/simulator.py)
* Прогрессивная шкала rate_tiers (db_migrations/001)

Повторный запуск ничего не дублирует: кампании и пользователи
переиспользуются, транзакции и рекомендации досеваются только тем
пользователям, у которых их ещё нет. Упавший шаг не останавливает
независимые шаги, попадает в итоговую сводку и даёт exit-code 1.

Запуск с хоста:

    pip install psycopg2-binary redis
    python3 scripts/seed_demo_data.py --users 1000 --tx-per-user 80
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Настройки по умолчанию (для локального docker-compose стека)
# ---------------------------------------------------------------------------
DEFAULT_PG_DSN    = os.getenv("DEMO_PG_DSN",
                              "postgresql://cashback:cashback@localhost:5432/cashback")
DEFAULT_CH_URL    = os.getenv("DEMO_CH_URL",    "http://localhost:8123")
DEFAULT_CH_USER   = os.getenv("DEMO_CH_USER",   "cashback")
DEFAULT_CH_PASS   = os.getenv("DEMO_CH_PASS",   "cashback")
DEFAULT_CH_DB     = os.getenv("DEMO_CH_DB",     "cashback")
DEFAULT_REDIS_URL = os.getenv("DEMO_REDIS_URL", "redis://localhost:6379/0")


# ---------------------------------------------------------------------------
# Топ-50 MCC (синхронизировано с services/etl/app/feature_eng.py и
# infrastructure/clickhouse/migrations/002_create_user_rfm_features.sql)
# ---------------------------------------------------------------------------
TOP50_MCC: list[str] = [
    "5411", "5812", "5814", "5541", "5499", "5912", "5311", "5651",
    "5732", "5942", "5921", "5462", "4111", "4121", "4131", "4814",
    "4829", "4900", "7832", "7011", "7299", "7211", "7230", "7538",
    "7995", "8011", "8021", "8062", "8099", "8211", "8398", "8999",
    "5993", "5995", "5970", "5945", "5947", "5946", "5641", "5611",
    "5621", "5712", "5722", "5933", "5200", "5599", "5331", "5300",
    "4511", "5691",
]

# Веса как в TX simulator (Listing 1) — топ-4 явно прописаны, остальное равномерно.
MCC_WEIGHT_OVERRIDES = {"5411": 0.25, "5812": 0.15, "5541": 0.10, "5912": 0.08}
_REST_WEIGHT = (1.0 - sum(MCC_WEIGHT_OVERRIDES.values())) / \
               (len(TOP50_MCC) - len(MCC_WEIGHT_OVERRIDES))
MCC_WEIGHTS = [MCC_WEIGHT_OVERRIDES.get(m, _REST_WEIGHT) for m in TOP50_MCC]

# Лог-нормальные параметры (μ, σ) для суммы по основным MCC.
MCC_AMOUNT_PARAMS = {
    "5411": (6.68, 0.60),   # exp(6.68)≈800₽   средний чек продукты
    "5812": (7.31, 0.70),   # рестораны
    "5814": (6.21, 0.60),   # фастфуд
    "5541": (7.82, 0.40),   # АЗС — крупный чек
    "5912": (5.99, 0.60),   # аптеки
    "5732": (8.99, 1.00),   # электроника — большой разброс
    "4111": (4.09, 0.60),   # транспорт — мелкий чек
    "4121": (5.99, 0.70),   # такси
    "7011": (8.52, 0.90),   # отели — крупный чек
    "5942": (6.68, 0.70),   # книги
}
_DEFAULT_AMOUNT_PARAMS = (6.91, 0.70)   # ≈1000₽


# Демо-кампании покрывают 7 популярных MCC.
DEMO_CAMPAIGNS: list[dict[str, Any]] = [
    {"mcc": "5411", "name": "Кэшбэк на продукты",       "rate": 5.0, "budget": 1_500_000,
     "min_tx": 300,
     "tiers": [{"min_amount": 0, "max_amount": 5000, "rate": 5.0},
               {"min_amount": 5000, "max_amount": 20000, "rate": 7.0},
               {"min_amount": 20000, "max_amount": None, "rate": 10.0}]},
    {"mcc": "5812", "name": "Рестораны выходного дня",  "rate": 7.0, "budget":   800_000,
     "min_tx": 500, "tiers": None},
    {"mcc": "5541", "name": "АЗС на лето",              "rate": 3.0, "budget": 1_200_000,
     "min_tx": 1000, "tiers": None},
    {"mcc": "5912", "name": "Аптеки и здоровье",        "rate": 6.0, "budget":   500_000,
     "min_tx": 200, "tiers": None},
    {"mcc": "5732", "name": "Электроника — Black Friday","rate": 4.0,"budget": 2_000_000,
     "min_tx": 3000,
     "tiers": [{"min_amount": 0, "max_amount": 10000, "rate": 4.0},
               {"min_amount": 10000, "max_amount": 50000, "rate": 6.0},
               {"min_amount": 50000, "max_amount": None, "rate": 8.0}]},
    {"mcc": "4111", "name": "Транспорт каждый день",    "rate": 2.5, "budget":   300_000,
     "min_tx": 30, "tiers": None},
    {"mcc": "7011", "name": "Отели — отпуск",           "rate": 8.0, "budget":   900_000,
     "min_tx": 2000, "tiers": None},
]

# Сегменты с базовым accept_rate (5%–15% как в seed_recommendations.py).
ACCEPT_RATE_BY_SEGMENT = {
    1: 0.05, 2: 0.06, 3: 0.07, 4: 0.08, 5: 0.10,
    6: 0.10, 7: 0.11, 8: 0.13, 9: 0.14, 10: 0.15,
}

CHANNELS = ["ONLINE", "POS", "ATM", "MOBILE"]


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\033[1;36m[{ts}]\033[0m {msg}", flush=True)


def err(msg: str) -> None:
    print(f"\033[1;31m!!\033[0m {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# ClickHouse HTTP helper
# ---------------------------------------------------------------------------
def ch_exec(url: str, user: str, password: str, db: str, sql: str,
            data: bytes | None = None) -> str:
    """Run a query against the ClickHouse HTTP endpoint."""
    full_url = f"{url}/?database={db}"
    req = urllib.request.Request(
        full_url,
        data=(data if data is not None else sql.encode("utf-8")),
        method="POST",
        headers={
            "X-ClickHouse-User":     user,
            "X-ClickHouse-Key":      password,
            "Content-Type":          "text/plain",
        },
    )
    if data is not None:
        req.add_header("X-ClickHouse-Query", sql)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err(f"ClickHouse error: {e.read().decode('utf-8', errors='replace')}")
        raise


# ---------------------------------------------------------------------------
# 1. Кампании (в Postgres)
# ---------------------------------------------------------------------------
def ensure_campaigns(
    cur, rng: random.Random,
) -> tuple[list[tuple[str, str, float]], int]:
    """Возвращает ([(campaign_id, mcc, cashback_rate), ...], создано кампаний)."""
    cur.execute("SELECT count(*) FROM cashback_campaigns WHERE status = 'ACTIVE'")
    existing = cur.fetchone()[0]
    if existing >= len(DEMO_CAMPAIGNS):
        cur.execute("""
            SELECT c.campaign_id::text, cc.mcc_code, c.cashback_rate
              FROM cashback_campaigns c
              JOIN campaign_categories cc USING (campaign_id)
             WHERE c.status = 'ACTIVE'
        """)
        return [(cid, mcc.strip(), float(rate)) for cid, mcc, rate in cur.fetchall()], 0

    log(f"  + создаю {len(DEMO_CAMPAIGNS)} демо-кампаний")
    now = datetime.now(UTC)
    campaigns: list[tuple[str, str, float]] = []
    for camp in DEMO_CAMPAIGNS:
        campaign_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO cashback_campaigns (
                campaign_id, name, target_segment_ids, cashback_rate,
                min_transaction_amount, budget_total, budget_spent,
                status, start_date, end_date, allowed_channels,
                require_existing_behavior, rate_tiers
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 0, 'ACTIVE', %s, %s, %s, false, %s
            )
            """,
            (
                campaign_id, camp["name"],
                rng.sample(range(1, 11), 6),
                camp["rate"], camp["min_tx"], camp["budget"],
                now - timedelta(days=30), now + timedelta(days=60),
                ["ONLINE", "POS", "MOBILE"],
                json.dumps(camp["tiers"]) if camp["tiers"] else None,
            ),
        )
        cur.execute(
            "INSERT INTO campaign_categories (campaign_id, mcc_code, min_transaction_amount) "
            "VALUES (%s, %s, %s)",
            (campaign_id, camp["mcc"], camp["min_tx"]),
        )
        campaigns.append((campaign_id, camp["mcc"], camp["rate"]))
    return campaigns, len(DEMO_CAMPAIGNS)


# ---------------------------------------------------------------------------
# 2. Пользователи + согласия (в Postgres)
# ---------------------------------------------------------------------------
def ensure_users(
    cur, count: int, rng: random.Random,
) -> tuple[list[tuple[str, int]], int]:
    """Возвращает ([(user_id, segment_id), ...], добавлено пользователей)."""
    from psycopg2.extras import execute_values

    cur.execute("SELECT count(*) FROM users")
    existing = cur.fetchone()[0]
    if existing >= count:
        log(f"  - в users уже {existing} строк, скип")
        cur.execute("SELECT user_id::text, COALESCE(segment_id, 5) "
                    "FROM users ORDER BY created_at, external_id LIMIT %s", (count,))
        return [(uid, int(seg)) for uid, seg in cur.fetchall()], 0

    rows_users = [
        (str(uuid.uuid4()), f"demo_{i:08d}", rng.randint(1, 10))
        for i in range(count)
    ]
    # demo_* от прошлого запуска (с меньшим --users) уже есть, ON CONFLICT
    # их пропускает. Поэтому согласия ставим только реально вставленным
    # (RETURNING), а список пользователей перечитываем из БД: сгенерированные
    # выше user_id для пропущенных строк в users не существуют (FK).
    inserted = execute_values(
        cur,
        "INSERT INTO users (user_id, external_id, segment_id) VALUES %s "
        "ON CONFLICT (external_id) DO NOTHING RETURNING user_id::text",
        rows_users, page_size=1000, fetch=True,
    )
    new_ids = [uid for (uid,) in inserted]
    log(f"  + добавлено {len(new_ids)} пользователей")

    # Согласие на персонализацию (нужно для applicable filter)
    rows_consents = [
        (str(uuid.uuid4()), user_id, "personalised_cashback", "GRANTED", "v1.0",
         datetime.now(UTC) - timedelta(days=rng.randint(30, 365)))
        for user_id in new_ids
    ]
    cur.executemany(
        "INSERT INTO user_consents (consent_id, user_id, consent_type, status, "
        "document_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        rows_consents,
    )

    cur.execute("SELECT user_id::text, COALESCE(segment_id, 5) FROM users "
                "WHERE external_id = ANY(%s) ORDER BY external_id",
                ([ext for _uid, ext, _seg in rows_users],))
    return [(uid, int(seg)) for uid, seg in cur.fetchall()], len(new_ids)


# ---------------------------------------------------------------------------
# 3. Транзакции (в ClickHouse) — большой объём, грузим VALUES-блоками
# ---------------------------------------------------------------------------
def _sample_mcc(rng: random.Random) -> str:
    return rng.choices(TOP50_MCC, weights=MCC_WEIGHTS, k=1)[0]


def _sample_amount(mcc: str, rng: random.Random) -> Decimal:
    import math
    mu, sigma = MCC_AMOUNT_PARAMS.get(mcc, _DEFAULT_AMOUNT_PARAMS)
    # log-normal sampling без numpy
    z = rng.gauss(0, 1)
    val = math.exp(mu + sigma * z)
    val = max(val, 1.0)
    return Decimal(f"{val:.2f}")


def seed_transactions(
    ch_url: str, ch_user: str, ch_pass: str, ch_db: str,
    users: list[tuple[str, int]], tx_per_user: int, rng: random.Random,
) -> tuple[int, int]:
    """Заливает синтетические транзакции в ClickHouse через bulk VALUES INSERT.

    MergeTree не дедуплицирует строки, поэтому повторный запуск пропускает
    пользователей, у которых демо-транзакции уже есть, и продолжает
    нумерацию transaction_id. Возвращает (залито строк, пропущено юзеров).
    """
    existing = ch_exec(
        ch_url, ch_user, ch_pass, ch_db,
        "SELECT toString(user_id), count() FROM transactions_raw "
        "WHERE startsWith(transaction_id, 'tx-demo-') "
        "GROUP BY user_id FORMAT TSV",
    )
    seeded: set[str] = set()
    tx_idx = 0
    for line in existing.splitlines():
        uid, n = line.split("\t")
        seeded.add(uid)
        tx_idx += int(n)
    todo = [(uid, seg) for uid, seg in users if uid not in seeded]
    skipped = len(users) - len(todo)
    if skipped:
        log(f"  - у {skipped} пользователей демо-транзакции уже есть, пропускаю их")

    n_total = len(todo) * tx_per_user
    log(f"  + готовлю {n_total} строк транзакций для transactions_raw...")

    base_date = datetime.now(UTC) - timedelta(days=90)
    span_seconds = 90 * 86400

    chunk_size = 20_000
    chunk: list[str] = []
    written = 0

    def flush(rows: list[str]) -> None:
        nonlocal written
        if not rows:
            return
        sql = (
            "INSERT INTO transactions_raw "
            "(transaction_id, user_id, transaction_date, amount, currency, "
            "mcc_code, merchant_id, merchant_name, channel, city, country, "
            "inserted_at) VALUES "
            + ",".join(rows)
        )
        ch_exec(ch_url, ch_user, ch_pass, ch_db, sql)
        written += len(rows)
        log(f"    ... залил {written}/{n_total}")

    for user_id, _seg in todo:
        offsets = sorted(rng.uniform(0, span_seconds) for _ in range(tx_per_user))
        for off in offsets:
            ts = (base_date + timedelta(seconds=off)).strftime("%Y-%m-%d %H:%M:%S")
            mcc = _sample_mcc(rng)
            amount = _sample_amount(mcc, rng)
            channel = rng.choice(CHANNELS)
            chunk.append(
                f"('tx-demo-{tx_idx:09d}','{user_id}','{ts}',{amount},'RUB',"
                f"'{mcc}','m-{tx_idx % 100}','M{tx_idx % 100}',"
                f"'{channel}','Moscow','RU',now())"
            )
            tx_idx += 1
            if len(chunk) >= chunk_size:
                flush(chunk)
                chunk = []
    flush(chunk)
    return written, skipped


# ---------------------------------------------------------------------------
# 4. user_rfm_features = INSERT INTO ... SELECT RFM_QUERY (на стороне CH)
# ---------------------------------------------------------------------------
def _build_rfm_query() -> str:
    """Та же RFM_QUERY что в services/etl/app/feature_eng.py."""
    freq_cols = ",\n    ".join(
        f"countIf(mcc_code = '{m}') AS freq_{m}" for m in TOP50_MCC
    )
    amt_cols = ",\n    ".join(
        f"sumIf(amount, mcc_code = '{m}') AS amt_{m}" for m in TOP50_MCC
    )
    return f"""
SELECT
    user_id,
    now()                                                               AS computed_at,
    toUInt32(dateDiff('day', max(transaction_date), now()))              AS recency_days,
    toUInt32(count())                                                    AS frequency_total,
    sum(amount)                                                          AS monetary_total,
    avg(amount)                                                          AS avg_ticket,
    toFloat32(avg(toDayOfWeek(transaction_date) >= 6))                   AS weekend_ratio,
    toFloat32(avg(toHour(transaction_date) BETWEEN 18 AND 21))           AS evening_ratio,
    toUInt16(uniq(mcc_code))                                             AS distinct_mcc_count,
    {freq_cols},
    {amt_cols},
    toUInt16(dateDiff('month', min(transaction_date), now()))            AS tenure_months
FROM transactions_raw
WHERE transaction_date >= now() - INTERVAL 90 DAY
GROUP BY user_id
""".strip()


def compute_rfm(ch_url: str, ch_user: str, ch_pass: str, ch_db: str) -> int:
    """Удаляет старые данные и пересчитывает user_rfm_features."""
    log("  - очищаю старые признаки в user_rfm_features")
    # TRUNCATE синхронный, в отличие от мутации ALTER ... DELETE: старые
    # строки не доживут до INSERT и не попадут в count() ниже.
    ch_exec(ch_url, ch_user, ch_pass, ch_db, "TRUNCATE TABLE user_rfm_features")

    log("  + INSERT INTO user_rfm_features SELECT RFM_QUERY ...")
    rfm_query = _build_rfm_query()
    sql = f"INSERT INTO user_rfm_features {rfm_query}"
    ch_exec(ch_url, ch_user, ch_pass, ch_db, sql)

    res = ch_exec(ch_url, ch_user, ch_pass, ch_db,
                  "SELECT count() FROM user_rfm_features")
    return int(res.strip())


# ---------------------------------------------------------------------------
# 5. Прогрев Redis features:{user_id}
# ---------------------------------------------------------------------------
def warm_redis_features(
    ch_url: str, ch_user: str, ch_pass: str, ch_db: str, rdb,
    ttl: int = 3600,
) -> int:
    """SELECT * FROM user_rfm_features -> Redis SETEX в pipeline."""
    log(f"  + прогреваю Redis features:* (TTL {ttl}s)")
    sql = "SELECT * FROM user_rfm_features FORMAT JSONEachRow"
    payload = ch_exec(ch_url, ch_user, ch_pass, ch_db, sql)
    pipe = rdb.pipeline()
    count = 0
    for line in payload.strip().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        user_id = row.get("user_id")
        if not user_id:
            continue
        # Сериализуем компактно (без user_id и computed_at).
        pipe.set(f"features:{user_id}", json.dumps(row, default=str), ex=ttl)
        count += 1
        if count % 1000 == 0:
            pipe.execute()
            pipe = rdb.pipeline()
    if count % 1000 != 0:
        pipe.execute()
    return count


# ---------------------------------------------------------------------------
# 6. Recommendations в Postgres (с биномиальным accept_rate по сегменту)
# ---------------------------------------------------------------------------
def seed_recommendations(
    cur, users: list[tuple[str, int]],
    campaigns: list[tuple[str, str, float]],
    recs_per_user: int, rng: random.Random,
) -> tuple[list[dict[str, Any]], int]:
    """Возвращает (вставленные рекомендации, пропущено пользователей).

    Пользователи, у которых рекомендации уже есть (прошлый запуск сидера
    или живой recommendation-api), пропускаются — повторный запуск
    не удваивает воронку и начисления.
    """
    cur.execute("SELECT DISTINCT user_id::text FROM recommendations "
                "WHERE user_id = ANY(%s::uuid[])", ([uid for uid, _ in users],))
    have_recs = {uid for (uid,) in cur.fetchall()}
    todo = [(uid, seg) for uid, seg in users if uid not in have_recs]
    if have_recs:
        log(f"  - у {len(have_recs)} пользователей рекомендации уже есть, пропускаю их")

    log(f"  + ~{len(todo) * recs_per_user} recommendations")
    rows: list[tuple] = []
    out: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for user_id, seg in todo:
        n = rng.randint(max(1, recs_per_user - 2), recs_per_user + 2)
        for _ in range(n):
            cid, mcc, rate = rng.choice(campaigns)
            generated_at = now - timedelta(
                days=rng.randint(0, 30),
                hours=rng.randint(0, 23),
                minutes=rng.randint(0, 59),
            )
            expires_at = generated_at + timedelta(days=7)
            accept_rate = ACCEPT_RATE_BY_SEGMENT.get(seg, 0.08)
            r = rng.random()
            if r < accept_rate:
                status = "ACCEPTED"
            elif r < accept_rate + 0.45:
                status = "DECLINED"
            elif r < accept_rate + 0.65:
                status = "EXPIRED"
            elif r < accept_rate + 0.80:
                status = "SNOOZE"
            else:
                status = "PENDING"
            rec_id = str(uuid.uuid4())
            score = round(rng.uniform(0.10, 0.95), 4)
            # Фаза 16 (миграция 003): момент реакции + канал доставки.
            # Отреагировавшие получают responded_at в пределах 48 часов
            # после генерации; каналы — взвешенный микс пайплайна уведомлений.
            if status in ("ACCEPTED", "DECLINED", "SNOOZE"):
                responded_at = generated_at + timedelta(
                    minutes=rng.randint(1, 48 * 60))
            else:
                responded_at = None
            channel = rng.choices(
                ["PUSH", "SMS", "EMAIL", "IN_APP"],
                weights=[0.40, 0.20, 0.15, 0.25],
            )[0]
            rows.append((rec_id, user_id, cid, mcc, score, generated_at,
                         status, expires_at, responded_at, channel))
            out.append({
                "rec_id": rec_id, "user_id": user_id,
                "campaign_id": cid, "mcc": mcc, "rate": rate,
                "status": status, "generated_at": generated_at,
                "expires_at": expires_at,
            })

    # Bulk insert through executemany with page-batching.
    BATCH = 2000
    for i in range(0, len(rows), BATCH):
        cur.executemany(
            """
            INSERT INTO recommendations (
                recommendation_id, user_id, campaign_id, mcc_code,
                model_score, generated_at, response_status, expires_at,
                responded_at, channel
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            rows[i:i + BATCH],
        )
    return out, len(have_recs)


# ---------------------------------------------------------------------------
# 7. Cashback accruals + update budget_spent
# ---------------------------------------------------------------------------
def seed_accruals(
    cur, recs: list[dict[str, Any]], rng: random.Random,
) -> tuple[int, Decimal, int]:
    """Для ACCEPTED-рекомендаций ~60% «закрылись транзакцией» → начисление.

    Как и движок начислений, не выходит за бюджет кампании: начисление,
    которое не помещается в остаток budget_total - budget_spent, не
    создаётся (иначе UPDATE ниже нарушает CHECK ck_campaign_budget и
    откатывает весь шаг). Возвращает (начислений, сумма, отказов по бюджету).
    """
    log("  + cashback_accruals (для части ACCEPTED — отметка о начислении)")
    rows: list[tuple] = []
    budget_delta: dict[str, Decimal] = {}
    now = datetime.now(UTC)

    campaign_ids = sorted({rec["campaign_id"] for rec in recs
                           if rec["status"] == "ACCEPTED"})
    remaining: dict[str, Decimal] = {}
    if campaign_ids:
        # FOR UPDATE: остаток не уменьшат параллельные начисления до COMMIT.
        cur.execute(
            "SELECT campaign_id::text, budget_total - budget_spent "
            "FROM cashback_campaigns WHERE campaign_id = ANY(%s::uuid[]) "
            "FOR UPDATE",
            (campaign_ids,),
        )
        remaining = dict(cur.fetchall())
    over_budget = 0

    for rec in recs:
        if rec["status"] != "ACCEPTED":
            continue
        if rng.random() > 0.6:
            continue   # 40% accepted-offer'ов не дошли до транзакции

        rate = Decimal(str(rec["rate"]))
        tx_amount = Decimal(str(round(rng.uniform(500, 12000), 2)))
        cashback = (tx_amount * rate / Decimal("100")).quantize(Decimal("0.01"))
        left = remaining.get(rec["campaign_id"], Decimal("0"))
        if cashback > left:
            over_budget += 1
            continue   # бюджет кампании исчерпан — кэшбэк не начисляется
        remaining[rec["campaign_id"]] = left - cashback
        status = "PAID" if rng.random() < 0.7 else "PENDING"
        accrued_at = rec["generated_at"] + timedelta(
            days=rng.randint(1, 5), hours=rng.randint(0, 23),
        )
        if accrued_at > now:
            accrued_at = now - timedelta(hours=1)
        rows.append((
            str(uuid.uuid4()), rec["user_id"], rec["campaign_id"],
            f"tx-acc-{uuid.uuid4().hex[:12]}", rec["mcc"], tx_amount,
            cashback, status, accrued_at,
        ))
        budget_delta[rec["campaign_id"]] = (
            budget_delta.get(rec["campaign_id"], Decimal("0")) + cashback
        )

    BATCH = 2000
    for i in range(0, len(rows), BATCH):
        cur.executemany(
            """
            INSERT INTO cashback_accruals (
                accrual_id, user_id, campaign_id, transaction_id,
                mcc_code, transaction_amount, cashback_amount, status, accrued_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transaction_id, campaign_id) DO NOTHING
            """,
            rows[i:i + BATCH],
        )

    for cid, delta in budget_delta.items():
        cur.execute(
            "UPDATE cashback_campaigns SET budget_spent = budget_spent + %s "
            "WHERE campaign_id = %s",
            (delta, cid),
        )
    return len(rows), sum(budget_delta.values(), Decimal("0")), over_budget


# ---------------------------------------------------------------------------
# 2b. Пользователи админ-панели (фаза 15 — auth/RBAC)
# ---------------------------------------------------------------------------
# bcrypt-хэши предвычислены (пароль = часть email до @-роли, см. README):
#   admin@bank.ru / admin — создаётся миграцией 002
#   m.sokolova@bank.ru / marketer, d.ivanov@bank.ru / analyst — ниже
ADMIN_USERS = [
    ("m.sokolova@bank.ru",
     "$2b$12$yygSc5CQMnJW2odxHX/31edrZY5etN6U4afFW6/NtH.JgbcEwSmNS",
     "Мария Соколова", "MARKETER"),
    ("d.ivanov@bank.ru",
     "$2b$12$7urn2rB62CxjIkZhTLw2ueE0wjoyhMn0cwC.ZCkMsS1nmIy7ad3XG",
     "Дмитрий Иванов", "ANALYST"),
]


def seed_admin_users(cur) -> int:
    """Демо-логины маркетолога и аналитика (админ приходит из миграции).

    Возвращает число реально добавленных логинов (уже существующие — 0).
    """
    cur.executemany(
        "INSERT INTO admin_users (email, password_hash, full_name, role) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
        ADMIN_USERS,
    )
    return cur.rowcount



# ---------------------------------------------------------------------------
# 7b. Демо A/B-эксперименты (фаза 16 — страница «Эксперименты»)
# ---------------------------------------------------------------------------
def seed_ab_experiments(cur, users, rng: random.Random) -> tuple[int, int, int]:
    """Два эксперимента: ACTIVE с ~2000 назначений и заметным uplift
    (p-value < 0.05 на странице) и DRAFT без данных.

    Возвращает (создано экспериментов, назначений, событий); уже
    существующие эксперименты (по имени) не трогаются."""
    now = datetime.now(UTC)

    def insert_experiment(name, metric, status, started_days_ago, variants):
        exp_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO ab_experiments (experiment_id, name, status, "
            "target_metric, start_date, end_date) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING "
            "RETURNING experiment_id",
            (exp_id, name, status, metric,
             now - timedelta(days=started_days_ago), None),
        )
        if cur.fetchone() is None:      # эксперимент уже существует
            return None, []
        var_ids = []
        for vname, weight, strategy, conv_rate in variants:
            vid = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO ab_variants (variant_id, experiment_id, name, "
                "traffic_weight, strategy_class, strategy_params) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (vid, exp_id, vname, weight, strategy, None),
            )
            var_ids.append((vid, conv_rate))
        return exp_id, var_ids

    exp_id, var_ids = insert_experiment(
        "LightGBM vs SVD-baseline (ranking)", "acceptance_rate", "ACTIVE", 21,
        [("control", 0.5, "SVDRanker", 0.082),
         ("treatment", 0.5, "LightGBMRanker", 0.104)],
    )
    n_created = 0 if exp_id is None else 1
    n_assignments = n_events = 0
    if exp_id is not None:
        sample = rng.sample(users, min(2000, len(users)))
        assignments, events = [], []
        for i, (user_id, _seg) in enumerate(sample):
            vid, conv = var_ids[i % 2]
            assigned = now - timedelta(days=rng.randint(0, 20),
                                       hours=rng.randint(0, 23))
            # ab_events ссылаются на assignment_id (схема 001) —
            # генерируем id назначения сами.
            aid = str(uuid.uuid4())
            assignments.append((aid, user_id, exp_id, vid, assigned))
            events.append((str(uuid.uuid4()), aid, "IMPRESSION", assigned))
            if rng.random() < conv:
                events.append((str(uuid.uuid4()), aid, "CONVERSION",
                               assigned + timedelta(hours=rng.randint(1, 72))))
        cur.executemany(
            "INSERT INTO ab_assignments (assignment_id, user_id, "
            "experiment_id, variant_id, assigned_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            assignments,
        )
        cur.executemany(
            "INSERT INTO ab_events (event_id, assignment_id, event_type, "
            "event_at) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            events,
        )
        n_assignments = len(assignments)
        n_events = len(events)

    draft_id, _ = insert_experiment(
        "OST: вечерняя vs утренняя отправка push", "open_rate", "DRAFT", 2,
        [("morning", 0.5, "MorningSendStrategy", 0.0),
         ("evening", 0.5, "EveningSendStrategy", 0.0)],
    )
    if draft_id is not None:
        n_created += 1
    return n_created, n_assignments, n_events


# ---------------------------------------------------------------------------
# Учёт шагов: итоговая сводка и exit-code строятся по фактическим исходам
# ---------------------------------------------------------------------------
STEP_OK, STEP_SKIPPED, STEP_FAILED, STEP_NOT_RUN = "OK", "SKIP", "FAIL", "NOT RUN"


class Steps:
    """Запускает шаги сидера и запоминает исход каждого.

    Исключение шага не валит сидер: оно фиксируется как FAIL, независимые
    шаги выполняются дальше, а зависящие от него (needs) — NOT RUN.
    Пропуск по флагу (SKIP) зависимость не нарушает: данные шага уже
    лежат в хранилище.
    """

    def __init__(self) -> None:
        self.results: list[tuple[str, str, str, str]] = []  # key, title, status, detail
        self._status: dict[str, str] = {}

    def _record(self, key: str, title: str, status: str, detail: str) -> None:
        self.results.append((key, title, status, detail))
        self._status[key] = status

    def skip(self, key: str, title: str, reason: str) -> None:
        log(f"[{key}] {title}")
        log(f"  - пропущен: {reason}")
        self._record(key, title, STEP_SKIPPED, reason)

    def run(self, key: str, title: str, fn, *, needs: tuple[str, ...] = (),
            on_error=None) -> Any:
        """fn() -> (результат, строка для сводки); возвращает результат или None."""
        log(f"[{key}] {title}")
        blocked = [k for k in needs
                   if self._status.get(k) not in (STEP_OK, STEP_SKIPPED)]
        if blocked:
            reason = "не выполнен шаг " + ", ".join(blocked)
            err(f"  шаг {key} не запускался: {reason}")
            self._record(key, title, STEP_NOT_RUN, reason)
            return None
        try:
            result, detail = fn()
        except Exception as e:  # noqa: BLE001 — исход уходит в сводку и exit-code
            lines = str(e).strip().splitlines()
            reason = f"{type(e).__name__}: {lines[0] if lines else ''}".rstrip(": ")
            err(f"  шаг {key} упал: {reason}")
            if on_error is not None:
                try:
                    on_error()
                except Exception as rollback_exc:  # noqa: BLE001
                    err(f"  откат после ошибки тоже упал: {rollback_exc}")
            self._record(key, title, STEP_FAILED, reason)
            return None
        log(f"  - {detail}")
        self._record(key, title, STEP_OK, detail)
        return result

    @property
    def failed(self) -> list[str]:
        return [key for key, _t, status, _d in self.results if status == STEP_FAILED]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=1000,
                        help="how many users to seed (default 1000)")
    parser.add_argument("--tx-per-user", type=int, default=80,
                        help="transactions per user (default 80)")
    parser.add_argument("--recs-per-user", type=int, default=4,
                        help="avg recommendations per user (default 4)")
    parser.add_argument("--pg-dsn",    default=DEFAULT_PG_DSN)
    parser.add_argument("--ch-url",    default=DEFAULT_CH_URL)
    parser.add_argument("--ch-user",   default=DEFAULT_CH_USER)
    parser.add_argument("--ch-pass",   default=DEFAULT_CH_PASS)
    parser.add_argument("--ch-db",     default=DEFAULT_CH_DB)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--seed",      type=int, default=42)
    parser.add_argument("--skip-transactions", action="store_true",
                        help="skip the heavy ClickHouse INSERT")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    try:
        import psycopg2
        import redis
    except ImportError as e:
        err(f"missing dependency: {e}. Установите: pip install psycopg2-binary redis")
        return 1

    started = time.monotonic()
    steps = Steps()
    ch = (args.ch_url, args.ch_user, args.ch_pass, args.ch_db)

    # ----- Postgres -----
    def connect_pg():
        conn = psycopg2.connect(args.pg_dsn)
        conn.autocommit = False
        return conn, "подключено"

    pg = steps.run("PG", f"подключение к Postgres @ {args.pg_dsn.split('@')[-1]}",
                   connect_pg)
    cur = pg.cursor() if pg is not None else None

    def pg_rollback() -> None:
        pg.rollback()

    def do_campaigns():
        campaigns, created = ensure_campaigns(cur, rng)
        pg.commit()
        n = len({cid for cid, _mcc, _rate in campaigns})
        return campaigns, f"{n} ACTIVE-кампаний (создано {created})"

    campaigns = steps.run("1/7", "кампании", do_campaigns,
                          needs=("PG",), on_error=pg_rollback)

    def do_users():
        users, added = ensure_users(cur, args.users, rng)
        pg.commit()
        return users, (f"{len(users)} пользователей "
                       f"(добавлено {added}, согласий +{added})")

    users = steps.run("2/7", "пользователи + согласия", do_users,
                      needs=("PG",), on_error=pg_rollback)

    def do_admin_users():
        added = seed_admin_users(cur)
        pg.commit()
        return added, f"+{added} демо-логинов админ-панели (см. README)"

    steps.run("2b/7", "логины админ-панели", do_admin_users,
              needs=("PG",), on_error=pg_rollback)

    # ----- ClickHouse -----
    tx_title = f"транзакции в ClickHouse ({args.ch_url})"
    if args.skip_transactions:
        steps.skip("3/7", tx_title, "--skip-transactions, transactions_raw не трогаю")
    else:
        def do_transactions():
            written, skipped = seed_transactions(*ch, users, args.tx_per_user, rng)
            return written, (f"+{written} строк в transactions_raw "
                             f"(у {skipped} пользователей уже были)")

        steps.run("3/7", tx_title, do_transactions, needs=("2/7",))

    def do_rfm():
        rows = compute_rfm(*ch)
        return rows, f"{rows} строк в user_rfm_features (108 признаков на юзера)"

    steps.run("4/7", "пересчёт user_rfm_features (INSERT INTO ... SELECT RFM_QUERY)",
              do_rfm, needs=("3/7",))

    # ----- Redis -----
    def do_redis():
        rdb = redis.Redis.from_url(args.redis_url, decode_responses=True)
        warmed = warm_redis_features(*ch, rdb)
        return warmed, f"{warmed} ключей features:* (TTL 1ч)"

    steps.run("5/7", f"прогрев Redis ({args.redis_url})", do_redis, needs=("4/7",))

    # ----- Recommendations + Accruals -----
    def do_recommendations():
        recs, skipped = seed_recommendations(cur, users, campaigns,
                                             args.recs_per_user, rng)
        pg.commit()
        return recs, (f"+{len(recs)} рекомендаций "
                      f"(у {skipped} пользователей уже были)")

    recs = steps.run("6/7", "recommendations", do_recommendations,
                     needs=("1/7", "2/7"), on_error=pg_rollback)

    def do_ab():
        created, assignments, events = seed_ab_experiments(cur, users, rng)
        pg.commit()
        return events, (f"+{created} экспериментов, +{assignments} назначений, "
                        f"+{events} ab_events")

    steps.run("6b/7", "A/B-эксперименты (страница «Эксперименты»)", do_ab,
              needs=("2/7",), on_error=pg_rollback)

    def do_accruals():
        n_accruals, total, over_budget = seed_accruals(cur, recs, rng)
        pg.commit()
        return n_accruals, (f"+{n_accruals} начислений, budget_spent +{total} "
                            f"(не хватило бюджета кампании: {over_budget})")

    steps.run("7/7", "cashback_accruals + budget_spent", do_accruals,
              needs=("6/7",), on_error=pg_rollback)

    if pg is not None:
        pg.close()

    elapsed = time.monotonic() - started
    print()
    failed = steps.failed
    if failed:
        err(f"Завершено с ошибками за {elapsed:.1f}s: упали шаги {', '.join(failed)}")
    else:
        log(f"\033[1;32mГотово за {elapsed:.1f}s\033[0m")
    print("\nИтог по шагам (что сделал этот запуск):")
    for key, title, status, detail in steps.results:
        print(f"  [{status}]".ljust(12) + f"{key:<6} {title} — {detail}")
    print("""
Откройте Adminer:
  http://localhost:8090   (PostgreSQL: cashback/cashback, db=cashback)
                          (ClickHouse: server clickhouse:8123, cashback/cashback)
""")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
