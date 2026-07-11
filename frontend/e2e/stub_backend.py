#!/usr/bin/env python3
"""Stub FastAPI-shaped backends on :8001 (recommendation) and :8002 (campaign).

Mirrors the exact JSON wire format of the real services (Decimal → strings,
UUID ids, ISO datetimes) so the frontend live-mode wiring can be verified
without the docker stack.
"""
import json
import re
import threading
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
        },
        {"mcc_code": "5912", "score": 0.61, "campaign_id": None,
         "top_factors": {"mcc_5912_cnt_90d": 0.12, "frequency_total": 0.06}},
        {"mcc_code": "5541", "score": 0.44, "campaign_id": None,
         "top_factors": {"mcc_5541_cnt_90d": 0.09}},
    ],
    "model_version": "3",
    "candidates_considered": 24,
}

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

        if self.service == "campaign" and p == "/auth/me":
            user = self._auth_user()
            if user is None:
                return self._send(401, {"detail": "authentication required"})
            return self._send(200, public_user(user))

        if self.service == "campaign" and p == "/auth/users":
            if not self._require_auth():
                return
            return self._send(200, [public_user(x) for x in ADMIN_USERS])

        if self.service == "campaign" and (p.startswith("/campaigns")
                                            or p.startswith("/analytics")
                                            or p.startswith("/experiments")):
            if not self._require_auth():
                return

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
            return self._send(200, {
                "campaign_id": (q.get("campaign_id") or [None])[0],
                "period_days": int((q.get("period") or ["30"])[0]),
                "steps": FUNNEL_STEPS,
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
            }
            CAMPAIGNS.append(c)
            STATS[c["campaign_id"]] = {
                "campaign_id": c["campaign_id"], "impressions": 0, "clicks": 0,
                "accepted": 0, "transactions": 0, "revenue": "0", "cashback_paid": "0",
                "ctr": 0.0, "conversion_rate": 0.0, "roi": 0.0,
            }
            return self._send(201, c)
        return self._send(404, {"detail": "not found"})

    def do_PATCH(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if self.service == "campaign":
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
                      "start_date", "end_date"):
                if k in body and body[k] is not None:
                    c[k] = body[k]
            for k in ("cashback_rate", "min_transaction_amount", "budget_total"):
                if k in body and body[k] is not None:
                    c[k] = str(body[k])
            return self._send(200, c)
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
