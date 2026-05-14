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
from datetime import datetime, timedelta, timezone
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
def ensure_campaigns(cur, rng: random.Random) -> list[tuple[str, str, float]]:
    """Возвращает [(campaign_id, mcc, cashback_rate), ...]"""
    cur.execute("SELECT count(*) FROM cashback_campaigns WHERE status = 'ACTIVE'")
    existing = cur.fetchone()[0]
    if existing >= len(DEMO_CAMPAIGNS):
        cur.execute("""
            SELECT c.campaign_id::text, cc.mcc_code, c.cashback_rate
              FROM cashback_campaigns c
              JOIN campaign_categories cc USING (campaign_id)
             WHERE c.status = 'ACTIVE'
        """)
        return [(cid, mcc.strip(), float(rate)) for cid, mcc, rate in cur.fetchall()]

    log(f"  + создаю {len(DEMO_CAMPAIGNS)} демо-кампаний")
    now = datetime.now(timezone.utc)
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
    return campaigns


# ---------------------------------------------------------------------------
# 2. Пользователи + согласия (в Postgres)
# ---------------------------------------------------------------------------
def ensure_users(cur, count: int, rng: random.Random) -> list[tuple[str, int]]:
    """Возвращает [(user_id, segment_id), ...]"""
    cur.execute("SELECT count(*) FROM users")
    existing = cur.fetchone()[0]
    if existing >= count:
        log(f"  - в users уже {existing} строк, скип")
        cur.execute("SELECT user_id::text, COALESCE(segment_id, 5) "
                    "FROM users ORDER BY created_at LIMIT %s", (count,))
        return [(uid, int(seg)) for uid, seg in cur.fetchall()]

    log(f"  + добавляю {count - existing} пользователей")
    users: list[tuple[str, int]] = []
    rows_users: list[tuple] = []
    rows_consents: list[tuple] = []
    for i in range(count):
        user_id = str(uuid.uuid4())
        ext = f"demo_{i:08d}"
        seg = rng.randint(1, 10)
        rows_users.append((user_id, ext, seg))
        # Согласие на персонализацию (нужно для applicable filter)
        rows_consents.append(
            (str(uuid.uuid4()), user_id, "personalised_cashback", "GRANTED", "v1.0",
             datetime.now(timezone.utc) - timedelta(days=rng.randint(30, 365)))
        )
        users.append((user_id, seg))

    cur.executemany(
        "INSERT INTO users (user_id, external_id, segment_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (external_id) DO NOTHING",
        rows_users,
    )
    cur.executemany(
        "INSERT INTO user_consents (consent_id, user_id, consent_type, status, "
        "document_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        rows_consents,
    )
    return users


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
) -> int:
    """Заливает синтетические транзакции в ClickHouse через bulk VALUES INSERT."""
    n_total = len(users) * tx_per_user
    log(f"  + готовлю {n_total} строк транзакций для transactions_raw...")

    base_date = datetime.now(timezone.utc) - timedelta(days=90)
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

    tx_idx = 0
    for user_id, _seg in users:
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
    return written


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
    # TRUNCATE через DROP + recreate невозможен, но мы можем удалить
    # все партиции (ReplacingMergeTree dedups при OPTIMIZE).
    ch_exec(ch_user=ch_user, password=ch_pass, db=ch_db, url=ch_url,
            sql="ALTER TABLE user_rfm_features DELETE WHERE 1=1")
    # Дождёмся применения mutation.
    time.sleep(2)

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
        pipe.setex(f"features:{user_id}", ttl, json.dumps(row, default=str))
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
) -> list[dict[str, Any]]:
    log(f"  + ~{len(users) * recs_per_user} recommendations")
    rows: list[tuple] = []
    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for user_id, seg in users:
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
            rows.append((rec_id, user_id, cid, mcc, score, generated_at,
                         status, expires_at))
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
                model_score, generated_at, response_status, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            rows[i:i + BATCH],
        )
    return out


# ---------------------------------------------------------------------------
# 7. Cashback accruals + update budget_spent
# ---------------------------------------------------------------------------
def seed_accruals(
    cur, recs: list[dict[str, Any]], rng: random.Random,
) -> int:
    """Для ACCEPTED-рекомендаций ~60% «закрылись транзакцией» → начисление."""
    log("  + cashback_accruals (для части ACCEPTED — отметка о начислении)")
    rows: list[tuple] = []
    budget_delta: dict[str, Decimal] = {}
    now = datetime.now(timezone.utc)

    for rec in recs:
        if rec["status"] != "ACCEPTED":
            continue
        if rng.random() > 0.6:
            continue   # 40% accepted-offer'ов не дошли до транзакции

        rate = Decimal(str(rec["rate"]))
        tx_amount = Decimal(str(round(rng.uniform(500, 12000), 2)))
        cashback = (tx_amount * rate / Decimal("100")).quantize(Decimal("0.01"))
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
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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

    # ----- Postgres -----
    log(f"connect Postgres @ {args.pg_dsn.split('@')[-1]}")
    pg = psycopg2.connect(args.pg_dsn)
    pg.autocommit = False
    cur = pg.cursor()

    log("[1/7] кампании")
    campaigns = ensure_campaigns(cur, rng)
    pg.commit()

    log("[2/7] пользователи + согласия")
    users = ensure_users(cur, args.users, rng)
    pg.commit()

    # ----- ClickHouse -----
    log(f"[3/7] транзакции в ClickHouse ({args.ch_url})")
    if args.skip_transactions:
        log("  - --skip-transactions, не трогаю transactions_raw")
    else:
        seed_transactions(args.ch_url, args.ch_user, args.ch_pass, args.ch_db,
                          users, args.tx_per_user, rng)

    log("[4/7] пересчёт user_rfm_features (INSERT INTO ... SELECT RFM_QUERY)")
    rows = compute_rfm(args.ch_url, args.ch_user, args.ch_pass, args.ch_db)
    log(f"  - {rows} строк в user_rfm_features")

    # ----- Redis -----
    log(f"[5/7] прогрев Redis ({args.redis_url})")
    rdb = redis.Redis.from_url(args.redis_url, decode_responses=True)
    warmed = warm_redis_features(args.ch_url, args.ch_user, args.ch_pass, args.ch_db, rdb)
    log(f"  - {warmed} ключей features:*")

    # ----- Recommendations + Accruals -----
    log("[6/7] recommendations")
    recs = seed_recommendations(cur, users, campaigns, args.recs_per_user, rng)
    pg.commit()

    log("[7/7] cashback_accruals + budget_spent")
    n_accruals = seed_accruals(cur, recs, rng)
    pg.commit()
    log(f"  - {n_accruals} записей в cashback_accruals")

    pg.close()

    elapsed = time.monotonic() - started
    print()
    log(f"\033[1;32mГотово за {elapsed:.1f}s\033[0m")
    print(f"""
Что заполнено:
  Postgres:
    • users               — {len(users)} строк (+ user_consents)
    • cashback_campaigns  — {len(campaigns)} ACTIVE кампаний
    • recommendations     — ~{len(recs)} штук
    • cashback_accruals   — {n_accruals} начислений
  ClickHouse:
    • transactions_raw    — ~{len(users) * args.tx_per_user} транзакций за 90 дней
    • user_rfm_features   — {rows} строк (108 признаков на юзера)
  Redis:
    • features:* — {warmed} ключей (TTL 1ч)

Откройте Adminer:
  http://localhost:8090   (PostgreSQL: cashback/cashback, db=cashback)
                          (ClickHouse: server clickhouse:8123, cashback/cashback)
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
