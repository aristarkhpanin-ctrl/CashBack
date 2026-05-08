# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-05-08

### Added
- **Phase 1.** Monorepo skeleton, `docker-compose.yml` for the full local
  stack (Postgres 16, ClickHouse 24, Redis 7.2, Kafka 3.7 KRaft, Schema
  Registry, MLflow 2.13, Airflow 2.9, Adminer, Kafka UI), Makefile with
  `up / down / logs / ps / seed / test / clean`.
- **Phase 2.** Alembic OLTP migrations (chapter 2.2, table 9):
  `users`, `user_segments`, `cashback_campaigns`, `campaign_categories`,
  `recommendations`, `user_consents`, `cashback_accruals`,
  `ab_experiments`, `ab_variants`, `ab_assignments`, `ab_events`. Hot-path
  indexes per the dissertation. PL/pgSQL `calculate_cashback` function
  (progressive `rate_tiers`). ClickHouse `transactions_raw`
  (MergeTree, monthly partitions, 36-month TTL),
  `user_rfm_features` (108 columns), `transactions_buffer`.
- **Phase 3.** TX simulator (`services/tx_simulator/`) — Avro
  `TransactionEvent` (listing 1), realistic MCC distribution, Friday-
  evening boost, Click CLI (`init-users / backfill / stream`),
  multi-stage Docker.
- **Phase 4.** ETL service (`services/etl/`) — TransactionConsumer
  (listing 3.2), DataValidator (listing 3.4, six rules from table 16),
  ClickHouseLoader (buffer-table ingest), RFMComputer (listing 3.6, RFM_QUERY
  + Redis pipeline), Airflow DAGs `cashback_daily_etl` (listing 3.3) and
  `ml_retrain_pipeline`. 27 unit tests with 98 % coverage on validator.
- **Phase 5.** ML training (`services/ml_training/`) —
  `LGBMTrainer` (listing 3.7) with 5-fold CV + SHAP top-20,
  `SVDPPTrainer` (ALS over user×MCC), `promote_if_better` (listing 3.8)
  with PSI gate, MLflow tracking + model registry.
- **Phase 6.** Recommendation API (`services/recommendation_api/`) —
  three-stage pipeline (listing 3.9): retrieval (FAISS) → LightGBM
  ranking → BRE filter; `BusinessRulesEngine` with six rules from
  table 19, `ModelWatcher` zero-downtime swap, OpenTelemetry
  instrumentation, Prometheus `/metrics`.
- **Phase 7.** Campaign Manager (`services/campaign_manager/`) — table 18
  REST endpoints (CRUD + applicable + budget reservation
  `SELECT FOR UPDATE`), analytics (funnel / segment-matrix / cohort /
  top-campaigns), A/B testing (table 20) with z-test + significance
  badge, APScheduler housekeeping.
- **Phase 8.** Transaction listener + accrual engine + notification
  pipeline (listings 3.11 + 3.12) — chain-of-responsibility adapters
  (Push / Email / SMS / In-App), OST (Optimal Send Time), atomic
  PG transaction with `calculate_cashback` + budget update + Redis
  delete + Kafka `cashback.accrued` event.
- **Phase 9.** Mobile BFF (`services/mobile_api/`) — table 22, four
  endpoints, axios-style client to recommendation_api with retry
  + circuit breaker (tenacity), MCC catalog enrichment, deeplink
  generation, SnoozeScheduler (apscheduler).
- **Phase 10.** Frontend (React 19 + Vite + shadcn/ui + Recharts +
  TanStack Query + zustand) integrated with real APIs;
  `/experiments` page, `/recommendations/:id/explain` with
  bidirectional SHAP bar chart; nginx reverse-proxy, Vite dev proxy.
- **Phase 11.** Testing pyramid (chapter 3.3, table 23) — unit
  ≥85 % coverage gates, 47 integration tests, 9-step e2e cycle,
  Locust load test (1 000 RPS / 10 min, 70/20/10 mix).
- **Phase 12.** GitHub Actions CI/CD (listing 3.16) — lint &
  type-check (Python 3.11 + 3.12 matrix), test, build (multi-arch
  GHCR push), deploy stub. PR-fast-feedback workflow,
  Dependabot, CODEOWNERS, pre-commit hooks (ruff + mypy +
  prettier + shellcheck), README badges.
- **Phase 13.** Helm chart for Kubernetes (chapter 3.3, table 27) —
  per-service Deployment / Service / HPA, ML CronJob,
  Ingress (regex + rewrite), NetworkPolicy default-deny baseline,
  bitnami subcharts (Postgres / Redis / Kafka / ClickHouse,
  conditional). `helm-package.sh` for distribution.
- **Phase 14.** Comprehensive documentation — master README,
  ADRs 0001-0012 (Nygard format), C4 / IDEF0 / BPMN / UML diagrams
  (PlantUML + PNG), thesis mapping document, CHANGELOG, CONTRIBUTING,
  LICENSE (MIT), SECURITY policy.

### Reference
- Reference dissertation: chapters 1-3 of "Personalised cashback
  recommendation system based on transaction history".
- All chapter / listing / table numbers in code comments correspond
  to the published dissertation text.
