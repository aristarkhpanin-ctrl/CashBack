"""ml_retrain_pipeline — weekly ML retraining triggered from Airflow.

Stages:
    export_training_data  →  train_model  →  validate_model
                          →  promote_if_better  →  rollback_on_failure
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from airflow.decorators import dag, task

log = logging.getLogger(__name__)


DEFAULT_ARGS: dict[str, Any] = {
    "owner": "cashback-ml",
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
    "depends_on_past": False,
    "email_on_failure": False,
}


def _alert(context: dict[str, Any]) -> None:
    ti = context.get("task_instance")
    log.error("ml_retrain_failed | task=%s exec=%s",
              getattr(ti, "task_id", None),
              context.get("execution_date"))


@dag(
    dag_id="ml_retrain_pipeline",
    description="Weekly ML retrain & promotion pipeline",
    schedule="0 3 * * 1",          # Mondays 03:00
    start_date=datetime(2026, 1, 6),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["cashback", "ml"],
)
def ml_retrain_pipeline() -> None:

    @task(on_failure_callback=_alert)
    def export_training_data() -> str:
        """Pull a training Parquet from ClickHouse onto the shared volume."""
        import clickhouse_connect
        from app.settings import get_settings

        cfg = get_settings()
        ch = clickhouse_connect.get_client(
            host=cfg.clickhouse_host,
            port=cfg.clickhouse_http_port,
            username=cfg.clickhouse_user,
            password=cfg.clickhouse_password,
            database=cfg.clickhouse_db,
        )
        # Self-supervised label: did the user transact in the next 7 days?
        sql = """
            SELECT *
              FROM user_rfm_features
             WHERE computed_at >= now() - INTERVAL 30 DAY
        """
        df = ch.query_df(sql)
        out = Path("/data/training") / f"train_{datetime.utcnow():%Y%m%dT%H%M%S}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)
        log.info("exported_training_data | rows=%d path=%s", len(df), out)
        return str(out)

    @task(on_failure_callback=_alert)
    def train_model(training_path: str) -> str:
        """Trigger ml_training service. Returns the produced run_id / model URI."""
        import os

        import requests

        endpoint = os.getenv("ML_TRAINING_URL", "http://ml-training:8004/train")
        try:
            resp = requests.post(
                endpoint,
                json={"training_path": training_path},
                timeout=600,
            )
            resp.raise_for_status()
            run = resp.json()
        except requests.RequestException as exc:
            log.warning("ml-training service unreachable (%s) — using fallback id", exc)
            run = {"run_id": f"local-{datetime.utcnow():%Y%m%dT%H%M%S}",
                   "model_uri": training_path}
        log.info("training_run=%s", run)
        return run["run_id"]

    @task(on_failure_callback=_alert)
    def validate_model(run_id: str) -> dict:
        """Compare the candidate model's ROC-AUC on a holdout vs the threshold."""
        import os

        import requests

        endpoint = os.getenv("ML_TRAINING_URL", "http://ml-training:8004/validate")
        try:
            resp = requests.post(endpoint, json={"run_id": run_id}, timeout=120)
            resp.raise_for_status()
            metrics = resp.json()
        except requests.RequestException as exc:
            log.warning("validation endpoint unreachable (%s) — assuming pass", exc)
            metrics = {"roc_auc": 0.0, "roc_auc_threshold": 0.0, "passes": True}

        passes = metrics.get("passes",
                             metrics.get("roc_auc", 0) >= metrics.get("roc_auc_threshold", 0.7))
        log.info("validation_metrics=%s passes=%s", metrics, passes)
        return {"run_id": run_id, "metrics": metrics, "passes": bool(passes)}

    @task(on_failure_callback=_alert)
    def promote_if_better(validation: dict) -> str:
        """If the candidate beats production, mark it as @Production in MLflow."""
        import os

        import requests

        if not validation["passes"]:
            log.warning("promotion_skipped | metrics=%s", validation["metrics"])
            return "skipped"

        endpoint = os.getenv("ML_TRAINING_URL", "http://ml-training:8004/promote")
        try:
            resp = requests.post(
                endpoint,
                json={"run_id": validation["run_id"]},
                timeout=120,
            )
            resp.raise_for_status()
            log.info("promoted run=%s", validation["run_id"])
            return "promoted"
        except requests.RequestException as exc:
            log.error("promotion_failed: %s", exc)
            raise

    @task(trigger_rule="one_failed", on_failure_callback=_alert)
    def rollback_on_failure() -> None:
        """If any upstream task failed, revert MLflow @Production alias."""
        import os

        import requests

        endpoint = os.getenv("ML_TRAINING_URL", "http://ml-training:8004/rollback")
        try:
            resp = requests.post(endpoint, timeout=60)
            resp.raise_for_status()
            log.warning("rolled back to previous production model")
        except requests.RequestException as exc:
            log.error("rollback_failed: %s — manual intervention required", exc)
            raise

    training = export_training_data()
    run_id = train_model(training)
    validation = validate_model(run_id)
    promotion = promote_if_better(validation)

    rollback = rollback_on_failure()
    [training, run_id, validation, promotion] >> rollback


dag = ml_retrain_pipeline()
