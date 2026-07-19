#!/usr/bin/env python3
"""Stub FastAPI-shaped backends on :8001 (recommendation) and :8002 (campaign).

Mirrors the exact JSON wire format of the real services (Decimal → strings,
UUID ids, ISO datetimes) so the frontend live-mode wiring can be verified
without the docker stack.
"""
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


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
    for c, imp in zip(CAMPAIGNS, [12000, 4200, 0])
}

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

ADMIN_USERS = [
    {"user_id": str(uuid.uuid4()), "email": "admin@bank.ru", "full_name": "Аристарх Панин",
     "role": "ADMIN", "is_active": True, "created_at": iso(NOW), "last_login_at": iso(NOW),
     "_password": "admin"},
    {"user_id": str(uuid.uuid4()), "email": "m.sokolova@bank.ru", "full_name": "Мария Соколова",
     "role": "MARKETER", "is_active": True, "created_at": iso(NOW), "last_login_at": None,
     "_password": "marketer"},
    {"user_id": str(uuid.uuid4()), "email": "d.ivanov@bank.ru", "full_name": "Дмитрий Иванов",
     "role": "ANALYST", "is_active": True, "created_at": iso(NOW), "last_login_at": None,
     "_password": "analyst"},
]


def public_user(u):
    return {k: v for k, v in u.items() if not k.startswith("_")}


LOG = []


class Handler(BaseHTTPRequestHandler):
    service = "campaign"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def log_message(self, fmt, *args):
        LOG.append(f"{self.service} {fmt % args}")
        print(f"[{self.service}] {fmt % args}", flush=True)

    def _auth_user(self):
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer stub-access-"):
            return None
        email = h.removeprefix("Bearer stub-access-")
        return next((x for x in ADMIN_USERS if x["email"] == email), None)

    def _require_auth(self):
        if self._auth_user() is None:
            self._send(401, {"detail": "authentication required"})
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p == "/health/live":
            return self._send(200, {"status": "ok"})

        # SSE realtime (фаза 19): держим соединение, шлём один stats-event
        # и дальше heartbeat'ы — EventSource в браузере не должен упасть.
        if self.service == "campaign" and p == "/events/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                time.sleep(0.8)
                evt = json.dumps({"type": "stats",
                                  "campaign_id": CAMPAIGNS[0]["campaign_id"],
                                  "spent_delta": 250.0, "accepted_delta": 1})
                self.wfile.write(f"data: {evt}\n\n".encode())
                self.wfile.flush()
                for _ in range(600):  # ~20 мин heartbeat'ов, до дисконнекта
                    time.sleep(2)
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if self.service == "campaign" and p == "/auth/me":
            user = self._auth_user()
            if user is None:
                return self._send(401, {"detail": "authentication required"})
            # Фаза 26: живые права роли для фронт-гейтов.
            me = dict(public_user(user))
            me["permissions"] = ROLE_PERMISSIONS.get(user.get("role"), {})
            return self._send(200, me)

        if self.service == "campaign" and p == "/roles/permissions":
            u2 = self._auth_user()
            if u2 is None:
                return self._send(401, {"detail": "authentication required"})
            if u2.get("role") != "ADMIN":
                return self._send(403, {"detail": "role is not allowed"})
            return self._send(200, ROLE_PERMISSIONS)

        if self.service == "campaign" and p == "/auth/users":
            if not self._require_auth():
                return
            return self._send(200, [public_user(x) for x in ADMIN_USERS])

        if self.service == "campaign" and (p.startswith("/campaigns")
                                            or p.startswith("/analytics")
                                            or p.startswith("/experiments")
                                            or p.startswith("/reference")
                                            or p.startswith("/ml/")
                                            or p == "/ml-limits"):
            if not self._require_auth():
                return

        if self.service == "campaign" and p == "/reference/segments":
            return self._send(200, SEGMENTS_REF)
        if self.service == "campaign" and p == "/reference/mcc-categories":
            return self._send(200, MCC_REF)
        if self.service == "campaign" and p == "/ml/customers":
            return self._send(200, ML_CUSTOMERS)

        if self.service == "campaign" and p == "/ml-limits":
            return self._send(200, ML_LIMITS)

        if self.service == "campaign" and p == "/analytics/kpis":
            seg = (q.get("segment_id") or [None])[0]
            # Сегмент-фильтр сужает охват — цифры меняются (фаза 24).
            return self._send(200, {**KPIS, "reach": 210000 if seg else KPIS["reach"]})
        if self.service == "campaign" and p == "/analytics/daily-trend":
            return self._send(200, {
                "campaign_id": (q.get("campaign_id") or [None])[0],
                "period_days": int((q.get("period") or ["30"])[0]),
                "points": TREND_POINTS,
            })
        if self.service == "campaign" and p == "/analytics/channels":
            return self._send(200, CHANNELS)
        if self.service == "campaign" and p == "/experiments":
            return self._send(200, EXPERIMENTS)
        m = re.fullmatch(r"/experiments/([0-9a-f-]{36})/results", p)
        if self.service == "campaign" and m:
            exp = next((e for e in EXPERIMENTS if e["experiment_id"] == m.group(1)), None)
            if exp is None:
                return self._send(404, {"detail": "experiment not found"})
            return self._send(200, experiment_results(exp))

        if self.service == "recommendation":
            m = re.fullmatch(r"/recommendations/([^/]+)", p)
            if m:
                if m.group(1) == "00000000-0000-0000-0000-000000000000":
                    return self._send(404, {"detail": f"user '{m.group(1)}' not found in the feature store"})
                resp = dict(RECOMMENDATION, user_id=m.group(1))
                return self._send(200, resp)
            return self._send(404, {"detail": "not found"})

        # campaign manager
        if p == "/campaigns":
            status = (q.get("status") or [None])[0]
            items = [c for c in CAMPAIGNS if not status or c["status"] == status]
            return self._send(200, items)
        if p == "/campaigns/active":
            return self._send(200, [c for c in CAMPAIGNS if c["status"] == "ACTIVE"])
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})/stats", p)
        if m:
            s = STATS.get(m.group(1))
            return self._send(200, s) if s else self._send(404, {"detail": "campaign not found"})
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

    def do_POST(self):
        u = urlparse(self.path)
        if self.service == "campaign" and u.path == "/auth/login":
            body = self._read_body()
            user = next((x for x in ADMIN_USERS
                         if x["email"] == body.get("email")
                         and x["_password"] == body.get("password")
                         and x["is_active"]), None)
            if user is None:
                return self._send(401, {"detail": "invalid email or password"})
            user["last_login_at"] = iso(NOW)
            return self._send(200, {
                "access_token": f"stub-access-{user['email']}",
                "refresh_token": f"stub-refresh-{user['email']}",
                "token_type": "bearer",
            })
        if self.service == "campaign" and u.path == "/auth/refresh":
            body = self._read_body()
            token = str(body.get("refresh_token") or "")
            if not token.startswith("stub-refresh-"):
                return self._send(401, {"detail": "invalid token"})
            email = token.removeprefix("stub-refresh-")
            return self._send(200, {
                "access_token": f"stub-access-{email}",
                "refresh_token": token,
                "token_type": "bearer",
            })
        if self.service == "campaign" and u.path == "/auth/users":
            if not self._require_auth():
                return
            body = self._read_body()
            user = {"user_id": str(uuid.uuid4()), "email": body["email"],
                    "full_name": body["full_name"], "role": body.get("role", "ANALYST"),
                    "is_active": True, "created_at": iso(NOW), "last_login_at": None,
                    "_password": body.get("password", "x")}
            ADMIN_USERS.append(user)
            return self._send(201, public_user(user))
        if self.service == "campaign" and u.path.startswith("/campaigns"):
            if not self._require_auth():
                return
        if self.service == "campaign" and u.path == "/campaigns":
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
        return self._send(404, {"detail": "not found"})

    def do_PUT(self):
        u = urlparse(self.path)
        if self.service == "campaign" and u.path == "/ml-limits":
            if not self._require_auth():
                return
            body = self._read_body()
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
            ML_LIMITS["updated_at"] = iso(NOW)
            return self._send(200, ML_LIMITS)
        return self._send(404, {"detail": "not found"})

    def do_PATCH(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if self.service == "campaign":
            # Фаза 26: PATCH /roles/{role}/permissions (только ADMIN).
            mrole = re.fullmatch(r"/roles/(ADMIN|MARKETER|ANALYST)/permissions", u.path)
            if mrole:
                actor = self._auth_user()
                if actor is None:
                    return self._send(401, {"detail": "authentication required"})
                if actor.get("role") != "ADMIN":
                    return self._send(403, {"detail": "role is not allowed"})
                role = mrole.group(1)
                if role == "ADMIN":
                    return self._send(403, {"detail": "ADMIN cannot be modified"})
                body = self._read_body()
                ROLE_PERMISSIONS[role].update(
                    {k: bool(v) for k, v in (body.get("permissions") or {}).items()})
                return self._send(200, ROLE_PERMISSIONS[role])
            m = re.fullmatch(r"/auth/users/([0-9a-f-]{36})", u.path)
            if m:
                if not self._require_auth():
                    return
                user = next((x for x in ADMIN_USERS if x["user_id"] == m.group(1)), None)
                if user is None:
                    return self._send(404, {"detail": "user not found"})
                body = self._read_body()
                for k in ("full_name", "role", "is_active"):
                    if k in body and body[k] is not None:
                        user[k] = body[k]
                return self._send(200, public_user(user))
            if u.path.startswith("/campaigns") and not self._require_auth():
                return
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})/status", u.path)
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
            prev, c["status"] = c["status"], new
            return self._send(200, {
                "campaign_id": c["campaign_id"], "previous": prev,
                "current": new, "action": action,
            })
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})", u.path)
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

    def do_DELETE(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/campaigns/([0-9a-f-]{36})", u.path)
        if self.service == "campaign" and m:
            user = self._auth_user()
            if user is None:
                return self._send(401, {"detail": "authentication required"})
            # Фаза 23: удаление — только ADMIN.
            if user.get("role") != "ADMIN":
                return self._send(403, {"detail": "role is not allowed"})
            c = next((x for x in CAMPAIGNS if x["campaign_id"] == m.group(1)), None)
            if not c:
                return self._send(404, {"detail": "campaign not found"})
            if c["status"] == "ACTIVE":
                return self._send(409, {"detail": "cannot delete an ACTIVE campaign"})
            CAMPAIGNS.remove(c)
            STATS.pop(c["campaign_id"], None)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._send(404, {"detail": "not found"})


class RecHandler(Handler):
    service = "recommendation"


def serve(port, handler):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve(8002, Handler)
    serve(8001, RecHandler)
    print("stub backends on :8001 (rec) and :8002 (campaign)", flush=True)
    threading.Event().wait()
