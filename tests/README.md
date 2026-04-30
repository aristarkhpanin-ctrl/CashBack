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

## Integration (47+ tests across 6 categories)

Repo-level tests under `tests/integration/`. Real testcontainers (Postgres
+ Redis) drive IO-bound tests; in-memory stubs cover the remaining
flows so the suite finishes in ~30 s on a warm machine.

| File                                  | Tests | Category                      |
|---------------------------------------|-------|-------------------------------|
| `test_kafka_loading.py`               |   6   | Kafka consumer → ClickHouse   |
| `test_feature_store.py`               |   6   | Redis FeatureStore CRUD       |
| `test_campaigns_crud.py`              |   8   | Campaign Manager CRUD/filter  |
| `test_bre_rules.py`                   |  18   | BRE 6 rules × 3 cases         |
| `test_notifications.py`               |   5   | Notification 4 channels       |
| `test_recommendation_flow.py`         |   4   | Recommendation API end-to-end |
| **total**                             | **47**|                               |

```bash
make up                       # backend services need to be alive
make test-integration
```

The `tests/conftest.py` exposes session-scoped `postgres_container`,
`redis_container`, `clickhouse_container`, `kafka_container` fixtures
that import-skip when Docker isn't available.

## E2E (1 test, ≤5 s)

`tests/e2e/test_full_cashback_cycle.py` runs the 9-step "полный цикл
персонализированного кэшбэка" against the live stack:

1. create test user + ACTIVE campaign (campaign_manager)
2. insert 150 synthetic transactions / 4 MCC / 90 days into ClickHouse
3. seed RFM features in Redis (Airflow alternative)
4. `GET /v1/mobile/recommendations/{user}` → assert *mobile shape*
   (`cashback_rate`, `deeplink`, `terms_summary`; **no** `model_score`)
5. `POST /respond {ACCEPTED}` → assert `accepted_offers:*` SETEX
6. publish a transaction to Kafka
7. wait ≤ 3 s for the listener
8. assert `cashback_accruals` row + correct amount
9. assert `budget_spent` incremented + Redis hot-key removed

```bash
make up
make test-e2e
```

Override targets via env (`E2E_CAMPAIGN_API`, `E2E_RECO_API`, …).

## Load — Locust

`tests/load/locustfile.py` defines `CashbackUser` (FastHttpUser) with
the production traffic mix:

| Endpoint                                              | Share |
|-------------------------------------------------------|-------|
| `GET /recommendations/{user_id}`                      | 70 %  |
| `POST /v1/mobile/recommendations/{rec_id}/respond`    | 20 %  |
| `GET /campaigns/applicable/{user_id}`                 | 10 %  |

A `LoadTestShape` ramp 100→1200 RPS over 10 minutes is included for
soak/peak runs. The default `make test-load` invocation wraps
`scripts/run_load_test.sh` (1000 users × 5 minutes) and writes the
report to `test-reports/load-<timestamp>.html`.

```bash
make test-load                                # 1000 users × 5 min
LOAD_USERS=1500 LOAD_DURATION=10m make test-load
```

## Conventions

* Markers — `@pytest.mark.integration`, `@pytest.mark.e2e`, `slow`.
* Async tests — `asyncio_mode = "auto"` (no decorator needed).
* Fixtures factor out at the layer boundary: per-service `conftest.py`
  for unit-level stubs, `tests/conftest.py` for repo-wide containers.
* Each load run produces `load-<timestamp>.html` plus `*_stats.csv`,
  `*_failures.csv`, `*_exceptions.csv` for downstream parsing.
