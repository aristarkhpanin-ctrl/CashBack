"""cashback_daily_etl: Kafka offsets are committed only after a successful load (B43 b).

The DAG's task code runs through ``tests.dag_runner`` (a recording stand-in
for ``airflow.decorators`` + an ``all_success`` executor) against in-memory
Kafka / ClickHouse fakes.
"""
from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiokafka import TopicPartition
from app import feature_eng, kafka_consumer
from app import settings as settings_mod
from app.kafka_consumer import ExtractManifest

from tests.dag_runner import load_dag, run
from tests.unit.fakes import FakeClickHouse, FakeKafka, Msg, json_deserializer, msg

TOPIC = "transactions.raw"


def _run(nodes, stubs):
    results, failed = run(nodes, stubs)
    return results, set(failed)


@pytest.fixture
def nodes(monkeypatch):
    return load_dag(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Patch the clients the DAG tasks build so they hit in-memory fakes."""
    import clickhouse_connect

    kafka, ch = FakeKafka(), FakeClickHouse()
    cfg = settings_mod.Settings(staging_dir=str(tmp_path), kafka_bootstrap_servers="kafka:9092")
    monkeypatch.setattr(settings_mod, "get_settings", lambda: cfg)
    monkeypatch.setattr(kafka_consumer, "AIOKafkaConsumer", kafka.factory)
    monkeypatch.setattr(kafka_consumer.TransactionConsumer, "_build_deserializer",
                        lambda self: json_deserializer)
    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **_kw: ch)
    monkeypatch.setattr(feature_eng.RFMComputer, "run", lambda self, window_days: 0)

    async def no_sleep(_seconds):  # drain() idles between empty polls
        return None

    monkeypatch.setattr(kafka_consumer.asyncio, "sleep", no_sleep)
    return types.SimpleNamespace(kafka=kafka, ch=ch, cfg=cfg)


def _records(n: int) -> list[dict]:
    when = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    return [{
        "transaction_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "mcc_code": "5411", "amount": "10.00", "currency": "RUB",
        "transaction_date": when, "channel": "POS", "merchant_id": None,
        "metadata": '{"src": "app"}',
    } for _ in range(n)]


def _batch(records: list[dict]) -> list:
    return [msg(r, partition=i % 2, offset=i // 2) for i, r in enumerate(records)]


LAG = {"check_kafka_lag": {"lag_per_partition": {}, "topic": TOPIC}}


# ---------------------------------------------------------------------------
def test_commit_task_depends_on_load_and_extract_manifest(nodes):
    commit = nodes["commit_kafka_offsets"]
    assert commit.args == (nodes["extract_transactions"], nodes["load_to_clickhouse"])
    assert nodes["validate_and_cleanse"].args == (nodes["extract_transactions"],)
    assert nodes["load_to_clickhouse"].upstream() == {"validate_and_cleanse"}


def test_successful_run_commits_exactly_the_staged_offsets(nodes, env):
    records = _records(5)  # partitions 0,1 -> offsets 0..2 and 0..1
    env.kafka.batches = [_batch(records)]

    results, failed = _run(nodes, LAG)

    assert failed == set()
    manifest = ExtractManifest.read(results["extract_transactions"])
    assert manifest.offsets == {0: 3, 1: 2}
    assert env.kafka.commits == [{TopicPartition(TOPIC, 0): 3, TopicPartition(TOPIC, 1): 2}]
    assert results["commit_kafka_offsets"] == {"0": 3, "1": 2}
    assert sorted(env.ch.raw_ids()) == sorted(r["transaction_id"] for r in records)
    json.dumps(results["commit_kafka_offsets"])  # XCom-serialisable


def test_failed_load_commits_nothing_and_next_run_recovers(nodes, env):
    records = _records(4)
    env.kafka.batches = [_batch(records)]
    env.ch.fail_next_inserts = 1  # ClickHouse down during the load

    _results, failed = _run(nodes, LAG)

    assert {"load_to_clickhouse", "commit_kafka_offsets"} <= failed
    assert env.kafka.commits == []  # the old extract committed right after staging
    assert env.kafka.group_committed == {}

    # Next run: nothing was committed, so Kafka redelivers the same messages.
    env.kafka.batches = [_batch(records)]
    _results, failed = _run(nodes, LAG)
    assert failed == set()
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 2,
                                         TopicPartition(TOPIC, 1): 2}
    assert sorted(env.ch.raw_ids()) == sorted(r["transaction_id"] for r in records)


def test_rfm_failure_does_not_block_the_commit(nodes, env, monkeypatch):
    env.kafka.batches = [_batch(_records(2))]

    def boom(self, window_days):
        raise RuntimeError("redis down")

    monkeypatch.setattr(feature_eng.RFMComputer, "run", boom)
    _results, failed = _run(nodes, LAG)
    assert failed == {"compute_rfm_features"}
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 1,
                                         TopicPartition(TOPIC, 1): 1}


def test_dead_letters_only_run_still_commits(nodes, env):
    env.kafka.batches = [[Msg(TOPIC, 0, 0, b"not avro"), Msg(TOPIC, 0, 1, b"\x00")]]
    results, failed = _run(nodes, LAG)
    assert failed == set()
    manifest = ExtractManifest.read(results["extract_transactions"])
    assert manifest.files == [] and manifest.dead_letter_count == 2
    assert Path(manifest.dead_letter_files[0]).exists()
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 2}
