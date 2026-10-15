"""cashback_daily_etl: Kafka offsets are committed only after a successful load (B43 b),
and no single message or row can block the pipeline or vanish silently.

The DAG's task code runs through ``tests.dag_runner`` (a recording stand-in
for ``airflow.decorators`` + an ``all_success`` executor) against in-memory
Kafka / Schema Registry / ClickHouse fakes.
"""
from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiokafka import TopicPartition
from app import feature_eng, kafka_consumer
from app import settings as settings_mod
from app.kafka_consumer import ExtractManifest

from tests.dag_runner import AirflowFailException, load_dag, run
from tests.unit.fakes import (
    FakeAvroDeserializer,
    FakeClickHouse,
    FakeKafka,
    FakeSchemaRegistry,
    Msg,
    msg,
)

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

    kafka, ch, registry = FakeKafka(), FakeClickHouse(), FakeSchemaRegistry()
    cfg = settings_mod.Settings(staging_dir=str(tmp_path), kafka_bootstrap_servers="kafka:9092")
    monkeypatch.setattr(settings_mod, "get_settings", lambda: cfg)
    monkeypatch.setattr(kafka_consumer, "AIOKafkaConsumer", kafka.factory)
    monkeypatch.setattr(kafka_consumer, "SchemaRegistryClient", lambda conf: registry)
    monkeypatch.setattr(kafka_consumer, "AvroDeserializer", FakeAvroDeserializer)
    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **_kw: ch)
    monkeypatch.setattr(feature_eng.RFMComputer, "run", lambda self, window_days: 0)

    async def no_sleep(_seconds):  # drain() idles between empty polls
        return None

    monkeypatch.setattr(kafka_consumer.asyncio, "sleep", no_sleep)
    return types.SimpleNamespace(kafka=kafka, ch=ch, cfg=cfg, registry=registry, staging=tmp_path)


def _records(n: int, **overrides) -> list[dict]:
    when = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    return [{
        "transaction_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "mcc_code": "5411", "amount": "10.00", "currency": "RUB",
        "transaction_date": when, "channel": "POS", "merchant_id": None,
        "metadata": '{"src": "app"}', **overrides,
    } for _ in range(n)]


def _jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def _batch(records: list[dict]) -> list:
    return [msg(r, partition=i % 2, offset=i // 2) for i, r in enumerate(records)]


LAG = {"check_kafka_lag": {"lag_per_partition": {}, "topic": TOPIC}}


# ---------------------------------------------------------------------------
def test_commit_task_depends_on_load_and_extract_manifest(nodes):
    commit = nodes["commit_kafka_offsets"]
    assert commit.args == (nodes["extract_transactions"], nodes["load_to_clickhouse"])
    assert nodes["validate_and_cleanse"].args == (nodes["extract_transactions"],)
    assert nodes["load_to_clickhouse"].upstream() == {"validate_and_cleanse"}
    # The quarantine check runs only once the offsets are committed.
    assert nodes["check_quarantine"].upstream() == {
        "extract_transactions", "load_to_clickhouse", "commit_kafka_offsets"}


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
    assert results["load_to_clickhouse"] == {"rows": 5, "quarantined_rows": 0,
                                             "quarantine_files": []}
    assert results["check_quarantine"]["dead_letters"] == 0
    for task_id in ("commit_kafka_offsets", "load_to_clickhouse", "check_quarantine"):
        json.dumps(results[task_id])  # XCom-serialisable


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


def test_dead_letters_are_committed_and_fail_the_run_visibly(nodes, env):
    env.kafka.batches = [[Msg(TOPIC, 0, 0, b"not avro"), Msg(TOPIC, 0, 1, b"\x00")]]

    results, failed = run(nodes, LAG)

    # The pipeline moves past them (offsets committed) ...
    manifest = ExtractManifest.read(results["extract_transactions"])
    assert manifest.files == [] and manifest.dead_letter_count == 2
    assert Path(manifest.dead_letter_files[0]).exists()
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 2}
    # ... but the run is red, naming the dead-letter file (it used to be green).
    assert set(failed) == {"check_quarantine"}
    assert isinstance(failed["check_quarantine"], AirflowFailException)
    assert manifest.dead_letter_files[0] in str(failed["check_quarantine"])


def test_registry_outage_fails_extract_commits_nothing_and_next_run_loads_all(nodes, env):
    """B43 review: dead-lettering a registry outage committed past valid transactions."""
    records = _records(4)
    env.kafka.batches = [_batch(records)]
    env.registry.error = ConnectionRefusedError(111, "Connection refused")

    _results, failed = _run(nodes, LAG)

    assert "extract_transactions" in failed
    assert env.kafka.group_committed == {} and env.kafka.commits == []
    assert list(env.staging.rglob("dead_letter_*")) == []
    assert env.ch.raw_ids() == []

    # Registry back: Kafka redelivers the same messages, all of them land.
    env.registry.error = None
    env.kafka.batches = [_batch(records)]
    results, failed = _run(nodes, LAG)
    assert failed == set()
    assert sorted(env.ch.raw_ids()) == sorted(r["transaction_id"] for r in records)
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 2,
                                         TopicPartition(TOPIC, 1): 2}
    assert results["check_quarantine"]["dead_letters"] == 0


def test_records_the_validator_rejects_go_to_a_file_and_do_not_block(nodes, env):
    """Rows ClickHouse cannot parse (or that crashed the validator) blocked every run."""
    good = _records(4)
    bad = (_records(1, user_id="user-42") + _records(1, amount="1_000")
           + _records(1, amount="NaN") + _records(1, amount="-5.00"))
    env.kafka.batches = [_batch(good + bad)]

    results, failed = _run(nodes, LAG)

    assert failed == set()  # business-rule rejects are expected, not a red run
    assert sorted(env.ch.raw_ids()) == sorted(r["transaction_id"] for r in good)
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 4,
                                         TopicPartition(TOPIC, 1): 4}
    [rejected] = env.staging.rglob("rejected_*.jsonl")
    [clean] = results["validate_and_cleanse"]
    assert rejected.parent == Path(clean).parent
    assert rejected.name.removeprefix("rejected_").split(".")[0] in clean
    by_id = {e["record"]["transaction_id"]: e for e in _jsonl(rejected)}
    assert {tid: e["rule"] for tid, e in by_id.items()} == {
        bad[0]["transaction_id"]: "R1_not_null",
        bad[1]["transaction_id"]: "R2_amount_range",
        bad[2]["transaction_id"]: "R2_amount_range",
        bad[3]["transaction_id"]: "R2_amount_range",
    }
    assert "user-42" in by_id[bad[0]["transaction_id"]]["reason"]
    assert "NaN" in by_id[bad[2]["transaction_id"]]["reason"]


def test_mixed_date_shapes_load_as_utc(nodes, env):
    """Mixed ISO-8601 shapes failed StagingWriter (pandas) and then every run."""
    hour_ago = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    shapes = [hour_ago.isoformat(), hour_ago.isoformat().replace("+00:00", "Z"),
              hour_ago.replace(microsecond=123456).isoformat(),
              hour_ago.astimezone(timezone(timedelta(hours=3))).isoformat(),
              hour_ago.replace(tzinfo=None).isoformat()]
    records = [{**r, "transaction_date": d} for r, d in zip(_records(5), shapes)]
    env.kafka.batches = [_batch(records)]

    _results, failed = _run(nodes, LAG)

    assert failed == set()
    loaded = {r["transaction_id"]: r["transaction_date"] for r in env.ch.raw}
    assert [loaded[r["transaction_id"]].replace(microsecond=0) for r in records] == [hour_ago] * 5


def test_rows_clickhouse_rejects_are_quarantined_and_offsets_commit(nodes, env):
    """A row the validator passes but ClickHouse rejects used to block the ETL forever."""
    records = _records(6)
    poison = records[3]["transaction_id"]
    env.ch.reject_ids = {poison}
    env.kafka.batches = [_batch(records)]

    results, failed = run(nodes, LAG)

    assert sorted(env.ch.raw_ids()) == sorted(
        r["transaction_id"] for r in records if r["transaction_id"] != poison)
    assert env.kafka.group_committed == {TopicPartition(TOPIC, 0): 3,
                                         TopicPartition(TOPIC, 1): 3}
    loaded = results["load_to_clickhouse"]
    assert loaded["rows"] == 5 and loaded["quarantined_rows"] == 1
    [entry] = _jsonl(loaded["quarantine_files"][0])
    assert entry["record"]["transaction_id"] == poison and entry["code"] == 6
    # Set aside, so the run is red and points at the file.
    assert set(failed) == {"check_quarantine"}
    assert loaded["quarantine_files"][0] in str(failed["check_quarantine"])
