# 0004 — PostgreSQL 16 as the OLTP store

* **Status:** Accepted
* **Date:** 2025-12-17

## Context

Thirteen relational entities from chapter 2.2 (table 9): `users`,
`user_segments`, `cashback_campaigns`, `campaign_categories`,
`recommendations`, `user_consents`, `cashback_accruals`,
`ab_experiments`, `ab_variants`, `ab_assignments`, `ab_events`.

Hard requirements:

* row-level locking (`SELECT … FOR UPDATE` for budget reservation —
  rule R6 of the BRE);
* native arrays (`target_segment_ids INT[]`, `allowed_channels TEXT[]`);
* JSONB for `rate_tiers` (progressive cashback) and `strategy_params`
  (A/B variants);
* native ENUM types (`campaign_status`, `accrual_status`, ...);
* PL/pgSQL (`calculate_cashback` stored procedure — listing 1).

## Alternatives

| Option        | Pros | Cons |
|---------------|------|------|
| **PostgreSQL 16** | Native arrays, JSONB, ENUMs, plpgsql, `FOR UPDATE`, mature ecosystem (psycopg2/asyncpg, alembic, sqlalchemy 2.0 async). | Heavier than MySQL; tuning complexity. |
| MySQL 8       | Wide hosting; simpler. | No native arrays/ENUM in our shape; weaker `FOR UPDATE` semantics. |
| CockroachDB   | Horizontal scale, SQL-compat. | Distributed transaction tax for our row-locking patterns; heavier ops. |
| DynamoDB      | Managed, fast. | Wrong shape — we need joins, FK + arrays. |

## Decision

**PostgreSQL 16-alpine** with `psycopg2-binary` (admin / migrations) +
**SQLAlchemy 2.0 async + asyncpg** (FastAPI services). Migrations are
managed by **Alembic** (`db_migrations/`) — a single `001_init_oltp.py`
encapsulates all 13 tables, all six ENUM types and the
`calculate_cashback` PL/pgSQL function.

Indexes designed in chapter 2.2:

* `(user_id, generated_at DESC)` on recommendations
* partial `WHERE response_status='PENDING'` on recommendations
* `(user_id, consent_type) INCLUDE (status)` on user_consents
* `(user_id, experiment_id)` on ab_assignments

Three logical databases (provisioned at first start by
`infrastructure/postgres/init/01-init-databases.sh`):

* `cashback`  — application data
* `airflow`   — Airflow metastore
* `mlflow`    — MLflow backend store (currently sqlite, reserved here)

## Consequences

* + Rule R6 (`budget_reservation`) is implemented as a single
  `BEGIN; SELECT … FOR UPDATE; UPDATE; COMMIT` — no distributed
  transaction needed (see
  `services/transaction_listener/app/accrual/engine.py`).
* + Foreign keys catch orphan rows during ETL backfills.
* + `calculate_cashback` runs server-side — the accrual engine doesn't
  reimplement progressive tiers.
* − Vertical scaling only; mitigated by read-replicas in production.
* − Schema changes need Alembic migrations — small overhead, big payoff.

## References

* `db_migrations/migrations/versions/001_init_oltp.py`
* `services/transaction_listener/app/accrual/engine.py`
* `services/campaign_manager/app/api/campaigns.py` (`SELECT FOR UPDATE`).
