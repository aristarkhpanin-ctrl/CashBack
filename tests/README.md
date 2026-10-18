# Testing pyramid

CashBack ships with a four-layer test pyramid (chapter 3.3, table 23).

| Layer        | Where                                | Run with             |
|--------------|--------------------------------------|----------------------|
| Unit         | `services/*/tests/unit/`             | `make test-unit`     |
| Integration  | `tests/integration/`                 | `make test-integration` |
| E2E          | `tests/e2e/`                         | `make test-e2e`      |
| Load         | `tests/load/locustfile.py`           | `make test-load`     |

## Unit (≥85 % coverage per service)

Each service ships its own `pyproject.toml` with a `[tool.coverage.run]`
block (`omit = ["tests/*", "alembic/*", ...]`) and a
`[tool.coverage.report] fail_under = 85`. Coverage is enforced by
`pytest-cov`, so a regression below the threshold fails the run.

```bash
make test-unit                                   # all services
cd services/recommendation_api && pytest -m "not integration" --cov  # one service
```

## Integration (tests/integration, runs in CI)

Repo-level tests under `tests/integration/`. Real testcontainers (Postgres
migrated to alembic head, Redis, ClickHouse with its SQL migrations, Kafka
in KRaft mode, Schema Registry) drive the IO-bound tests; in-memory stubs
cover the remaining flows.

| File                                  | Tests | Category                      |
|---------------------------------------|-------|-------------------------------|
| `test_kafka_loading.py`               |   7   | ETL staging → ClickHouse      |
| `test_listener_avro_accrual.py`       |   3   | Avro transaction → listener → accrual: the offer pays once (lost DEL), category minimum, JSON → DLQ |
| `test_feature_store.py`               |   6   | Redis FeatureStore CRUD       |
| `test_campaigns_crud.py`              |   8   | Campaign CRUD/filter (real schema) |
| `test_bre_rules.py`                   |  20   | BRE 6 rules (R6 read-only, daily limit) |
| `test_notifications.py`               |   5   | Notification 4 channels       |
| `test_recommendation_flow.py`         |   9   | Recommendation API: service tokens only, issuer per route |
| `test_seed_demo_data.py`              |  22   | `scripts/seed_demo_data.py` (+ demo-login passwords, B20; stage C: `rfm_score`, offers only where eligible, ledger by the accrual-engine rules) |
| `test_stack_secrets.py`               |  12   | `make env`, compose secrets per service, `init_db.sh` → migrations |
| `test_helm_chart.py`                  |   7   | Helm chart renders with required secrets |

```bash
pip install -r services/recommendation_api/requirements.lock
pip install -e services/recommendation_api -e services/transaction_listener -e services/etl
pip install -r tests/requirements-integration.txt
make test-integration         # needs Docker only, not the running stack
```

Every module runs in its own pytest process (each service ships its own
top-level `app` package); the target runs all of them and fails at the end
if any failed.

Retries, the DLQ, a Postgres outage mid-batch and the daily limit under
concurrent purchases are proven by the listener's own suite
(`services/transaction_listener/tests/integration`, CI job
`service-integration`); the SQL money rules of migration 009 by
`db_migrations/tests` (same job). This suite keeps the money assertions of
the e2e test that CI can run without the full stack.

The `tests/conftest.py` exposes session-scoped `postgres_container`,
`redis_container`, `clickhouse_container`, `kafka_container`,
`schema_registry_container` fixtures (plus `migrated_postgres_dsn`,
`migrated_clickhouse_url`, `kafka_bootstrap`, `schema_registry_url`).
Without Docker they skip locally; under CI (`CI=true`) they fail — an
integration job whose tests were all skipped verified nothing. The same
rule applies to host tools (`require_tool`: `make`, `openssl`, `docker
compose` for `test_stack_secrets.py`).

Keys (stage B, `docs/adr/0013-auth-hardening.md`): the tests need no `.env`.
`test_recommendation_flow.py` uses `SERVICE_JWT_SECRET` / `JWT_SECRET` /
`MOBILE_JWT_SECRET` from the environment when set (CI generates a random set
per run) and fixed test values otherwise; `test_stack_secrets.py` strips them
from the environment of the `make` / `docker compose` it runs.

## E2E (1 test)

`tests/e2e/test_full_cashback_cycle.py` runs the 9-step "полный цикл
персонализированного кэшбэка" against the live stack, plus step 10 of
stage C (the money rules, `docs/adr/0014-money-integrity.md`):

1. create test user with a GRANTED `personalised_cashback` consent +
   ACTIVE campaign (campaign_manager, admin JWT): minimum 100 ₽, 1000 ₽ per
   category
2. insert 150 synthetic transactions / 4 MCC / 90 days into ClickHouse
3. seed RFM features in Redis (Airflow alternative)
4. the admin's ML explanation (`GET /ml/explain/{user}` on campaign_manager →
   rec_api `/explain` with a service token) answers and records nothing — no
   `recommendations` row, no `rec_count:*` / `recent_offer:*` keys (B27);
   `GET /v1/mobile/recommendations/{user}` → 401 without a token, 403 with
   another customer's token (B12), and with the customer's own token →
   assert *mobile shape* (`cashback_rate`, `deeplink`, `terms_summary`;
   **no** `model_score`) and an offer of the test campaign
5. `POST /respond {ACCEPTED}` with another customer's token → 404 and the
   offer stays `PENDING`; with the owner's token → assert
   `accepted_offers:*` SETEX; a repeated response → 409
6. publish to `transactions.raw` as **Avro via Schema Registry**, exactly
   like tx_simulator (`tests/e2e/avro_producer.py`) — the listener decodes
   nothing else: a 500 ₽ purchase (below the category minimum), then 1500 ₽
7. wait ≤ `E2E_ACCRUAL_TIMEOUT_S` (30 s) for the accrual of *this*
   transaction for *this* campaign (no accrual → the test fails)
8. assert `cashback_accruals` row + correct amount + its `recommendation_id`;
   the 500 ₽ purchase paid nothing (B21)
9. assert `budget_spent` incremented + Redis hot-key removed
10. the offer pays once (B45): the hot key is put back as if the listener's
    `DEL` had been lost, a second 1500 ₽ purchase → no second accrual for the
    `recommendation_id`, `budget_spent` unchanged, the leftover key removed

The test campaign is completed at the end. It is skipped only when no
CashBack service answers (stack not running); a partially running stack,
recommendation-api not ready within `E2E_READY_TIMEOUT_S` (120 s — no
Production model in MLflow), or a missing accrual fail.

```bash
make env && make up && make migrate   # + a Production model in MLflow (make train-models)
pip install -r tests/requirements-integration.txt
make test-e2e
```

Credentials come from the environment, else the repo-root `.env` of
`make env` (`tests/auth_tokens.py` — the same way docker compose reads it):
the admin logs in with `ADMIN_BOOTSTRAP_PASSWORD` (`E2E_ADMIN_PASSWORD`
overrides it; a password the migration generated must be changed in the UI
first — the test says so), customer tokens are minted with
`MOBILE_JWT_SECRET` / `MOBILE_JWT_ISSUER` by the demo IdP
(`scripts/mint_customer_token.py`).

Override targets via env (`E2E_CAMPAIGN_API`, `E2E_RECO_API`,
`E2E_MOBILE_API`, `E2E_POSTGRES_DSN`, `E2E_REDIS_URL`, `E2E_CLICKHOUSE_URL`,
`E2E_KAFKA_BS`, `E2E_SCHEMA_REGISTRY_URL`, `E2E_ACCRUAL_TIMEOUT_S`,
`E2E_READY_TIMEOUT_S`, …).

## Load — Locust

`tests/load/locustfile.py` defines `CashbackUser` (FastHttpUser) with
the production traffic mix:

| Endpoint                                              | Share |
|-------------------------------------------------------|-------|
| `GET /recommendations/{user_id}`                      | 70 %  |
| `POST /v1/mobile/recommendations/{rec_id}/respond`    | 20 %  |
| `GET /campaigns/applicable/{user_id}`                 | 10 %  |

Any non-2xx answer is a failure (401/404 included). The scenario
therefore authenticates like the real clients, with secrets from the
environment or the repo-root `.env` (`tests/auth_tokens.py`): the service
token mobile_api sends to recommendation_api (`iss mobile-api`, signed with
`SERVICE_JWT_SECRET`, renewed before its 5 min expire), the customer's own
token for the mobile BFF (demo IdP, `MOBILE_JWT_SECRET`) and an admin token
for campaign_manager (`LOAD_ADMIN_EMAIL`, password `LOAD_ADMIN_PASSWORD` or
else `ADMIN_BOOTSTRAP_PASSWORD`). A missing secret stops locust at start-up.
It needs real users with features: `LOCUST_USER_POOL_FILE` (one
`user_id` per line from a seeded stack). "Respond" opens the offers via the
mobile BFF with that user's token, stores the first offer's
`recommendation_id` (rec_api returns none) and accepts it with the same
token (`POST /respond`, 2xx; 410 if it expired meanwhile).
The next respond repeats the accept on the same offer — answered once (B13),
so `POST /respond (repeat)` expects 409/410 and a 2xx there is a failure —
and then a fresh offer is opened.

The BRE lets a user see 5 offers a day (frequency gate) and hides a shown
campaign for 14 days (anti-fatigue), so after one `/recommendations` call a
user has nothing left to accept. The pool is split: even lines drive
`/recommendations` and `/campaigns/applicable`, odd lines are walked one by
one to open offers. An opened offer list with nothing to accept is a
failure ("offer pool used up") — use a bigger pool for long runs, or reseed /
delete `rec_count:*` and `recent_offer:*` in Redis between runs.

A `LoadTestShape` ramp 100→1200 RPS over 10 minutes is available for
soak/peak runs with `LOAD_STEP_SHAPE=1` (a shape overrides users/duration,
so it is off by default). The default `make test-load` invocation wraps
`scripts/run_load_test.sh` (1000 users × 5 minutes) and writes the
report to `test-reports/load-<timestamp>.html`.

```bash
make test-load                                # 1000 users × 5 min
LOAD_USERS=1500 LOAD_DURATION=10m make test-load
LOAD_STEP_SHAPE=1 make test-load              # 100 → 1200 users ramp
```

## Conventions

* Markers — `@pytest.mark.integration`, `@pytest.mark.e2e`, `slow`.
* Async tests — `asyncio_mode = "auto"` (no decorator needed).
* Fixtures factor out at the layer boundary: per-service `conftest.py`
  for unit-level stubs, `tests/conftest.py` for repo-wide containers.
* Each load run produces `load-<timestamp>.html` plus `*_stats.csv`,
  `*_failures.csv`, `*_exceptions.csv` for downstream parsing.
