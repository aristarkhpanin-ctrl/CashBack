"""E2E: Полный цикл персонализированного кэшбэка (chapter 3.3, 9 шагов + этап C).

Runs against a *live* stack: ``make env`` + ``make up`` + ``make migrate``,
with a ranking model loaded in recommendation-api (``/health/ready``).
Marked ``e2e`` so the regular test runs ignore it.

Credentials come from the environment or the repo-root ``.env`` written by
``make env`` (stage B, ADR-0013), like docker compose reads them: the admin
logs in with ``ADMIN_BOOTSTRAP_PASSWORD`` (``E2E_ADMIN_PASSWORD`` overrides
it), the customer's token is minted with ``MOBILE_JWT_SECRET`` by the demo
IdP (``scripts/mint_customer_token.py``, via ``tests/auth_tokens.py``).

The test is skipped only when the stack is not running at all (none of the
CashBack services answers). A partially running stack, or any step that does not
behave — including no accrual within ``E2E_ACCRUAL_TIMEOUT_S`` — fails:
a money-path test that can end in ``skip`` never turns red.

Steps (per the dissertation; stage C money rules — ADR-0014):

    1. Create test user (Postgres) with a GRANTED ``personalised_cashback``
       consent (eligible_campaigns requires the latest one) + ACTIVE campaign
       via campaign_manager (8002): minimum 100 ₽ per purchase, 1000 ₽ per
       category (the category's minimum wins, B21)
    2. Insert 150 synthetic transactions across 4 MCCs / 90 days into ClickHouse
    3. Seed the user's RFM features in Redis (shortcut for the Airflow DAG)
    4. GET /v1/mobile/recommendations/{user} with the customer's token —
       assert mobile-shape payload (cashback_rate, deeplink, terms_summary;
       NO model_score) and an offer of the test campaign. Before it: the
       admin's ML explanation (campaign_manager GET /ml/explain/{user} →
       rec_api with a service token) answers and writes nothing — no
       recommendations row, no BRE keys (B27); the mobile API refuses the
       request without a token (401) and with another customer's (403, B12)
    5. POST /respond {ACCEPTED} — another customer cannot answer the offer
       (404, it stays PENDING); the owner's accept SETEXes accepted_offers:*
       in Redis; a repeated response is refused with 409
    6. Publish to Kafka ``transactions.raw`` as Avro via Schema Registry —
       exactly like tx_simulator (the listener reads nothing else): first a
       500 ₽ purchase (below the category minimum), then the 1500 ₽ one
    7. Wait for transaction_listener to accrue *this* transaction for *this*
       campaign
    8. Assert the cashback_accruals row, the cashback amount and its
       recommendation_id; the purchase below the category minimum (same
       partition, processed before) paid nothing and did not spend the offer
    9. Assert budget_spent incremented & accepted_offers key deleted
   10. The offer pays once (B45): the key comes back as if the listener's DEL
       had been lost, and a SECOND qualifying purchase on the same offer pays
       nothing — exactly one accrual for the recommendation_id, the budget is
       debited once, and the listener removes the leftover key
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.auth_tokens import bearer, customer_token, env_value
from tests.e2e.avro_producer import publish_transactions, transaction_event

# pytest-timeout registers the marker itself (--strict-markers).
_TIMEOUT = [pytest.mark.timeout(360)] if importlib.util.find_spec("pytest_timeout") else []
pytestmark = [pytest.mark.e2e, *_TIMEOUT]


# ---------------------------------------------------------------------------
# URLs (override via environment when running against a non-local stack).
# Defaults are the host ports published by docker-compose.yml.
# ---------------------------------------------------------------------------
CAMPAIGN_API    = os.getenv("E2E_CAMPAIGN_API",    "http://localhost:8002")
RECO_API        = os.getenv("E2E_RECO_API",        "http://localhost:8001")
MOBILE_API      = os.getenv("E2E_MOBILE_API",      "http://localhost:8003")
POSTGRES_DSN    = os.getenv("E2E_POSTGRES_DSN",
                            "postgresql://cashback:cashback@localhost:5432/cashback")
REDIS_URL       = os.getenv("E2E_REDIS_URL",       "redis://localhost:6379/0")
CLICKHOUSE_URL  = os.getenv("E2E_CLICKHOUSE_URL",  "http://localhost:8123")
CLICKHOUSE_USER = os.getenv("E2E_CLICKHOUSE_USER", "cashback")
CLICKHOUSE_PASSWORD = os.getenv("E2E_CLICKHOUSE_PASSWORD", "cashback")
CLICKHOUSE_DB   = os.getenv("E2E_CLICKHOUSE_DB",   "cashback")
# EXTERNAL listener of the compose broker (INTERNAL kafka:9092 is in-network only).
KAFKA_BS        = os.getenv("E2E_KAFKA_BS",        "localhost:9094")
SCHEMA_REGISTRY_URL = os.getenv("E2E_SCHEMA_REGISTRY_URL", "http://localhost:8081")
TX_TOPIC        = os.getenv("E2E_TX_TOPIC",        "transactions.raw")
ACCRUAL_TIMEOUT_S = float(os.getenv("E2E_ACCRUAL_TIMEOUT_S", "30"))
# recommendation-api polls MLflow for the Production model every
# MODEL_POLL_INTERVAL_SECONDS (60 in compose): a model promoted right
# before the test needs up to that long to be loaded.
READY_TIMEOUT_S = float(os.getenv("E2E_READY_TIMEOUT_S", "120"))

# Cold-start users get recommendation_api's POPULAR_FALLBACK candidates
# (app/candidate_gen.py). The test campaign covers all of them. rec_api joins
# each candidate to its best eligible campaign (eligible_campaigns order:
# rate, then the later start): the test campaign starts later than the seeded
# ones, so it wins every MCC except those where a seeded campaign pays more
# than 5 % (5812, 5912, 7011 — when it targets the user's segment). The other
# 17 MCCs belong to the test campaign: its offer is among the top 5.
CAMPAIGN_MCCS = [
    "5411", "5812", "5541", "5912", "5732", "5311", "5814", "5651",
    "5921", "5942", "4111", "4121", "5462", "5499", "7011", "7832",
    "5611", "5641", "5722", "5712",
]
CASHBACK_RATE = Decimal("5.0")
PURCHASE_AMOUNT = Decimal("1500.00")
# Per-category minimum of every campaign MCC (the campaign's own is 100 ₽):
# a purchase between the two must not be paid (B21).
CATEGORY_MIN_AMOUNT = Decimal("1000.00")
BELOW_CATEGORY_MIN = Decimal("500.00")


# ---------------------------------------------------------------------------
# Stack probe — stdlib only, so "stack not running" is decided before any
# third-party import.
# ---------------------------------------------------------------------------
def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _tcp_ok(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _stack_endpoints() -> dict[str, bool]:
    pg = urllib.parse.urlsplit(POSTGRES_DSN)
    redis_url = urllib.parse.urlsplit(REDIS_URL)
    kafka_host, _, kafka_port = KAFKA_BS.split(",")[0].rpartition(":")
    return {
        "campaign-manager": _http_ok(f"{CAMPAIGN_API}/health/live"),
        "recommendation-api": _http_ok(f"{RECO_API}/health/live"),
        "mobile-api": _http_ok(f"{MOBILE_API}/health/live"),
        "clickhouse": _http_ok(f"{CLICKHOUSE_URL}/ping"),
        "schema-registry": _http_ok(f"{SCHEMA_REGISTRY_URL}/subjects"),
        "kafka": _tcp_ok(kafka_host or "localhost", int(kafka_port)),
        "postgres": _tcp_ok(pg.hostname or "localhost", pg.port or 5432),
        "redis": _tcp_ok(redis_url.hostname or "localhost", redis_url.port or 6379),
    }


# The stack "is running" when any CashBack service answers; Postgres /
# Redis ports alone may belong to something else on a developer machine.
_APP_SERVICES = ("campaign-manager", "recommendation-api", "mobile-api")


def _require_stack() -> None:
    state = _stack_endpoints()
    if not any(state[name] for name in _APP_SERVICES):
        pytest.skip("E2E stack not running — `make up` first")
    down = sorted(name for name, ok in state.items() if not ok)
    if down:
        pytest.fail(f"E2E stack is only partially up, not reachable: {down}",
                    pytrace=False)


# ---------------------------------------------------------------------------
async def _wait_for(probe, timeout: float, interval: float = 0.2) -> Any:
    """Poll ``probe()`` until it returns something truthy or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result or time.monotonic() >= deadline:
            return result
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_full_cashback_cycle():
    _require_stack()

    import httpx
    import psycopg2
    import redis

    test_started = time.monotonic()
    user_id = str(uuid.uuid4())

    async with httpx.AsyncClient(timeout=10) as http:
        # recommendation-api answers /health/live without a model; without
        # one step 4 can only end in 502 — wait for readiness, then say so.
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            ready = await http.get(f"{RECO_API}/health/ready")
            if ready.status_code == 200 or time.monotonic() >= deadline:
                break
            await asyncio.sleep(2)
        assert ready.status_code == 200, (
            f"recommendation-api is up but not ready within {READY_TIMEOUT_S:.0f} s "
            f"(ranking model in MLflow 'Production'?): {ready.status_code} {ready.text}"
        )

        # The bootstrap admin of migration 002: its password is the one
        # `make env` wrote to .env. Without it the test cannot log in.
        admin_password = (os.getenv("E2E_ADMIN_PASSWORD")
                          or env_value("ADMIN_BOOTSTRAP_PASSWORD"))
        if not admin_password:
            pytest.fail("no admin password: set ADMIN_BOOTSTRAP_PASSWORD (`make env` "
                        "writes it to .env before `make migrate`) or E2E_ADMIN_PASSWORD",
                        pytrace=False)
        # The customer's token from the demo IdP; a second customer's token
        # for the B12 checks.
        customer = bearer(customer_token(user_id))
        stranger = bearer(customer_token(uuid.uuid4()))

        pg = psycopg2.connect(POSTGRES_DSN)
        pg.autocommit = True
        cur = pg.cursor()
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        auth: dict[str, str] = {}
        campaign_id: str | None = None
        try:
            # ----------------------------------------------------------
            # Step 1 — create user (raw SQL for determinism) + campaign
            # ----------------------------------------------------------
            cur.execute(
                "INSERT INTO users (user_id, external_id, segment_id) "
                "VALUES (%s, %s, 5)",
                (user_id, f"e2e-{user_id}"),
            )
            # Stage C: offers and accruals need the customer's latest
            # personalised_cashback consent to be GRANTED (eligible_campaigns).
            cur.execute(
                "INSERT INTO user_consents (user_id, consent_type, status, "
                "document_version) VALUES (%s, 'personalised_cashback', 'GRANTED', 'v1')",
                (user_id,),
            )

            # Мутации campaign_manager требуют JWT с правами на кампании.
            # Bootstrap-администратор создаётся миграцией 002 с паролем
            # ADMIN_BOOTSTRAP_PASSWORD; пароль, сгенерированный миграцией,
            # требует смены при первом входе — тогда токен годится только
            # для /auth/change-password, и тест останавливается здесь.
            login = await http.post(f"{CAMPAIGN_API}/auth/login", json={
                "email": os.getenv("E2E_ADMIN_EMAIL", "admin@bank.ru"),
                "password": admin_password,
            })
            assert login.status_code == 200, login.text
            assert not login.json()["must_change_password"], (
                "the admin must change the bootstrap password first: the database "
                "was migrated without ADMIN_BOOTSTRAP_PASSWORD (README → «Быстрый старт»)"
            )
            auth = bearer(login.json()["access_token"])

            now = datetime.now(UTC)
            resp = await http.post(f"{CAMPAIGN_API}/campaigns", headers=auth, json={
                "name": f"E2E Test {uuid.uuid4().hex[:6]}",
                "target_segment_ids": [5],
                "cashback_rate": str(CASHBACK_RATE),
                "min_transaction_amount": "100.0",
                "budget_total": "100000",
                "start_date": (now - timedelta(hours=1)).isoformat(),
                "end_date": (now + timedelta(days=30)).isoformat(),
                "allowed_channels": ["POS", "ONLINE", "MOBILE"],
                "mcc_codes": CAMPAIGN_MCCS,
                "min_tx_amounts": {mcc: str(CATEGORY_MIN_AMOUNT) for mcc in CAMPAIGN_MCCS},
            })
            assert resp.status_code == 201, resp.text
            campaign_id = resp.json()["campaign_id"]

            act = await http.patch(
                f"{CAMPAIGN_API}/campaigns/{campaign_id}/status",
                params={"action": "activate"}, headers=auth,
            )
            assert act.status_code == 200, act.text
            assert act.json()["current"] == "ACTIVE"

            # ----------------------------------------------------------
            # Step 2 — 150 synthetic transactions across 4 MCCs / 90 days
            # ----------------------------------------------------------
            rows: list[str] = []
            history_mccs = ["5411", "5812", "5541", "5912"]
            base = now - timedelta(days=90)
            for i in range(150):
                ts = (base + timedelta(minutes=i * 8)).strftime("%Y-%m-%d %H:%M:%S")
                amt = 100 + (i * 13) % 1500
                rows.append(
                    f"('tx-e2e-hist-{user_id[:8]}-{i:04d}','{user_id}','{ts}',"
                    f"{amt:.2f},'RUB','{history_mccs[i % 4]}','m-{i % 7}','M{i}',"
                    f"'POS','RU','RU')"
                )
            ch_headers = {"X-ClickHouse-User": CLICKHOUSE_USER,
                          "X-ClickHouse-Key": CLICKHOUSE_PASSWORD}
            ch = await http.post(
                f"{CLICKHOUSE_URL}/", params={"database": CLICKHOUSE_DB},
                headers=ch_headers,
                content=("INSERT INTO transactions_raw "
                         "(transaction_id,user_id,transaction_date,amount,currency,"
                         "mcc_code,merchant_id,merchant_name,channel,city,country) "
                         "VALUES " + ",".join(rows)),
            )
            assert ch.status_code == 200, ch.text
            ch = await http.post(
                f"{CLICKHOUSE_URL}/", params={"database": CLICKHOUSE_DB},
                headers=ch_headers,
                content=f"SELECT count() FROM transactions_raw WHERE user_id = '{user_id}'",
            )
            assert ch.status_code == 200, ch.text
            assert ch.text.strip() == "150"

            # ----------------------------------------------------------
            # Step 3 — refresh RFM features. A real Airflow run is too slow;
            # write the feature payload rec_api's FeatureStore reads.
            # ----------------------------------------------------------
            r.set(
                f"features:{user_id}",
                json.dumps({
                    "user_id": user_id,
                    "segment_id": 5,
                    "recency_days": 1,
                    "frequency_total": 150,
                    "monetary_total": 75000.0,
                    "avg_ticket": 500.0,
                    "weekend_ratio": 0.4,
                    "evening_ratio": 0.6,
                }),
            )

            # ----------------------------------------------------------
            # Step 4a — the admin's ML explanation is read-only (B27):
            # campaign_manager → rec_api /explain with a service token;
            # nothing is served, so nothing is recorded.
            # ----------------------------------------------------------
            explain = await http.get(f"{CAMPAIGN_API}/ml/explain/{user_id}",
                                     params={"top_k": 5}, headers=auth)
            assert explain.status_code == 200, explain.text
            assert explain.json()["user_id"] == user_id
            assert explain.json()["recommendations"], explain.text
            cur.execute("SELECT count(*) FROM recommendations WHERE user_id = %s",
                        (user_id,))
            assert cur.fetchone()[0] == 0, "explain recorded a recommendation"
            bre_keys = [f"rec_count:{user_id}",
                        *r.scan_iter(match=f"recent_offer:{user_id}:*")]
            assert not any(r.exists(key) for key in bre_keys), bre_keys

            # ----------------------------------------------------------
            # Step 4 — fetch mobile recommendations + assert mobile shape.
            # Only the customer's own token opens them (B12).
            # ----------------------------------------------------------
            recs_url = f"{MOBILE_API}/v1/mobile/recommendations/{user_id}"
            anonymous = await http.get(recs_url, params={"top_k": 5})
            assert anonymous.status_code == 401, anonymous.text
            foreign = await http.get(recs_url, params={"top_k": 5}, headers=stranger)
            assert foreign.status_code == 403, foreign.text

            resp = await http.get(recs_url, params={"top_k": 5}, headers=customer)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["user_id"] == user_id
            items = body["recommendations"]
            assert items, f"no recommendations for the e2e user: {body}"
            # Mobile-shape: trim model internals, enrich for the UI.
            for item in items:
                assert "model_score" not in item
                assert "top_factors" not in item
                assert "score" not in item
                assert "category_name" in item
                assert item["cashback_rate"].endswith("%")
                assert item["deeplink"].startswith("cashback://offer/")
                assert len(item["terms_summary"]) <= 140

            offers = [i for i in items if i.get("campaign_id") == campaign_id]
            assert offers, (
                f"no offer of the e2e campaign {campaign_id} among the "
                f"recommendations: {items}"
            )
            offer = offers[0]
            rec_id, mcc = offer["recommendation_id"], offer["mcc_code"]
            assert rec_id is not None, offer
            assert offer["cashback_rate"] == "5%"
            assert offer["deeplink"] == f"cashback://offer/{rec_id}"

            # ----------------------------------------------------------
            # Step 5 — accept the offer and assert the Redis hot key
            # ----------------------------------------------------------
            respond_url = f"{MOBILE_API}/v1/mobile/recommendations/{rec_id}/respond"
            # Another customer's accept looks like an unknown offer (no
            # existence leak) and does not touch it.
            hijack = await http.post(respond_url, json={"action": "ACCEPTED"},
                                     headers=stranger)
            assert hijack.status_code == 404, hijack.text
            cur.execute("SELECT response_status::text FROM recommendations "
                        "WHERE recommendation_id = %s", (rec_id,))
            assert cur.fetchone()[0] == "PENDING"

            accept = await http.post(respond_url, json={"action": "ACCEPTED"},
                                     headers=customer)
            assert accept.status_code == 200, accept.text
            hot_key = accept.json()["accepted_offer_key"]
            assert hot_key == f"accepted_offers:{user_id}:{mcc}"

            raw = r.get(hot_key)
            assert raw is not None
            hot = json.loads(raw)
            assert hot["campaign_id"] == campaign_id
            assert hot["recommendation_id"] == rec_id
            assert r.ttl(hot_key) >= 60

            # The offer is answered once: a repeat cannot re-arm it (B13).
            again = await http.post(respond_url, json={"action": "ACCEPTED"},
                                    headers=customer)
            assert again.status_code == 409, again.text

            # ----------------------------------------------------------
            # Step 6 — the purchases: Avro TransactionEvents via Schema
            # Registry. First one below the category minimum, then the
            # qualifying one; separate flushes keep them in this order on
            # the user's partition (key = user_id).
            # ----------------------------------------------------------
            def _purchase(amount: Decimal) -> dict[str, Any]:
                return transaction_event(
                    transaction_id=f"tx-e2e-{uuid.uuid4().hex}",
                    user_id=user_id, mcc_code=mcc,
                    amount=str(amount), channel="POS", merchant_id="M-E2E",
                )

            async def _publish(event: dict[str, Any]) -> None:
                await asyncio.to_thread(
                    publish_transactions, KAFKA_BS, SCHEMA_REGISTRY_URL, TX_TOPIC,
                    [event],
                )

            small = _purchase(BELOW_CATEGORY_MIN)
            purchase = _purchase(PURCHASE_AMOUNT)
            await _publish(small)
            await _publish(purchase)

            # ----------------------------------------------------------
            # Step 7 — wait for the listener to accrue THIS transaction
            # ----------------------------------------------------------
            def _accrual_of(event: dict[str, Any]):
                cur.execute(
                    """SELECT user_id::text, mcc_code, transaction_amount,
                              cashback_amount, status::text, recommendation_id::text
                         FROM cashback_accruals
                        WHERE transaction_id = %s AND campaign_id = %s""",
                    (event["transaction_id"], campaign_id),
                )
                return cur.fetchone()

            accrual = await _wait_for(lambda: _accrual_of(purchase), ACCRUAL_TIMEOUT_S)
            if accrual is None:
                paid_small = _accrual_of(small) is not None
                pytest.fail(
                    f"transaction_listener did not accrue {purchase['transaction_id']} "
                    f"(campaign {campaign_id}) within {ACCRUAL_TIMEOUT_S:.0f} s; "
                    f"offer key still in Redis: {r.exists(hot_key) == 1}; the "
                    f"{BELOW_CATEGORY_MIN} purchase below the category minimum was "
                    f"{'PAID and spent the offer (B21)' if paid_small else 'not paid'}. "
                    "See `docker compose logs transaction-listener`.",
                    pytrace=False,
                )

            # ----------------------------------------------------------
            # Step 8 — the accrual row: 5 % of 1500 (calculate_cashback),
            # tied to the accepted offer; the purchase below the category
            # minimum (processed before it) paid nothing
            # ----------------------------------------------------------
            expected = (PURCHASE_AMOUNT * CASHBACK_RATE / 100).quantize(Decimal("0.01"))
            acc_user, acc_mcc, acc_amount, acc_cashback, acc_status, acc_rec = accrual
            assert acc_user == user_id
            assert acc_mcc.strip() == mcc
            assert Decimal(acc_amount) == PURCHASE_AMOUNT
            assert Decimal(acc_cashback) == expected
            assert acc_status == "PENDING"
            assert acc_rec == rec_id
            assert _accrual_of(small) is None, "paid below the category minimum (B21)"

            # ----------------------------------------------------------
            # Step 9 — budget debited once; the offer key is consumed
            # ----------------------------------------------------------
            def _budget_spent() -> Decimal:
                cur.execute(
                    "SELECT budget_spent FROM cashback_campaigns WHERE campaign_id = %s",
                    (campaign_id,),
                )
                return Decimal(cur.fetchone()[0])

            assert _budget_spent() == expected
            # The listener deletes the key right after its PG commit.
            assert await _wait_for(lambda: r.exists(hot_key) == 0, 5.0), \
                f"{hot_key} still present after the accrual"

            # ----------------------------------------------------------
            # Step 10 — the offer pays once (B45). The listener's DEL is
            # lost (it died between the PG commit and Redis): the key holds
            # the accepted offer again. A second qualifying purchase on it
            # must not be paid; the listener then removes the leftover key,
            # which is also how this test knows it was processed.
            # ----------------------------------------------------------
            r.set(hot_key, raw, ex=300)
            second = _purchase(PURCHASE_AMOUNT)
            await _publish(second)
            if not await _wait_for(lambda: r.exists(hot_key) == 0, ACCRUAL_TIMEOUT_S):
                pytest.fail(
                    f"transaction_listener did not process the second purchase "
                    f"{second['transaction_id']} within {ACCRUAL_TIMEOUT_S:.0f} s "
                    f"(the re-armed {hot_key} is still in Redis; paid: "
                    f"{_accrual_of(second) is not None})",
                    pytrace=False,
                )
            assert _accrual_of(second) is None, "the same offer was paid twice (B45)"
            cur.execute("SELECT count(*) FROM cashback_accruals WHERE recommendation_id = %s",
                        (rec_id,))
            assert cur.fetchone()[0] == 1
            assert _budget_spent() == expected
        finally:
            # A leftover ACTIVE test campaign would take over every popular
            # MCC in the stack's recommendations — complete it.
            if campaign_id is not None and auth:
                await http.patch(
                    f"{CAMPAIGN_API}/campaigns/{campaign_id}/status",
                    params={"action": "complete"}, headers=auth,
                )
            r.close()
            pg.close()

    elapsed = time.monotonic() - test_started
    print(f"E2E completed in {elapsed:.2f}s")
