"""cashback_daily_etl — chapter 3.1, listing 3.3.

Runs daily at 02:00, drains the previous 24h of Kafka events, validates &
cleanses them, loads into ClickHouse, and recomputes RFM features.

Kafka offsets are committed by a dedicated task, ``commit_kafka_offsets``,
only after ``load_to_clickhouse`` succeeded: extract records the offsets
it staged in a manifest instead of committing them, so a failure anywhere
before that makes the next run re-read the same messages (the loader
skips rows that are already in ClickHouse).

Nothing a single message or row can do blocks the pipeline, and nothing is
dropped silently:

* messages that can never be decoded -> ``dead_letter_*.jsonl`` (extract);
  a Schema Registry outage fails extract instead, so nothing is committed;
* records the validator rejects -> ``rejected_*.jsonl`` with rule + reason;
* rows ClickHouse still rejects -> ``ch_rejected_*.jsonl`` (load).

All of them sit next to the staged batch. ``check_quarantine`` runs after the
commit and fails the run when messages or rows were dead-lettered /
quarantined, so they show up as a red run with an alert, not a green one.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from airflow.decorators import dag, task

log = logging.getLogger(__name__)


DEFAULT_ARGS: dict[str, Any] = {
    "owner": "cashback-etl",
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
}


def _alert_on_failure(context: dict[str, Any]) -> None:
    """Failure callback — log + push placeholder to monitoring."""
    ti = context.get("task_instance")
    log.error(
        "etl_task_failed | task=%s dag=%s exec=%s log_url=%s",
        getattr(ti, "task_id", None),
        getattr(ti, "dag_id", None),
        context.get("execution_date"),
        getattr(ti, "log_url", None),
    )
    # Hook for slack/PagerDuty/whatever — left intentionally minimal.


@dag(
    dag_id="cashback_daily_etl",
    description="Daily ETL: Kafka → validate → ClickHouse → RFM features",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["cashback", "etl"],
)
def cashback_daily_etl() -> None:

    # ---------- 1) check Kafka lag ----------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def check_kafka_lag() -> dict:
        from aiokafka import AIOKafkaConsumer
        from app.settings import get_settings

        cfg = get_settings()

        async def _run() -> dict:
            consumer = AIOKafkaConsumer(
                cfg.kafka_topic_transactions,
                bootstrap_servers=cfg.kafka_bootstrap_servers,
                group_id=f"{cfg.kafka_consumer_group}-laglog",
                enable_auto_commit=False,
            )
            await consumer.start()
            try:
                partitions = consumer.assignment()
                if not partitions:
                    await asyncio.sleep(2)
                    partitions = consumer.assignment()
                end_offsets = await consumer.end_offsets(list(partitions)) if partitions else {}
                positions = {tp: await consumer.position(tp) for tp in partitions}
                lag = {str(tp): max(0, end_offsets[tp] - positions.get(tp, 0))
                       for tp in partitions}
                return {"lag_per_partition": lag, "topic": cfg.kafka_topic_transactions}
            finally:
                await consumer.stop()

        return asyncio.run(_run())

    # ---------- 2) extract from Kafka into Parquet -------------------
    @task(on_failure_callback=_alert_on_failure)
    def extract_transactions(_lag: dict) -> str:
        """Stage messages as Parquet; returns the manifest path (offsets NOT committed)."""
        from app.kafka_consumer import StagingWriter, TransactionConsumer
        from app.settings import get_settings

        cfg = get_settings()
        staging = StagingWriter(cfg.staging_dir)
        consumer = TransactionConsumer(
            bootstrap_servers=cfg.kafka_bootstrap_servers,
            schema_registry_url=cfg.schema_registry_url,
            topic=cfg.kafka_topic_transactions,
            staging=staging,
            group_id=cfg.kafka_consumer_group,
        )

        async def _run():
            await consumer.start()
            try:
                return await consumer.drain(max_batches=200, max_empty_polls=3)
            finally:
                await consumer.stop()

        manifest = asyncio.run(_run())
        path = staging.write_manifest(manifest)
        log.info(
            "extracted %d records in %d parquet batches, %d dead letters; manifest=%s",
            manifest.records, len(manifest.files), manifest.dead_letter_count, path,
        )
        if manifest.dead_letter_count:
            log.warning("dead letters staged: %s", manifest.dead_letter_files)
        return str(path)

    # ---------- 3) validate & cleanse --------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def validate_and_cleanse(manifest_path: str) -> list[str]:
        """Write ``*.clean.parquet`` (valid records) and ``rejected_*.jsonl`` per batch."""
        from collections import Counter
        from pathlib import Path

        import clickhouse_connect
        import pyarrow as pa
        import pyarrow.parquet as pq
        from app.jsonl import write_jsonl
        from app.kafka_consumer import ExtractManifest
        from app.settings import get_settings
        from app.validator import DataValidator, cleanse

        parquet_paths = ExtractManifest.read(manifest_path).files
        if not parquet_paths:
            return []

        cfg = get_settings()
        ch = clickhouse_connect.get_client(
            host=cfg.clickhouse_host,
            port=cfg.clickhouse_http_port,
            username=cfg.clickhouse_user,
            password=cfg.clickhouse_password,
            database=cfg.clickhouse_db,
        )
        validator = DataValidator(ch_client=ch)

        async def _validate_all() -> list[str]:
            cleansed_paths: list[str] = []
            for p in parquet_paths:
                table = pq.read_table(p)
                records = table.to_pylist()
                report = await validator.validate_batch(records)
                log.info(
                    "validated path=%s ok=%d err=%d (rate=%.4f)",
                    p, len(report.valid), len(report.errors),
                    report.error_rate(),
                )
                if report.errors:
                    stamp = Path(p).name.split(".", 1)[0].removeprefix("batch_")
                    rejected = write_jsonl(
                        Path(p).with_name(f"rejected_{stamp}.jsonl"),
                        [{"rule": e.rule, "reason": e.reason, "record": e.record}
                         for e in report.errors],
                    )
                    log.warning(
                        "rejected %d records of %s by rule %s -> %s",
                        len(report.errors), p,
                        dict(Counter(e.rule for e in report.errors)), rejected,
                    )
                if report.valid:
                    # Re-write cleansed Parquet next to the source file; R4
                    # parsed every transaction_date, cleanse() stores it as UTC.
                    cleansed_path = p.replace(".parquet", ".clean.parquet")
                    pq.write_table(
                        pa.Table.from_pylist([cleanse(r) for r in report.valid]),
                        cleansed_path,
                    )
                    cleansed_paths.append(cleansed_path)
            return cleansed_paths

        return asyncio.run(_validate_all())

    # ---------- 4) load to ClickHouse --------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def load_to_clickhouse(clean_paths: list[str]) -> dict:
        """Load the cleansed files; rows ClickHouse rejects are quarantined, not fatal."""
        if not clean_paths:
            return {"rows": 0, "quarantined_rows": 0, "quarantine_files": []}
        import clickhouse_connect
        from app.ch_loader import ClickHouseLoader
        from app.settings import get_settings

        cfg = get_settings()
        ch = clickhouse_connect.get_client(
            host=cfg.clickhouse_host,
            port=cfg.clickhouse_http_port,
            username=cfg.clickhouse_user,
            password=cfg.clickhouse_password,
            database=cfg.clickhouse_db,
        )
        loader = ClickHouseLoader(ch)
        rows = loader.load_paths(clean_paths)
        log.info("loaded total_rows=%d quarantined_rows=%d",
                 rows, loader.quarantined_rows)
        return {"rows": rows, "quarantined_rows": loader.quarantined_rows,
                "quarantine_files": loader.quarantine_files}

    # ---------- 5) compute RFM features ------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def compute_rfm_features(_loaded: dict) -> int:
        import clickhouse_connect
        import redis
        from app.feature_eng import RFMComputer
        from app.settings import get_settings

        cfg = get_settings()
        ch = clickhouse_connect.get_client(
            host=cfg.clickhouse_host,
            port=cfg.clickhouse_http_port,
            username=cfg.clickhouse_user,
            password=cfg.clickhouse_password,
            database=cfg.clickhouse_db,
        )
        rds = redis.Redis.from_url(cfg.redis_url, decode_responses=True)
        rfm = RFMComputer(ch, rds, ttl_seconds=cfg.feature_ttl_seconds)
        users = rfm.run(window_days=cfg.rfm_window_days)
        log.info("rfm_users=%d", users)
        return users

    # ---------- 6) commit Kafka offsets -------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def commit_kafka_offsets(manifest_path: str, _loaded: dict) -> dict:
        """Commit exactly the offsets extract staged — runs only after a successful load.

        Idempotent: a retry never moves the group's offsets backwards.
        """
        from app.kafka_consumer import ExtractManifest, commit_offsets
        from app.settings import get_settings

        cfg = get_settings()
        manifest = ExtractManifest.read(manifest_path)
        committed = asyncio.run(commit_offsets(manifest, cfg.kafka_bootstrap_servers))
        return {str(p): o for p, o in committed.items()}

    # ---------- 7) surface what was set aside -------------------------
    @task(on_failure_callback=_alert_on_failure)
    def check_quarantine(manifest_path: str, loaded: dict, _committed: dict) -> dict:
        """Fail the run (after the commit) if messages / rows were set aside.

        Dead letters and quarantined rows let the pipeline move on; this
        task keeps that from looking like a clean run. It fails without
        retries: re-running cannot change what was set aside.
        """
        from airflow.exceptions import AirflowFailException
        from app.kafka_consumer import ExtractManifest

        manifest = ExtractManifest.read(manifest_path)
        summary = {
            "dead_letters": manifest.dead_letter_count,
            "dead_letter_files": manifest.dead_letter_files,
            "quarantined_rows": loaded["quarantined_rows"],
            "quarantine_files": loaded["quarantine_files"],
        }
        if summary["dead_letters"] or summary["quarantined_rows"]:
            raise AirflowFailException(
                "data set aside, offsets committed past it; inspect / replay: "
                f"{summary}"
            )
        return summary

    # ---------- DAG wiring -------------------------------------------
    lag = check_kafka_lag()
    manifest = extract_transactions(lag)
    cleansed = validate_and_cleanse(manifest)
    loaded = load_to_clickhouse(cleansed)
    compute_rfm_features(loaded)
    committed = commit_kafka_offsets(manifest, loaded)
    check_quarantine(manifest, loaded, committed)


dag = cashback_daily_etl()
