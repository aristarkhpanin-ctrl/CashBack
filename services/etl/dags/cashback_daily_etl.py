"""cashback_daily_etl — chapter 3.1, listing 3.3.

Runs daily at 02:00, drains the previous 24h of Kafka events, validates &
cleanses them, loads into ClickHouse, and recomputes RFM features.
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
    def extract_transactions(_lag: dict) -> list[str]:
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

        async def _run() -> list[str]:
            await consumer.start()
            paths: list[str] = []
            try:
                empty_polls = 0
                while empty_polls < 3 and len(paths) < 200:
                    count, path = await consumer.consume_batch()
                    if path is None:
                        empty_polls += 1
                        await asyncio.sleep(0.5)
                        continue
                    empty_polls = 0
                    paths.append(str(path))
            finally:
                await consumer.stop()
            return paths

        paths = asyncio.run(_run())
        log.info("extracted %d parquet batches", len(paths))
        return paths

    # ---------- 3) validate & cleanse --------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def validate_and_cleanse(parquet_paths: list[str]) -> list[str]:
        import clickhouse_connect
        import pyarrow.parquet as pq
        from app.settings import get_settings
        from app.validator import DataValidator

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
                if report.valid:
                    # Re-write cleansed Parquet next to the source file.
                    import pandas as pd
                    cleansed_path = p.replace(".parquet", ".clean.parquet")
                    pd.DataFrame.from_records(report.valid).to_parquet(
                        cleansed_path, index=False
                    )
                    cleansed_paths.append(cleansed_path)
            return cleansed_paths

        return asyncio.run(_validate_all())

    # ---------- 4) load to ClickHouse --------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def load_to_clickhouse(clean_paths: list[str]) -> int:
        if not clean_paths:
            return 0
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
        log.info("loaded total_rows=%d", rows)
        return rows

    # ---------- 5) compute RFM features ------------------------------
    @task(on_failure_callback=_alert_on_failure)
    def compute_rfm_features(_rows: int) -> int:
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

    # ---------- DAG wiring -------------------------------------------
    lag = check_kafka_lag()
    paths = extract_transactions(lag)
    cleansed = validate_and_cleanse(paths)
    loaded = load_to_clickhouse(cleansed)
    compute_rfm_features(loaded)


dag = cashback_daily_etl()
