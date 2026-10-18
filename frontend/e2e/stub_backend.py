#!/usr/bin/env python3
"""Stub FastAPI-shaped backends on :8001 (recommendation) and :8002 (campaign).

Mirrors the exact JSON wire format of the real services (Decimal → strings,
UUID ids, ISO datetimes) so the frontend live-mode wiring can be verified
without the docker stack.

Этап B (docs/STAGE_B_CONTRACT §2–§9) повторён по поведению, а не только по
формату: одноразовые refresh-токены с отзывом семейства при повторе,
logout, обязательная смена пароля (403 «password change required»),
троттлинг входа (429 + Retry-After), таблица прав маршрутов §4, SSE только
по sse-токену из POST /events/token, ML-объяснения через /ml/explain.
Токены непрозрачные (stub-access-…): фронт JWT не разбирает. Ручки
/__stub/* — управление стабом из E2E (протухание токенов, обрыв SSE, …).

Этап C (STAGE_C_CONTRACT §3): удалить можно только черновик без истории,
остальное — в архив (POST …/archive, include_archived в списках); activate
после end_date — 409, до start_date — starts_in_future; смена статуса
архивной кампании — 409; бюджет — только чтение (GET …/budget, без
budget/check); PUT /ml-limits при недоступном serving-пути — 503 с записью
в «БД», снапшот переиздаёт отдельная фоновая задача.
"""
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOW = datetime.now(UTC)


def iso(dt):
    return dt.isoformat()


def parse_dt(value):
    """ISO-строка кампании (стаб или фронт, «…Z») → aware datetime."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


CAMPAIGNS = [
    {
        "campaign_id": str(uuid.uuid4()),
        "name": "Супермаркеты 5% (live)",
        "target_segment_ids": [5, 6, 7, 9, 10],
        "cashback_rate": "5.00",
        "min_transaction_amount": "500.00",
        "budget_total": "1000000.00",
        "budget_spent": "234000.00",
        "status": "ACTIVE",
        "start_date": iso(NOW - timedelta(days=30)),
        "end_date": iso(NOW + timedelta(days=60)),
        "allowed_channels": ["ONLINE", "POS", "MOBILE"],
        "require_existing_behavior": False,
        "rate_tiers": None,
        "mcc_codes": ["5411"],
    },
    {
        "campaign_id": str(uuid.uuid4()),
        "name": "Аптеки 6% (live)",
        "target_segment_ids": [2, 9, 10],
        "cashback_rate": "6.00",
        "min_transaction_amount": "200.00",
        "budget_total": "500000.00",
        "budget_spent": "410000.00",
        "status": "PAUSED",
        "start_date": iso(NOW - timedelta(days=20)),
        "end_date": iso(NOW + timedelta(days=40)),
        "allowed_channels": ["ONLINE", "POS"],
        "require_existing_behavior": False,
        "rate_tiers": None,
        "mcc_codes": ["5912"],
    },
    {
        "campaign_id": str(uuid.uuid4()),
        "name": "АЗС черновик (live)",
        "target_segment_ids": [1, 3, 4],
        "cashback_rate": "3.00",
        "min_transaction_amount": "1000.00",
        "budget_total": "300000.00",
        "budget_spent": "0.00",
        "status": "DRAFT",
        "start_date": iso(NOW + timedelta(days=5)),
        "end_date": iso(NOW + timedelta(days=65)),
        "allowed_channels": ["POS"],
        "require_existing_behavior": False,
        "rate_tiers": None,
        "mcc_codes": ["5541"],
    },
    {
        # Этап C (B24): черновик с историей (рекомендации из импорта) —
        # удалить нельзя (409), в архив тоже (DRAFT).
        "campaign_id": str(uuid.uuid4()),
        "name": "Кафе черновик с историей (live)",
        "target_segment_ids": [5, 6],
        "cashback_rate": "4.00",
        "min_transaction_amount": "300.00",
        "budget_total": "150000.00",
        "budget_spent": "0.00",
        "status": "DRAFT",
        "start_date": iso(NOW + timedelta(days=3)),
        "end_date": iso(NOW + timedelta(days=33)),
        "allowed_channels": ["POS"],
        "require_existing_behavior": False,
        "rate_tiers": None,
        "mcc_codes": ["5814"],
    },
    {
        # Этап C (B24): архивная — скрыта из списков без include_archived.
        "campaign_id": str(uuid.uuid4()),
        "name": "Кинотеатры 8% (архив)",
        "target_segment_ids": [3, 4],
        "cashback_rate": "8.00",
        "min_transaction_amount": "300.00",
        "budget_total": "200000.00",
        "budget_spent": "154000.00",
        "status": "COMPLETED",
        "start_date": iso(NOW - timedelta(days=120)),
        "end_date": iso(NOW - timedelta(days=30)),
        "allowed_channels": ["ONLINE", "POS"],
        "require_existing_behavior": False,
        "rate_tiers": None,
        "mcc_codes": ["7832"],
        "archived_at": iso(NOW - timedelta(days=10)),
    },
]

# Поля визарда (фаза 22): дневной лимит, авто-пауза, RFM, автор,
# per-категорийные мин. суммы. Нормализуем сид-кампании к новой схеме.
for _c in CAMPAIGNS:
    _dur_days = 90
    _c.setdefault("daily_limit", str(round(float(_c["budget_total"]) / _dur_days, 2)))
    _c.setdefault("auto_pause", True)
    _c.setdefault("rfm_min", 1)
    _c.setdefault("rfm_max", 5)
    _c.setdefault("created_by", None)
    _c.setdefault("archived_at", None)
    _c.setdefault("min_tx_amounts", {
        code: _c["min_transaction_amount"]
        for code in _c["mcc_codes"]
        if _c["min_transaction_amount"] is not None
    })

STATS = {
    c["campaign_id"]: {
        "campaign_id": c["campaign_id"],
        "impressions": imp,
        "clicks": int(imp * 0.4),
        "accepted": int(imp * 0.12),
        "transactions": int(imp * 0.07),
        "revenue": f"{imp * 42:.2f}",
        "cashback_paid": f"{imp * 3.1:.2f}",
        "ctr": 0.4,
        "conversion_rate": 0.12,
        "roi": 2.4,
    }
    for c, imp in zip(CAMPAIGNS, [12000, 4200, 0, 150, 900], strict=True)
}


def has_history(campaign_id):
    """Есть рекомендации (impressions) или начисления (transactions) — как
    exists()-проверка DELETE в сервисе (B24)."""
    s = STATS.get(campaign_id) or {}
    return bool(s.get("impressions") or s.get("transactions"))


# Начисления «с полуночи UTC» (spent_today в GET …/budget); остальным — 0.
SPENT_TODAY = {CAMPAIGNS[0]["campaign_id"]: "1250.00"}


def campaign_budget(c):
    """Тело GET /campaigns/{id}/budget (CampaignBudget) — только чтение (B23)."""
    total, spent = Decimal(c["budget_total"]), Decimal(c["budget_spent"])
    daily = Decimal(c["daily_limit"]) if c.get("daily_limit") is not None else None
    today = Decimal(SPENT_TODAY.get(c["campaign_id"], "0.00"))
    return {
        "campaign_id": c["campaign_id"],
        "budget_total": str(total), "budget_spent": str(spent),
        "remaining": str(total - spent),
        "daily_limit": None if daily is None else str(daily),
        "spent_today": str(today),
        "remaining_today": None if daily is None else str(max(daily - today, Decimal("0"))),
    }


def status_action_response(c, previous, action):
    """Тело PATCH /campaigns/{id}/status (StatusActionResponse)."""
    return {
        "campaign_id": c["campaign_id"], "previous": previous,
        "current": c["status"], "action": action,
        # B25: активна, но платить начнёт только со start_date.
        "starts_in_future": (c["status"] == "ACTIVE"
                             and parse_dt(c["start_date"]) > datetime.now(UTC)),
    }


def include_archived(query):
    """include_archived=true|1|yes|on — как bool-параметр FastAPI."""
    return (query.get("include_archived") or ["false"])[0].lower() in ("true", "1", "yes", "on")

FUNNEL_STEPS = [
    {"name": "target_audience", "count": 16200, "drop_off_pct": 0.0},
    {"name": "received", "count": 14100, "drop_off_pct": 12.96},
    {"name": "opened", "count": 8300, "drop_off_pct": 41.13},
    {"name": "accepted", "count": 1944, "drop_off_pct": 76.58},
    {"name": "transacted", "count": 1134, "drop_off_pct": 41.67},
    {"name": "cashback_paid", "count": 1058, "drop_off_pct": 6.70},
]

MATRIX = [
    {"segment_id": seg, "mcc_code": mcc, "impressions": 800 + seg * 100,
     "accepted": int((800 + seg * 100) * rate), "ctr": rate}
    for seg in range(1, 11)
    for mcc, rate in [("5411", 0.55 + 0.02 * seg), ("5912", 0.30 + 0.03 * seg), ("5541", 0.20 + 0.01 * seg)]
]

TREND_POINTS = [
    {"date": iso(NOW - timedelta(days=d)).split("T")[0], "segment_bucket": b,
     "accepted": base + d * 3}
    for d in range(14, -1, -1)
    for b, base in [("premium", 40), ("mass", 120), ("young", 60),
                    ("senior", 25), ("business", 15)]
]

CHANNELS = [
    {"channel": "PUSH",   "sent": 4200, "opened": 1850, "converted": 620},
    {"channel": "SMS",    "sent": 2100, "opened": 700,  "converted": 260},
    {"channel": "EMAIL",  "sent": 1500, "opened": 430,  "converted": 120},
    {"channel": "IN_APP", "sent": 2600, "opened": 1700, "converted": 540},
]

# Сводные KPI (фаза 24) — reach переопределяется при сегмент-фильтре.
KPIS = {
    "campaigns_count": 2,
    "reach": 1245000,
    "spent": "644000.00",
    "budget": "1500000.00",
    "avg_ctr": 0.184,
    "trends": {"reach": 8.2, "spent": -3.1, "ctr": 5.8},
    "has_data": True,
}

EXPERIMENTS = [
    {
        "experiment_id": str(uuid.uuid4()),
        "name": "LightGBM vs SVD-baseline (ranking)",
        "status": "ACTIVE",
        "target_metric": "acceptance_rate",
        "start_date": iso(NOW - timedelta(days=21)),
        "end_date": None,
        "variants": [
            {"variant_id": str(uuid.uuid4()), "name": "control",
             "traffic_weight": 0.5, "strategy_class": "SVDRanker",
             "strategy_params": None},
            {"variant_id": str(uuid.uuid4()), "name": "treatment",
             "traffic_weight": 0.5, "strategy_class": "LightGBMRanker",
             "strategy_params": None},
        ],
    },
]

def experiment_results(exp):
    c, t = exp["variants"][0], exp["variants"][1]
    return {
        "experiment_id": exp["experiment_id"],
        "target_metric": exp["target_metric"],
        "control":   {"variant_id": c["variant_id"], "name": c["name"],
                      "n": 1000, "successes": 82, "rate": 0.082},
        "treatment": {"variant_id": t["variant_id"], "name": t["name"],
                      "n": 1000, "successes": 104, "rate": 0.104},
        "diff": 0.022, "z": 2.31, "p_value": 0.0209,
        "confidence_interval": [0.0033, 0.0407],
        "significance": "significant",
    }

RECOMMENDATION = {
    "user_id": "",
    "recommendations": [
        {
            "mcc_code": "5411",
            "score": 0.78,
            "campaign_id": CAMPAIGNS[0]["campaign_id"],
            "top_factors": {
                "mcc_5411_cnt_90d": 0.21,
                "monetary_total_90d": 0.14,
                "recency_days": 0.08,
                "evening_ratio": -0.03,
                "mcc_5912_sum_90d": 0.05,
            },
            "feature_values": {
                "mcc_5411_cnt_90d": 12.0,
                "monetary_total_90d": 42800.0,
                "recency_days": 3.0,
                "evening_ratio": 0.18,
                "mcc_5912_sum_90d": 6100.0,
            },
        },
        {"mcc_code": "5912", "score": 0.61, "campaign_id": None,
         "top_factors": {"mcc_5912_cnt_90d": 0.12, "frequency_total": 0.06}},
        {"mcc_code": "5541", "score": 0.44, "campaign_id": None,
         "top_factors": {"mcc_5541_cnt_90d": 0.09}},
    ],
    "model_version": "3",
    "candidates_considered": 24,
    "serving_group": "prod",
    # Расширенный контракт ML-объяснений (фаза 25).
    "base_value": 0.18,
    "confidence": 0.84,
    "expected_roi": 3.4,
    "rationale": "Ранжирующая модель оценила вероятность принятия в 78%: "
                 "преобладают усиливающие факторы (топ-5 по |SHAP|).",
    "alt_recs": ["MCC 5912 — score 61%", "MCC 5541 — score 44%"],
    "feature_interpretations": {
        "mcc_5411_cnt_90d": "сильно повышает вероятность",
        "monetary_total_90d": "сильно повышает вероятность",
        "recency_days": "умеренно повышает вероятность",
        "evening_ratio": "слегка снижает вероятность",
        "mcc_5912_sum_90d": "умеренно повышает вероятность",
    },
}

# Матрица прав ролей (фаза 26), uppercase-ключи.
_PERM_ALL = {"dashboard": True, "campaigns_view": True, "campaigns_create": True,
             "campaigns_edit": True, "campaigns_delete": True, "analytics": True,
             "users": True}
ROLE_PERMISSIONS = {
    "ADMIN": dict(_PERM_ALL),
    "MARKETER": {**_PERM_ALL, "campaigns_delete": False, "analytics": False,
                 "users": False},
    "ANALYST": {"dashboard": True, "campaigns_view": True, "campaigns_create": False,
                "campaigns_edit": False, "campaigns_delete": False, "analytics": True,
                "users": False},
}

# Ростер клиентов для левой панели ML-объяснений (фаза 25).
ML_CUSTOMERS = [
    {"customer_id": str(uuid.uuid4()), "name": "u-10293",
     "segment": "Премиум", "prediction": 0.78},
    {"customer_id": str(uuid.uuid4()), "name": "u-40571",
     "segment": "Массовый", "prediction": 0.55},
    {"customer_id": str(uuid.uuid4()), "name": "u-88120",
     "segment": "Молодежь", "prediction": 0.41},
]

ML_LIMITS = {
    "global_enabled": True,
    "limits": [
        {"segment_bucket": "business", "min_rate": "3.00", "max_rate": "12.00",
         "daily_budget": "150000.00", "auto_approve": True, "risk_level": "medium"},
        {"segment_bucket": "mass", "min_rate": "1.00", "max_rate": "7.00",
         "daily_budget": "80000.00", "auto_approve": False, "risk_level": "low"},
        {"segment_bucket": "premium", "min_rate": "3.00", "max_rate": "15.00",
         "daily_budget": "200000.00", "auto_approve": True, "risk_level": "medium"},
        {"segment_bucket": "senior", "min_rate": "2.00", "max_rate": "8.00",
         "daily_budget": "60000.00", "auto_approve": True, "risk_level": "low"},
        {"segment_bucket": "young", "min_rate": "2.00", "max_rate": "10.00",
         "daily_budget": "100000.00", "auto_approve": False, "risk_level": "high"},
    ],
    "updated_by": "admin@bank.ru",
    "updated_at": iso(NOW),
}

# B39: ответ сервиса, когда лимиты записаны в БД, а снапшот для R7 — нет.
ML_LIMITS_NOT_PUBLISHED = "saved but not published to the serving path; it will be retried"
ML_REPUBLISH_INTERVAL_S = 1.0   # в сервисе — задача планировщика раз в 60 с


def ml_limits_snapshot():
    """Снапшот ml_limits:snapshot (build_snapshot сервиса): корзины с enabled."""
    return {
        "global_enabled": ML_LIMITS["global_enabled"],
        "buckets": {
            item["segment_bucket"]: {"min_rate": float(item["min_rate"]),
                                     "max_rate": float(item["max_rate"]),
                                     "enabled": True}
            for item in sorted(ML_LIMITS["limits"], key=lambda x: x["segment_bucket"])
        },
    }

# Справочники (фаза 21): сегменты с живым размером аудитории + MCC.
SEGMENTS_REF = [
    {"id": "premium",  "name": "Премиум",       "count": 124500, "deciles": [9, 10]},
    {"id": "mass",     "name": "Массовый",      "count": 892000, "deciles": [5, 6, 7, 8]},
    {"id": "young",    "name": "Молодежь",      "count": 340000, "deciles": [3, 4]},
    {"id": "senior",   "name": "Средний класс", "count": 210000, "deciles": [2]},
    {"id": "business", "name": "Бизнес",        "count": 87000,  "deciles": [1]},
]

MCC_REF = [
    {"code": "5411", "name": "Супермаркеты",        "icon": "shopping-cart"},
    {"code": "5912", "name": "Аптеки",              "icon": "pill"},
    {"code": "5541", "name": "АЗС",                 "icon": "fuel"},
    {"code": "5812", "name": "Рестораны",           "icon": "utensils"},
    {"code": "5999", "name": "Прочая розница",      "icon": "store"},
    {"code": "7011", "name": "Отели",               "icon": "hotel"},
    {"code": "4111", "name": "Транспорт",           "icon": "bus"},
    {"code": "5045", "name": "Электроника",         "icon": "laptop"},
    {"code": "5600", "name": "Одежда",              "icon": "shirt"},
    {"code": "7832", "name": "Кинотеатры",          "icon": "clapperboard"},
    {"code": "5251", "name": "DIY / Строительство", "icon": "hammer"},
    {"code": "5122", "name": "Косметика",           "icon": "sparkles"},
]

# ── Пользователи админки и сессии (этап B, STAGE_B_CONTRACT §2–§5) ──────────
# Пароли — только для E2E (frontend/e2e/utils.ts: STUB_USERS). Встроенного
# admin/admin больше нет: bootstrap@bank.ru — админ с временным паролем
# (must_change_password), как после миграции 002 без ADMIN_BOOTSTRAP_PASSWORD.
ADMIN_USERS = [
    {"user_id": str(uuid.uuid4()), "email": "admin@bank.ru", "full_name": "Аристарх Панин",
     "role": "ADMIN", "is_active": True, "created_at": iso(NOW), "last_login_at": iso(NOW),
     "must_change_password": False,
     "_password": "Admin-e2e-Passw0rd", "_ver": 0},
    {"user_id": str(uuid.uuid4()), "email": "m.sokolova@bank.ru", "full_name": "Мария Соколова",
     "role": "MARKETER", "is_active": True, "created_at": iso(NOW), "last_login_at": None,
     "must_change_password": False,
     "_password": "Marketer-e2e-Passw0rd", "_ver": 0},
    {"user_id": str(uuid.uuid4()), "email": "d.ivanov@bank.ru", "full_name": "Дмитрий Иванов",
     "role": "ANALYST", "is_active": True, "created_at": iso(NOW), "last_login_at": None,
     "must_change_password": False,
     "_password": "Analyst-e2e-Passw0rd", "_ver": 0},
    {"user_id": str(uuid.uuid4()), "email": "bootstrap@bank.ru", "full_name": "Временный Администратор",
     "role": "ADMIN", "is_active": True, "created_at": iso(NOW), "last_login_at": None,
     "must_change_password": True,
     "_password": "Bootstrap-tmp-0001", "_ver": 0},
]


def public_user(u):
    return {k: v for k, v in u.items() if not k.startswith("_")}


def user_by_id(uid):
    return next((x for x in ADMIN_USERS if x["user_id"] == uid), None)


def user_by_email(email):
    return next((x for x in ADMIN_USERS if x["email"] == email), None)


ACCESS_TTL_S = 15 * 60
REFRESH_TTL_S = 7 * 24 * 3600
SSE_TTL_S = 60
SSE_MAX_SUBSCRIBERS = 200
LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_FAILURES_PER_EMAIL_IP = 5
LOGIN_MAX_ATTEMPTS_PER_IP = 30
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72
MAX_PASSWORD_CHARS = 128
PASSWORD_CHANGE_REQUIRED = "password change required"
WEAK_PASSWORDS = {
    "dev-secret-change-me", "please-change-me", "change-me", "changeme", "secret",
    "test-secret", "admin", "password", "qwerty", "123456", "pass123", "admin123",
}
PERMISSION_KEYS = tuple(_PERM_ALL)

# Состояние сессий — в памяти (в сервисе: JWT + Redis + token_version в БД).
STATE_LOCK = threading.Lock()
ACCESS = {}            # access-токен → {"uid", "ver", "pwd_change", "exp"}
REFRESH = {}           # каждый выданный refresh → {"uid", "fam", "ver", "exp"}
REFRESH_UNUSED = set()  # auth:refresh:{jti} — одноразовость (GETDEL)
FAMILIES_REVOKED = set()  # auth:refresh_fam_revoked:{fam}
SSE_TOKENS = {}        # sse-токен → {"uid", "ver", "exp"}
SSE_STREAMS = set()    # открытые потоки (threading.Event на поток)
THROTTLE = {}          # ключ → [счётчик, начало окна]
CONTROL = {"refresh_delay_ms": 0, "session_store_down": False, "sse_stats_event": True,
           # «Redis serving-пути недоступен»: PUT /ml-limits → 503, снапшот не обновляется.
           "ml_limits_publish_down": False}
TRACE = {              # наблюдаемые побочные эффекты — для E2E (/__stub/state)
    "refresh_ok": 0, "refresh_reuse": 0, "logout_tokens": [],
    "sse_tokens": [], "sse_connects": [], "explain_calls": [], "rec_calls": 0,
    # Опубликованный снапшот ML-лимитов (то, что видит R7) и число публикаций.
    "ml_limits_snapshot": ml_limits_snapshot(), "ml_limits_publishes": 0,
}


def publish_ml_limits():
    """Публикация снапшота; False — serving-путь недоступен (вызывать под STATE_LOCK)."""
    if CONTROL["ml_limits_publish_down"]:
        return False
    TRACE["ml_limits_snapshot"] = ml_limits_snapshot()
    TRACE["ml_limits_publishes"] += 1
    return True


def republish_ml_limits_forever():
    """Задача планировщика republish-ml-limits: своя, независимая от PUT."""
    while True:
        time.sleep(ML_REPUBLISH_INTERVAL_S)
        with STATE_LOCK:
            if TRACE["ml_limits_snapshot"] != ml_limits_snapshot():
                publish_ml_limits()


def token_pair(access, refresh, must_change_password):
    """Тело ответа /auth/login, /auth/refresh, /auth/change-password."""
    return {"access_token": access, "refresh_token": refresh,
            "token_type": "bearer", "must_change_password": bool(must_change_password)}


def sse_token_response(token):
    """Тело ответа POST /events/token."""
    return {"token": token, "expires_in": SSE_TTL_S}


def explain_response(user_id):
    """Тело ответа GET /ml/explain/{user_id} (форма serving-ответа rec_api)."""
    return dict(RECOMMENDATION, user_id=user_id)


def _new_token(kind):
    return f"stub-{kind}-{secrets.token_urlsafe(18)}"


def issue_pair(user, fam=None):
    fam = fam or uuid.uuid4().hex
    now = time.time()
    access, refresh = _new_token("access"), _new_token("refresh")
    with STATE_LOCK:
        ACCESS[access] = {"uid": user["user_id"], "ver": user["_ver"],
                          "pwd_change": bool(user["must_change_password"]),
                          "exp": now + ACCESS_TTL_S}
        REFRESH[refresh] = {"uid": user["user_id"], "fam": fam, "ver": user["_ver"],
                            "exp": now + REFRESH_TTL_S}
        REFRESH_UNUSED.add(refresh)
    return token_pair(access, refresh, user["must_change_password"])


def bump_token_version(user):
    """Смена роли/статуса/пароля гасит все выданные токены пользователя."""
    user["_ver"] += 1


def _throttle_hit(key, now):
    count, start = THROTTLE.get(key, (0, now))
    if now - start >= LOGIN_WINDOW_S:
        count, start = 0, now
    THROTTLE[key] = (count + 1, start)
    return count + 1, start


def throttle_attempt(email, ip):
    """Retry-After (с), если лимит исчерпан; попытка засчитывается сразу."""
    now = time.time()
    with STATE_LOCK:
        pair_n, pair_start = _throttle_hit(f"fail:{email}:{ip}", now)
        ip_n, ip_start = _throttle_hit(f"ip:{ip}", now)
    if pair_n > LOGIN_MAX_FAILURES_PER_EMAIL_IP:
        return max(1, int(LOGIN_WINDOW_S - (now - pair_start)))
    if ip_n > LOGIN_MAX_ATTEMPTS_PER_IP:
        return max(1, int(LOGIN_WINDOW_S - (now - ip_start)))
    return None


def throttle_reset(email, ip):
    with STATE_LOCK:
        THROTTLE.pop(f"fail:{email}:{ip}", None)


def password_policy_violation(new, *, email, current):
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(new.encode()) > MAX_PASSWORD_BYTES:
        return f"password must be at most {MAX_PASSWORD_BYTES} bytes"
    if new.strip().lower() in WEAK_PASSWORDS:
        return "password is a known default"
    if new.strip().lower() == email.strip().lower():
        return "password must differ from email"
    if new == current:
        return "new password must differ from the current one"
    return None


def change_password_schema_error(current, new):
    """Ошибка pydantic-схемы ChangePasswordRequest (элемент списка detail 422):
    current_password 1..128 символов, new_password 12..128 символов и не
    длиннее 72 байт (предел bcrypt, AfterValidator — после длины)."""
    def err(kind, field, msg):
        return {"type": kind, "loc": ["body", field], "msg": msg}
    if not current:
        return err("string_too_short", "current_password",
                   "String should have at least 1 character")
    if len(current) > MAX_PASSWORD_CHARS:
        return err("string_too_long", "current_password",
                   f"String should have at most {MAX_PASSWORD_CHARS} characters")
    if len(new) < MIN_PASSWORD_LENGTH:
        return err("string_too_short", "new_password",
                   f"String should have at least {MIN_PASSWORD_LENGTH} characters")
    if len(new) > MAX_PASSWORD_CHARS:
        return err("string_too_long", "new_password",
                   f"String should have at most {MAX_PASSWORD_CHARS} characters")
    if len(new.encode()) > MAX_PASSWORD_BYTES:
        return err("value_error", "new_password",
                   f"Value error, password must be at most {MAX_PASSWORD_BYTES} bytes")
    return None


# ── Контракт §4: маршрут → требование (PUBLIC — без аутентификации) ─────────
PUBLIC, AUTHENTICATED, PWD_CHANGE_OK, ADMIN_ROLE = "public", "auth", "pwd_change_ok", "role:ADMIN"
_UUID = r"[0-9a-f-]{36}"
ROUTE_REQUIREMENTS = [
    ("GET", r"/health/(live|ready)", PUBLIC),
    ("POST", r"/auth/(login|refresh|logout)", PUBLIC),
    ("GET", r"/events/stream", PUBLIC),  # но обязателен sse-токен в ?token=
    ("GET", r"/auth/me", PWD_CHANGE_OK),
    ("POST", r"/auth/change-password", PWD_CHANGE_OK),
    ("GET", r"/reference/[^/]+", AUTHENTICATED),
    ("POST", r"/events/token", "dashboard"),
    ("GET", rf"/campaigns(/active|/audience/estimate|/applicable/[^/]+|/{_UUID}(/stats|/budget)?)?",
     "campaigns_view"),
    ("POST", r"/campaigns", "campaigns_create"),
    ("PATCH", rf"/campaigns/{_UUID}(/status)?", "campaigns_edit"),
    ("DELETE", rf"/campaigns/{_UUID}", "campaigns_delete"),
    ("POST", rf"/campaigns/{_UUID}/archive", "campaigns_delete"),
    ("GET", r"/analytics/[^/]+", "analytics"),
    ("GET", rf"/experiments(/{_UUID}(/results)?)?", "analytics"),
    ("GET", r"/ml/customers", "analytics"),
    ("GET", r"/ml/explain/[^/]+", "analytics"),
    ("GET", r"/ml-limits", "analytics"),
    ("POST", r"/experiments", "campaigns_edit"),
    ("PATCH", rf"/experiments/{_UUID}/status", "campaigns_edit"),
    ("POST", rf"/experiments/{_UUID}/assign/[^/]+", "campaigns_edit"),
    ("PUT", r"/ml-limits", ADMIN_ROLE),
    ("GET", r"/auth/users", "users"),
    ("POST", r"/auth/users", "users"),
    ("PATCH", rf"/auth/users/{_UUID}", "users"),
    ("GET", r"/roles/permissions", "users"),
    ("PATCH", r"/roles/(ADMIN|MARKETER|ANALYST)/permissions", ADMIN_ROLE),
]


def requirement(method, path):
    for m, pattern, req in ROUTE_REQUIREMENTS:
        if m == method and re.fullmatch(pattern, path):
            return req
    return None


LOG = []


class Handler(BaseHTTPRequestHandler):
    service = "campaign"

    def _send(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _no_content(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _unauthorized(self, detail):
        # Как security._unauthorized: вызов Bearer — признак отказа по токену.
        self._send(401, {"detail": detail}, {"WWW-Authenticate": "Bearer"})

    def _forbidden(self, detail):
        self._send(403, {"detail": detail})

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def log_message(self, fmt, *args):
        LOG.append(f"{self.service} {fmt % args}")
        print(f"[{self.service}] {fmt % args}", flush=True)

    # ── аутентификация / авторизация (security._authenticate, rbac) ─────────
    def _authenticate(self, *, allow_pwd_change):
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer "):
            return self._unauthorized("authentication required")
        rec = ACCESS.get(h.removeprefix("Bearer "))
        if rec is None or rec["exp"] < time.time():
            return self._unauthorized("invalid or expired token")
        user = user_by_id(rec["uid"])
        if user is None or not user["is_active"] or user["_ver"] != rec["ver"]:
            return self._unauthorized("token revoked")
        if not allow_pwd_change and (user["must_change_password"] or rec["pwd_change"]):
            return self._forbidden(PASSWORD_CHANGE_REQUIRED)
        return user

    def _authorize(self, req):
        """Пользователь или None (ответ 401/403 уже отправлен)."""
        user = self._authenticate(allow_pwd_change=req == PWD_CHANGE_OK)
        if user is None or req in (AUTHENTICATED, PWD_CHANGE_OK):
            return user
        if req == ADMIN_ROLE:
            if user["role"] != "ADMIN":
                return self._forbidden(
                    f"role {user['role']} is not allowed (need one of: ['ADMIN'])")
            return user
        # Роль — текущая из «БД», права роли — без кэша (контракт §3/§4).
        if user["role"] != "ADMIN" and not ROLE_PERMISSIONS.get(user["role"], {}).get(req):
            return self._forbidden(f"role {user['role']} lacks permission '{req}'")
        return user

    def _gate(self, method):
        """Разбор URL + требование маршрута. (path, query, user) или None."""
        u = urlparse(self.path)
        path, query = u.path, parse_qs(u.query)
        if self.service == "campaign" and path.startswith("/__stub/"):
            self._control(method, path)
            return None
        req = requirement(method, path)
        if req is None:
            self._send(404, {"detail": "not found"})
            return None
        if req == PUBLIC:
            return path, query, None
        user = self._authorize(req)
        return (path, query, user) if user is not None else None

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        gated = self._gate("GET")
        if gated is None:
            return
        p, q, user = gated

        if p in ("/health/live", "/health/ready"):
            return self._send(200, {"status": "ok"})
        if p == "/events/stream":
            return self._stream((q.get("token") or [None])[0])
        if p == "/auth/me":
            # Фаза 26: живые права роли для фронт-гейтов.
            me = dict(public_user(user))
            me["permissions"] = (dict(_PERM_ALL) if user["role"] == "ADMIN"
                                 else ROLE_PERMISSIONS.get(user["role"], {}))
            return self._send(200, me)
        if p == "/roles/permissions":
            return self._send(200, ROLE_PERMISSIONS)
        if p == "/auth/users":
            return self._send(200, [public_user(x) for x in ADMIN_USERS])
        if p == "/reference/segments":
            return self._send(200, SEGMENTS_REF)
        if p == "/reference/mcc-categories":
            return self._send(200, MCC_REF)
        if p == "/ml/customers":
            return self._send(200, ML_CUSTOMERS)
        m = re.fullmatch(r"/ml/explain/([^/]+)", p)
        if m:
            return self._explain(m.group(1))
        if p == "/ml-limits":
            return self._send(200, ML_LIMITS)
        if p == "/analytics/kpis":
            seg = (q.get("segment_id") or [None])[0]
            # Сегмент-фильтр сужает охват — цифры меняются (фаза 24).
            return self._send(200, {**KPIS, "reach": 210000 if seg else KPIS["reach"]})
        if p == "/analytics/daily-trend":
            return self._send(200, {
                "campaign_id": (q.get("campaign_id") or [None])[0],
                "period_days": int((q.get("period") or ["30"])[0]),
                "points": TREND_POINTS,
            })
        if p == "/analytics/channels":
            return self._send(200, CHANNELS)
        if p == "/experiments":
            return self._send(200, EXPERIMENTS)
        m = re.fullmatch(r"/experiments/([0-9a-f-]{36})/results", p)
        if m:
            exp = next((e for e in EXPERIMENTS if e["experiment_id"] == m.group(1)), None)
            if exp is None:
                return self._send(404, {"detail": "experiment not found"})
            return self._send(200, experiment_results(exp))
        if p == "/campaigns":
            status = (q.get("status") or [None])[0]
            items = [c for c in CAMPAIGNS if (not status or c["status"] == status)
                     and (include_archived(q) or c["archived_at"] is None)]
            return self._send(200, items)
        if p == "/campaigns/active":
            return self._send(200, [c for c in CAMPAIGNS if c["status"] == "ACTIVE"
                                    and (include_archived(q) or c["archived_at"] is None)])
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})/stats", p)
        if m:
            s = STATS.get(m.group(1))
            return self._send(200, s) if s else self._send(404, {"detail": "campaign not found"})
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})/budget", p)
        if m:
            c = next((x for x in CAMPAIGNS if x["campaign_id"] == m.group(1)), None)
            return (self._send(200, campaign_budget(c)) if c
                    else self._send(404, {"detail": "campaign not found"}))
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})", p)
        if m:
            c = next((x for x in CAMPAIGNS if x["campaign_id"] == m.group(1)), None)
            return self._send(200, c) if c else self._send(404, {"detail": "campaign not found"})
        if p == "/analytics/funnel":
            seg = (q.get("segment_id") or [None])[0]
            # Сегмент-фильтр сужает воронку на сервере — цифры меняются (фаза 24).
            scale = 0.2 if seg else 1.0
            steps = [{**s, "count": int(s["count"] * scale)} for s in FUNNEL_STEPS]
            return self._send(200, {
                "campaign_id": (q.get("campaign_id") or [None])[0],
                "period_days": int((q.get("period") or ["30"])[0]),
                "steps": steps,
            })
        if p == "/analytics/segment-matrix":
            return self._send(200, MATRIX)
        return self._send(404, {"detail": "not found"})

    def _explain(self, user_id):
        """Прокси на read-only explain rec_api (B27): 404 проходит насквозь,
        прочие сбои rec_api — 502."""
        TRACE["explain_calls"].append(user_id)
        if user_id == "00000000-0000-0000-0000-000000000000":
            return self._send(404, {"detail": f"user '{user_id}' not found in the feature store"})
        if user_id == "ffffffff-ffff-ffff-ffff-ffffffffffff":
            return self._send(502, {"detail": "recommendation service unavailable"})
        return self._send(200, explain_response(user_id))

    # ── SSE (контракт §5) ────────────────────────────────────────────────────
    def _sse_user(self, token):
        if not token:
            return self._unauthorized("authentication required")
        rec = SSE_TOKENS.get(token)
        if rec is None or rec["exp"] < time.time():
            return self._unauthorized("invalid or expired token")
        user = user_by_id(rec["uid"])
        if user is None or not user["is_active"] or user["_ver"] != rec["ver"]:
            return self._unauthorized("token revoked")
        if user["must_change_password"]:
            return self._forbidden(PASSWORD_CHANGE_REQUIRED)
        # Право dashboard — заново при подключении; токен одноразовый.
        if (user["role"] != "ADMIN"
                and not ROLE_PERMISSIONS.get(user["role"], {}).get("dashboard")):
            return self._forbidden(f"role {user['role']} lacks permission 'dashboard'")
        with STATE_LOCK:
            reused, rec["used"] = rec.get("used", False), True
        if reused:
            return self._unauthorized("sse token already used")
        return user

    def _stream(self, token):
        if self._sse_user(token) is None:
            return
        closed = threading.Event()
        with STATE_LOCK:
            if len(SSE_STREAMS) >= SSE_MAX_SUBSCRIBERS:
                return self._send(503, {"detail": "too many event stream subscribers"},
                                  {"Retry-After": "5"})
            SSE_STREAMS.add(closed)
            TRACE["sse_connects"].append(token)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            # Один stats-event на подключение (если не выключен через
            # /__stub/config), дальше heartbeat'ы, пока поток не закроют
            # (/__stub/sse/drop) или клиент не уйдёт.
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            if closed.wait(0.8):
                return
            if CONTROL["sse_stats_event"]:
                evt = json.dumps({"type": "stats",
                                  "campaign_id": CAMPAIGNS[0]["campaign_id"],
                                  "spent_delta": 250.0, "accepted_delta": 1})
                self.wfile.write(f"data: {evt}\n\n".encode())
                self.wfile.flush()
            while not closed.wait(2):
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with STATE_LOCK:
                SSE_STREAMS.discard(closed)

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        gated = self._gate("POST")
        if gated is None:
            return
        p, _q, user = gated
        if p == "/auth/login":
            return self._login()
        if p == "/auth/refresh":
            return self._refresh()
        if p == "/auth/logout":
            return self._logout()
        if p == "/auth/change-password":
            return self._change_password(user)
        if p == "/events/token":
            token = _new_token("sse")
            with STATE_LOCK:
                SSE_TOKENS[token] = {"uid": user["user_id"], "ver": user["_ver"],
                                     "exp": time.time() + SSE_TTL_S}
                TRACE["sse_tokens"].append(token)
            return self._send(200, sse_token_response(token))
        if p == "/auth/users":
            return self._create_user(user)
        if p == "/campaigns":
            return self._create_campaign()
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})/archive", p)
        if m:
            return self._archive_campaign(m.group(1))
        return self._send(404, {"detail": "not found"})

    def _archive_campaign(self, campaign_id):
        """Архив PAUSED/COMPLETED (B24); повторный вызов ничего не меняет."""
        c = next((x for x in CAMPAIGNS if x["campaign_id"] == campaign_id), None)
        if c is None:
            return self._send(404, {"detail": "campaign not found"})
        if c["status"] == "DRAFT":
            return self._send(409, {"detail": "a DRAFT campaign is not archived — delete it instead"})
        if c["status"] == "ACTIVE":
            return self._send(409, {"detail": "pause or complete the campaign first"})
        c["archived_at"] = c["archived_at"] or iso(datetime.now(UTC))
        return self._send(200, c)

    def _client_ip(self):
        return self.client_address[0]

    def _login(self):
        body = self._read_body()
        email = str(body.get("email") or "").strip().lower()
        ip = self._client_ip()
        retry_after = throttle_attempt(email, ip)
        if retry_after is not None:
            return self._send(429, {"detail": "too many login attempts"},
                              {"Retry-After": str(retry_after)})
        user = user_by_email(email)
        # Одно тело ответа для неизвестного email, неверного пароля и
        # отключённого пользователя.
        if user is None or user["_password"] != body.get("password") or not user["is_active"]:
            return self._send(401, {"detail": "invalid email or password"})
        throttle_reset(email, ip)
        user["last_login_at"] = iso(datetime.now(UTC))
        return self._send(200, issue_pair(user))

    def _session_store_down(self):
        return self._send(503, {"detail": "session store unavailable"})

    def _refresh(self):
        if CONTROL["refresh_delay_ms"]:
            time.sleep(CONTROL["refresh_delay_ms"] / 1000)
        if CONTROL["session_store_down"]:
            return self._session_store_down()  # fail closed
        token = str(self._read_body().get("refresh_token") or "")
        rec = REFRESH.get(token)
        if rec is None or rec["exp"] < time.time():
            return self._unauthorized("invalid or expired token")
        with STATE_LOCK:
            if token not in REFRESH_UNUSED:
                # Повторное предъявление: копия токена у кого-то ещё — гасим
                # всё семейство, в т.ч. токен «честного» клиента.
                FAMILIES_REVOKED.add(rec["fam"])
                TRACE["refresh_reuse"] += 1
                return self._unauthorized("refresh token reused or revoked")
            REFRESH_UNUSED.discard(token)
            revoked = rec["fam"] in FAMILIES_REVOKED
        if revoked:
            return self._unauthorized("refresh token reused or revoked")
        user = user_by_id(rec["uid"])
        if user is None or not user["is_active"] or user["_ver"] != rec["ver"]:
            return self._unauthorized("token revoked")
        TRACE["refresh_ok"] += 1
        return self._send(200, issue_pair(user, fam=rec["fam"]))

    def _logout(self):
        token = str(self._read_body().get("refresh_token") or "")
        rec = REFRESH.get(token)
        if rec is not None:
            if CONTROL["session_store_down"]:
                return self._session_store_down()
            with STATE_LOCK:
                FAMILIES_REVOKED.add(rec["fam"])
                REFRESH_UNUSED.discard(token)
        TRACE["logout_tokens"].append(token)
        return self._no_content()  # идемпотентно, и для неизвестного токена

    def _change_password(self, user):
        body = self._read_body()
        current = str(body.get("current_password") or "")
        new = str(body.get("new_password") or "")
        # Схема ChangePasswordRequest (pydantic) — до тела эндпоинта:
        # раньше троттлинга и проверки текущего пароля.
        schema_error = change_password_schema_error(current, new)
        if schema_error is not None:
            return self._send(422, {"detail": [schema_error]})
        ip = self._client_ip()
        retry_after = throttle_attempt(user["email"], ip)
        if retry_after is not None:
            return self._send(429, {"detail": "too many login attempts"},
                              {"Retry-After": str(retry_after)})
        if current != user["_password"]:
            # Без WWW-Authenticate: отказ по данным формы, а не по токену.
            return self._send(401, {"detail": "invalid current password"})
        throttle_reset(user["email"], ip)
        problem = password_policy_violation(new, email=user["email"], current=current)
        if problem is not None:
            return self._send(422, {"detail": problem})
        user["_password"] = new
        user["must_change_password"] = False
        bump_token_version(user)  # старые access/refresh/sse гаснут
        return self._send(200, issue_pair(user))

    def _admin_rule_violation(self, actor, target_role=None, new_role=None):
        # Делегируемое право users не даёт трогать ADMIN-аккаунты (контракт §4).
        if actor["role"] != "ADMIN" and "ADMIN" in (target_role, new_role):
            return "only ADMIN may create, promote or modify ADMIN accounts"
        return None

    def _create_user(self, actor):
        body = self._read_body()
        role = body.get("role") or "ANALYST"
        problem = self._admin_rule_violation(actor, new_role=role)
        if problem:
            return self._forbidden(problem)
        email = str(body.get("email") or "").lower()
        if user_by_email(email):
            return self._send(409, {"detail": "email already registered"})
        user = {"user_id": str(uuid.uuid4()), "email": email,
                "full_name": body["full_name"], "role": role,
                "is_active": True, "created_at": iso(NOW), "last_login_at": None,
                "must_change_password": False,
                "_password": body.get("password", "x"), "_ver": 0}
        ADMIN_USERS.append(user)
        return self._send(201, public_user(user))

    def _create_campaign(self):
        body = self._read_body()
        c = {
            "campaign_id": str(uuid.uuid4()),
            "name": body.get("name", "?"),
            "target_segment_ids": body.get("target_segment_ids", []),
            "cashback_rate": str(body.get("cashback_rate", "0")),
            "min_transaction_amount": (
                str(body["min_transaction_amount"])
                if body.get("min_transaction_amount") is not None else None
            ),
            "budget_total": str(body.get("budget_total", "0")),
            "budget_spent": "0",
            "status": "DRAFT",
            "start_date": body.get("start_date"),
            "end_date": body.get("end_date"),
            "allowed_channels": body.get("allowed_channels", []),
            "require_existing_behavior": bool(body.get("require_existing_behavior")),
            "rate_tiers": body.get("rate_tiers"),
            "mcc_codes": body.get("mcc_codes", []),
            # Поля визарда (фаза 22).
            "daily_limit": (
                str(body["daily_limit"])
                if body.get("daily_limit") is not None else None
            ),
            "auto_pause": bool(body.get("auto_pause", True)),
            "rfm_min": body.get("rfm_min"),
            "rfm_max": body.get("rfm_max"),
            "created_by": None,
            "archived_at": None,
            "min_tx_amounts": {
                code: str((body.get("min_tx_amounts") or {}).get(
                    code, body.get("min_transaction_amount")))
                for code in body.get("mcc_codes", [])
                if (body.get("min_tx_amounts") or {}).get(
                    code, body.get("min_transaction_amount")) is not None
            },
        }
        CAMPAIGNS.append(c)
        STATS[c["campaign_id"]] = {
            "campaign_id": c["campaign_id"], "impressions": 0, "clicks": 0,
            "accepted": 0, "transactions": 0, "revenue": "0", "cashback_paid": "0",
            "ctr": 0.0, "conversion_rate": 0.0, "roi": 0.0,
        }
        return self._send(201, c)

    # ── PUT ──────────────────────────────────────────────────────────────────
    def do_PUT(self):
        gated = self._gate("PUT")
        if gated is None:
            return
        p, _q, user = gated
        if p == "/ml-limits":
            body = self._read_body()
            # Сначала «коммит в Postgres», затем публикация снапшота (B39).
            with STATE_LOCK:
                if body.get("global_enabled") is not None:
                    ML_LIMITS["global_enabled"] = bool(body["global_enabled"])
                for item in body.get("limits") or []:
                    for existing in ML_LIMITS["limits"]:
                        if existing["segment_bucket"] == item["segment_bucket"]:
                            existing.update({
                                "min_rate": str(item["min_rate"]),
                                "max_rate": str(item["max_rate"]),
                                "daily_budget": str(item["daily_budget"]),
                                "auto_approve": bool(item.get("auto_approve")),
                                "risk_level": item.get("risk_level", "medium"),
                            })
                ML_LIMITS["updated_by"] = user["email"]
                ML_LIMITS["updated_at"] = iso(datetime.now(UTC))
                published = publish_ml_limits()
            if not published:
                return self._send(503, {"detail": ML_LIMITS_NOT_PUBLISHED})
            return self._send(200, ML_LIMITS)
        return self._send(404, {"detail": "not found"})

    # ── PATCH ────────────────────────────────────────────────────────────────
    def do_PATCH(self):
        gated = self._gate("PATCH")
        if gated is None:
            return
        p, q, actor = gated
        # Фаза 26: PATCH /roles/{role}/permissions (только ADMIN, §4).
        mrole = re.fullmatch(r"/roles/(ADMIN|MARKETER|ANALYST)/permissions", p)
        if mrole:
            role = mrole.group(1)
            if role == "ADMIN":
                return self._forbidden("ADMIN role always has full access and cannot be modified")
            patch = self._read_body().get("permissions") or {}
            unknown = sorted(set(patch) - set(PERMISSION_KEYS))
            if unknown:
                return self._send(422, {"detail": f"unknown permission keys: {unknown}"})
            ROLE_PERMISSIONS[role].update({k: bool(v) for k, v in patch.items()})
            return self._send(200, ROLE_PERMISSIONS[role])
        m = re.fullmatch(r"/auth/users/([0-9a-f-]{36})", p)
        if m:
            return self._update_user(actor, m.group(1))
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})/status", p)
        if m:
            c = next((x for x in CAMPAIGNS if x["campaign_id"] == m.group(1)), None)
            if not c:
                return self._send(404, {"detail": "campaign not found"})
            action = (q.get("action") or [""])[0]
            fsm = {
                ("DRAFT", "activate"): "ACTIVE",
                ("ACTIVE", "pause"): "PAUSED",
                ("ACTIVE", "complete"): "COMPLETED",
                ("PAUSED", "activate"): "ACTIVE",
                ("PAUSED", "complete"): "COMPLETED",
            }
            new = fsm.get((c["status"], action))
            if not new:
                return self._send(409, {"detail": f"cannot {action} a campaign in state '{c['status']}'"})
            if c["archived_at"] is not None:
                return self._send(409, {"detail": "campaign is archived"})
            # B25: после end_date не активируем; до start_date — можно.
            if new == "ACTIVE" and parse_dt(c["end_date"]) <= datetime.now(UTC):
                return self._send(409, {"detail": "campaign has already ended (end_date has passed)"})
            prev, c["status"] = c["status"], new
            return self._send(200, status_action_response(c, prev, action))
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})", p)
        if m:
            c = next((x for x in CAMPAIGNS if x["campaign_id"] == m.group(1)), None)
            if not c:
                return self._send(404, {"detail": "campaign not found"})
            if c["status"] != "DRAFT":
                return self._send(409, {"detail": f"only DRAFT campaigns are editable (status={c['status']})"})
            body = self._read_body()
            for k in ("name", "target_segment_ids", "allowed_channels",
                      "require_existing_behavior", "rate_tiers", "mcc_codes",
                      "start_date", "end_date",
                      "auto_pause", "rfm_min", "rfm_max", "min_tx_amounts"):
                if k in body and body[k] is not None:
                    c[k] = body[k]
            for k in ("cashback_rate", "min_transaction_amount", "budget_total",
                      "daily_limit"):
                if k in body and body[k] is not None:
                    c[k] = str(body[k])
            return self._send(200, c)
        return self._send(404, {"detail": "not found"})

    def _update_user(self, actor, user_id):
        user = user_by_id(user_id)
        if user is None:
            return self._send(404, {"detail": "user not found"})
        body = self._read_body()
        problem = self._admin_rule_violation(actor, target_role=user["role"],
                                             new_role=body.get("role"))
        if problem:
            return self._forbidden(problem)
        if user_id == actor["user_id"] and (
                body.get("role") not in (None, actor["role"]) or body.get("is_active") is False):
            return self._send(409, {"detail": "cannot demote or deactivate your own account"})
        revoke = any(k in body and body[k] is not None and body[k] != user[k]
                     for k in ("role", "is_active"))
        if body.get("password"):
            user["_password"] = body["password"]
            revoke = True
        for k in ("full_name", "role", "is_active"):
            if k in body and body[k] is not None:
                user[k] = body[k]
        if revoke:
            bump_token_version(user)
        return self._send(200, public_user(user))

    # ── DELETE ───────────────────────────────────────────────────────────────
    def do_DELETE(self):
        gated = self._gate("DELETE")
        if gated is None:
            return
        p, _q, _user = gated
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})", p)
        if m:
            c = next((x for x in CAMPAIGNS if x["campaign_id"] == m.group(1)), None)
            if not c:
                return self._send(404, {"detail": "campaign not found"})
            # B24: только черновик без истории; журнал начислений не удаляется.
            if c["status"] != "DRAFT" or has_history(c["campaign_id"]):
                return self._send(409, {"detail": "campaign has history — archive it instead"})
            CAMPAIGNS.remove(c)
            STATS.pop(c["campaign_id"], None)
            return self._no_content()
        return self._send(404, {"detail": "not found"})

    # ── Управление стабом из E2E (только :8002, не часть контракта) ──────────
    def _control(self, method, path):
        body = self._read_body() if method != "GET" else {}
        if method == "GET" and path == "/__stub/state":
            return self._send(200, {
                **TRACE,
                "families_revoked": len(FAMILIES_REVOKED),
                "sse_open": len(SSE_STREAMS),
                "users": {u["email"]: {"ver": u["_ver"], "role": u["role"],
                                       "is_active": u["is_active"],
                                       "must_change_password": u["must_change_password"]}
                          for u in ADMIN_USERS},
            })
        if method == "POST" and path == "/__stub/expire-access":
            # «Прошло 15 минут»: access-токены (все или одного email) протухли.
            uid = (user_by_email(body["email"]) or {}).get("user_id") if body.get("email") else None
            with STATE_LOCK:
                for token in [t for t, r in ACCESS.items() if uid is None or r["uid"] == uid]:
                    ACCESS[token]["exp"] = 0
            return self._no_content()
        if method == "POST" and path == "/__stub/config":
            CONTROL.update({k: body[k] for k in CONTROL if k in body})
            return self._send(200, CONTROL)
        if method == "POST" and path == "/__stub/sse/drop":
            with STATE_LOCK:
                for closed in SSE_STREAMS:
                    closed.set()
            return self._no_content()
        if method == "POST" and path == "/__stub/reset-throttle":
            with STATE_LOCK:
                THROTTLE.clear()
            return self._no_content()
        m = re.fullmatch(r"/__stub/users/([^/]+)", path)
        if method == "POST" and m:
            # Правка «строки admin_users» в обход API (оператор / другой админ).
            user = user_by_email(m.group(1))
            if user is None:
                return self._send(404, {"detail": "user not found"})
            for k in ("must_change_password", "role", "is_active"):
                if k in body:
                    user[k] = body[k]
            if body.get("bump_token_version") or "role" in body or "is_active" in body:
                bump_token_version(user)
            return self._send(200, public_user(user))
        m = re.fullmatch(r"/__stub/role-permissions/(MARKETER|ANALYST)", path)
        if method == "POST" and m:
            ROLE_PERMISSIONS[m.group(1)].update({k: bool(v) for k, v in body.items()})
            return self._send(200, ROLE_PERMISSIONS[m.group(1)])
        return self._send(404, {"detail": "unknown stub control"})


class RecHandler(Handler):
    """recommendation_api: serving и explain принимают только сервисные
    токены (iss mobile-api / campaign-manager, SERVICE_JWT_SECRET) — админский
    access-токен здесь 401 (контракт §6). Админка сюда не ходит."""
    service = "recommendation"

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health/live":
            return self._send(200, {"status": "ok"})
        if re.fullmatch(r"/recommendations/[^/]+(/explain)?", p):
            TRACE["rec_calls"] += 1
            return self._unauthorized("invalid or expired token")
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        return self._send(404, {"detail": "not found"})

    do_PUT = do_PATCH = do_DELETE = do_POST


def serve(port, handler):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    # Порты переопределяемы (локальные прогоны рядом с живым стеком);
    # vite-прокси берёт цели из CAMPAIGN_API_TARGET / RECOMMENDATION_API_TARGET.
    campaign_port = int(os.environ.get("STUB_CAMPAIGN_PORT", "8002"))
    rec_port = int(os.environ.get("STUB_RECOMMENDATION_PORT", "8001"))
    serve(campaign_port, Handler)
    serve(rec_port, RecHandler)
    threading.Thread(target=republish_ml_limits_forever, daemon=True).start()
    print(f"stub backends on :{rec_port} (rec) and :{campaign_port} (campaign)", flush=True)
    threading.Event().wait()
