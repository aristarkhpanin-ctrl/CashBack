# 0010 — Apache Airflow as the workflow orchestrator

* **Status:** Accepted
* **Date:** 2025-12-20

## Context

The platform has two scheduled multi-step workflows:

* `cashback_daily_etl` (chapter 3.1, listing 3.3) — daily 02:00 UTC,
  five tasks: `check_kafka_lag → extract_transactions →
  validate_and_cleanse → load_to_clickhouse → compute_rfm_features`;
* `ml_retrain_pipeline` — weekly Mondays 03:00 UTC,
  `export_training_data → train_model → validate_model →
  promote_if_better` with a `rollback_on_failure` trigger-rule task.

Both workflows need: retries, exponential back-off, alerting on
failure, structured logging, observability via UI.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **Apache Airflow 2.9** | Proven; rich UI; Python-native DAGs; broad operator catalogue; battle-tested in finance. | Heavyweight (scheduler + webserver + metastore); LocalExecutor caps at one machine. |
| Prefect 3 | Pythonic; cloud option. | Smaller ecosystem; fewer operators. |
| Dagster | Software-defined assets; strong typing. | Operationally lighter, but less ubiquitous in fintech. |
| Argo Workflows | Native K8s; container-per-step. | Steeper auth setup; YAML-heavy. |
| Plain cron + Bash | Trivial. | No retries, no UI, no DAG visualisation. |

## Decision

**Apache Airflow 2.9.0** with **LocalExecutor** in this thesis stack
(scheduler does the actual task execution — no Celery worker). For
production we ship an `airflow-worker` service profile gated behind
`docker compose --profile celery` and a Helm-chart toggle, so the
operator can switch executors without touching DAGs.

DAGs live in `services/etl/dags/`:

* `cashback_daily_etl.py` — listing 3.3 verbatim. `schedule="0 2 * * *"`,
  `catchup=False`, `max_active_runs=1`, `retries=3`, `retry_delay=5min`,
  `on_failure_callback` per task.
* `ml_retrain_pipeline.py` — listing 3.3 sibling for ML, weekly,
  `rollback_on_failure` task with `trigger_rule="one_failed"`.

Airflow uses Postgres as its metastore (separate database `airflow`
provisioned by `infrastructure/postgres/init/01-init-databases.sh`).

The custom Airflow image (`airflow/etl:0.1`,
`services/etl/Dockerfile`) bundles the ETL package so the DAG bodies
can `from app.kafka_consumer import TransactionConsumer` without a
PYTHONPATH dance.

## Consequences

* + DAG UI gives operators a clear view of every nightly run + per-task
  logs.
* + Trigger-via-API is trivial — see `scripts/trigger_etl.sh` calling
  `/api/v1/dags/cashback_daily_etl/dagRuns`.
* + Retries and backoff handled by the framework — DAG code stays
  business-focused.
* − Three Airflow processes (init / scheduler / webserver) at idle
  consume ~600 MB. Acceptable trade-off; documented in
  `helm/cashback/values.yaml: etlWorker.resources`.
* − Cold-start of a DAG run is a few seconds (parsing). All DAGs
  finish in well under their schedule's slot.

## References

* `services/etl/dags/cashback_daily_etl.py`
* `services/etl/dags/ml_retrain_pipeline.py`
* `scripts/trigger_etl.sh`
* `services/etl/Dockerfile`
* `helm/cashback/templates/deployment-etl-worker.yaml`
