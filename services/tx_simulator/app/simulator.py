"""TX Simulator — synthetic transaction generator.

Implements the simulator described in chapter 3.1 of the dissertation:
generates a population of synthetic users, each with its own behavioural
profile (top-3 MCC categories, average ticket, frequency-per-day), and
streams Avro-serialised TransactionEvent records to Kafka through the
Confluent Schema Registry.

Usage::

    python -m app.simulator init-users --count 10000
    python -m app.simulator backfill   --days 90 --rate 1000
    python -m app.simulator stream     --rate 50
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Iterator

import click
import numpy as np
import psycopg2
from confluent_kafka import KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import (
    MessageField,
    SerializationContext,
    StringSerializer,
)
from dotenv import load_dotenv
from faker import Faker
from fastavro import parse_schema, reader as avro_reader, writer as avro_writer
from psycopg2.extras import execute_batch

load_dotenv()

logger = logging.getLogger("tx_simulator")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
SCHEMA_REGISTRY = os.getenv("SCHEMA_REGISTRY_URL", "http://schema-registry:8081")
TOPIC = os.getenv("KAFKA_TOPIC_TRANSACTIONS", "transactions.raw")
AVRO_SCHEMA_PATH = Path(os.getenv("AVRO_SCHEMA_PATH", "/app/schemas/transaction_event.avsc"))
PROFILES_PATH = Path(os.getenv("PROFILES_PATH", "/data/profiles.avro"))
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://cashback:cashback@postgres:5432/cashback",
)

# Topic settings — chapter 3.1, table 14.
TOPIC_CONFIG = {
    "retention.ms": "604800000",
    "compression.type": "zstd",
    "max.message.bytes": "1048576",
}
TOPIC_PARTITIONS = int(os.getenv("KAFKA_TOPIC_PARTITIONS", "3"))
TOPIC_REPLICATION = int(os.getenv("KAFKA_TOPIC_REPLICATION", "1"))


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------
TOP30_MCC: list[int] = [
    5411, 5812, 5814, 5541, 5499, 5912, 5311, 5651, 5732, 5942,
    5921, 5462, 4111, 4121, 4131, 4814, 4829, 4900, 7832, 7011,
    7299, 7211, 7230, 7538, 7995, 8011, 8021, 8062, 8099, 8211,
]
assert len(TOP30_MCC) == 30 and len(set(TOP30_MCC)) == 30

# Explicit weights from the dissertation.
EXPLICIT_MCC_WEIGHTS = {
    5411: 0.25,  # groceries
    5812: 0.15,  # restaurants
    5541: 0.10,  # gas stations
    5912: 0.08,  # pharmacies
}


def _build_mcc_distribution() -> tuple[np.ndarray, np.ndarray]:
    rest_codes = [m for m in TOP30_MCC if m not in EXPLICIT_MCC_WEIGHTS]
    rest_weight = (1.0 - sum(EXPLICIT_MCC_WEIGHTS.values())) / len(rest_codes)
    codes = np.array(TOP30_MCC, dtype=np.int32)
    weights = np.array(
        [EXPLICIT_MCC_WEIGHTS.get(int(m), rest_weight) for m in codes],
        dtype=np.float64,
    )
    return codes, weights / weights.sum()


MCC_CODES, MCC_BASE_PROBS = _build_mcc_distribution()


# Hourly activity (24 buckets). Peaks at 12–14 (lunch) and 18–21 (evening).
HOUR_WEIGHTS: np.ndarray = np.array(
    [
        0.10, 0.05, 0.05, 0.05, 0.05, 0.05,   # 00-05
        0.10, 0.30, 0.50, 0.60, 0.70, 0.90,   # 06-11
        1.50, 1.50, 1.30, 0.80, 0.80, 0.90,   # 12-17
        1.80, 1.80, 1.80, 1.50, 0.90, 0.40,   # 18-23
    ],
    dtype=np.float64,
)


def hour_dow_weight(hour: int, dow: int) -> float:
    """Joint weight: hourly base × Friday/Saturday-evening boost."""
    base = HOUR_WEIGHTS[hour]
    if dow in (4, 5) and 17 <= hour <= 23:
        base *= 1.5
    return float(base)


def sample_hour_minute_second(rng: np.random.Generator, dow: int) -> tuple[int, int, int]:
    weights = np.array([hour_dow_weight(h, dow) for h in range(24)], dtype=np.float64)
    weights /= weights.sum()
    hour = int(rng.choice(24, p=weights))
    minute = int(rng.integers(0, 60))
    second = int(rng.integers(0, 60))
    return hour, minute, second


# Lognormal (mu, sigma) per MCC for amount in RUB.
MCC_AMOUNT_PARAMS: dict[int, tuple[float, float]] = {
    5411: (float(np.log(800)),  0.60),
    5812: (float(np.log(1500)), 0.70),
    5814: (float(np.log(500)),  0.60),
    5541: (float(np.log(2500)), 0.40),
    5499: (float(np.log(700)),  0.65),
    5912: (float(np.log(400)),  0.60),
    5311: (float(np.log(2000)), 0.70),
    5651: (float(np.log(3000)), 0.80),
    5732: (float(np.log(8000)), 1.00),
    5942: (float(np.log(800)),  0.70),
    5921: (float(np.log(1200)), 0.60),
    5462: (float(np.log(600)),  0.55),
    4111: (float(np.log(60)),   0.60),
    4121: (float(np.log(400)),  0.70),
    4131: (float(np.log(50)),   0.50),
    4814: (float(np.log(500)),  0.60),
    4829: (float(np.log(2000)), 1.00),
    4900: (float(np.log(3500)), 0.50),
    7832: (float(np.log(600)),  0.50),
    7011: (float(np.log(5000)), 0.90),
}
DEFAULT_AMOUNT = (float(np.log(1000)), 0.70)


def sample_amount(mcc: int, rng: np.random.Generator) -> Decimal:
    mu, sigma = MCC_AMOUNT_PARAMS.get(mcc, DEFAULT_AMOUNT)
    val = float(rng.lognormal(mean=mu, sigma=sigma))
    val = max(val, 1.0)
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


CHANNELS = ("ONLINE", "POS", "ATM", "MOBILE")
# Per-channel base mix; overlaid with MCC-specific tweaks (e.g. ATM rare for groceries).
CHANNEL_BASE_PROBS = np.array([0.30, 0.45, 0.05, 0.20])


def sample_channel(mcc: int, rng: np.random.Generator) -> str:
    probs = CHANNEL_BASE_PROBS.copy()
    if mcc == 5411:           # groceries — mostly POS
        probs = np.array([0.20, 0.65, 0.02, 0.13])
    elif mcc in (4111, 4121, 4131):  # transport — mostly mobile/online
        probs = np.array([0.40, 0.10, 0.00, 0.50])
    elif mcc == 4900:         # utilities — online
        probs = np.array([0.70, 0.05, 0.00, 0.25])
    return CHANNELS[int(rng.choice(len(CHANNELS), p=probs))]


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------
PROFILE_AVRO_SCHEMA = parse_schema(
    {
        "type": "record",
        "name": "UserProfile",
        "namespace": "ru.fintech.cashback",
        "fields": [
            {"name": "user_id", "type": "string"},
            {"name": "external_id", "type": "string"},
            {"name": "segment_id", "type": "int"},
            {"name": "favorite_mccs", "type": {"type": "array", "items": "int"}},
            {"name": "avg_ticket", "type": "double"},
            {"name": "freq_per_day", "type": "double"},
        ],
    }
)


@dataclass
class UserProfile:
    user_id: str
    external_id: str
    segment_id: int
    favorite_mccs: list[int]
    avg_ticket: float
    freq_per_day: float

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "external_id": self.external_id,
            "segment_id": int(self.segment_id),
            "favorite_mccs": [int(x) for x in self.favorite_mccs],
            "avg_ticket": float(self.avg_ticket),
            "freq_per_day": float(self.freq_per_day),
        }


def generate_profile(idx: int, rng: np.random.Generator) -> UserProfile:
    user_id = str(uuid.uuid4())
    external_id = f"sim_{idx:08d}"
    segment_id = int(rng.integers(1, 11))
    # Three favourite MCCs, sampled by the global probability (so popular MCCs
    # get over-represented as favourites but not exclusively).
    fav_idx = rng.choice(len(MCC_CODES), size=3, replace=False, p=MCC_BASE_PROBS)
    favourites = [int(MCC_CODES[i]) for i in fav_idx]
    avg_ticket = float(np.exp(rng.normal(np.log(900), 0.5)))     # ~ median 900
    freq_per_day = float(np.clip(rng.gamma(shape=2.0, scale=1.0), 0.2, 12.0))
    return UserProfile(user_id, external_id, segment_id, favourites, avg_ticket, freq_per_day)


def load_profiles(path: Path) -> list[UserProfile]:
    if not path.exists():
        raise click.ClickException(
            f"Profiles file not found at {path}. Run `init-users` first."
        )
    profiles: list[UserProfile] = []
    with path.open("rb") as f:
        for rec in avro_reader(f):
            profiles.append(
                UserProfile(
                    user_id=rec["user_id"],
                    external_id=rec["external_id"],
                    segment_id=int(rec["segment_id"]),
                    favorite_mccs=[int(x) for x in rec["favorite_mccs"]],
                    avg_ticket=float(rec["avg_ticket"]),
                    freq_per_day=float(rec["freq_per_day"]),
                )
            )
    return profiles


# ---------------------------------------------------------------------------
# Transaction generation
# ---------------------------------------------------------------------------
fake = Faker("ru_RU")
Faker.seed(42)


def user_mcc_probs(profile: UserProfile, fav_boost: float = 3.0) -> np.ndarray:
    """Mix the global MCC distribution with a 3× boost for the user's favourites."""
    weights = MCC_BASE_PROBS.copy()
    fav_set = set(profile.favorite_mccs)
    for i, code in enumerate(MCC_CODES):
        if int(code) in fav_set:
            weights[i] *= fav_boost
    return weights / weights.sum()


def build_transaction(
    profile: UserProfile,
    ts: datetime,
    rng: np.random.Generator,
    mcc_probs: np.ndarray | None = None,
) -> dict:
    if mcc_probs is None:
        mcc_probs = user_mcc_probs(profile)
    mcc = int(rng.choice(MCC_CODES, p=mcc_probs))
    amount = sample_amount(mcc, rng)
    # Personal scale: re-centre the lognormal around the user's avg_ticket.
    scale = profile.avg_ticket / 900.0
    amount = (amount * Decimal(str(scale))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    channel = sample_channel(mcc, rng)
    return {
        "transaction_id": str(uuid.uuid4()),
        "user_id": profile.user_id,
        "mcc_code": f"{mcc:04d}",
        "amount": str(amount),
        "currency": "RUB",
        "transaction_date": ts.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "merchant_id": fake.bothify(text="MID-########") if rng.random() > 0.05 else None,
        "metadata": None,
    }


# ---------------------------------------------------------------------------
# Kafka plumbing
# ---------------------------------------------------------------------------
def _load_avro_schema_str() -> str:
    return AVRO_SCHEMA_PATH.read_text(encoding="utf-8")


def ensure_topic(admin: AdminClient | None = None) -> None:
    """Create transactions.raw with the dissertation's table-14 settings."""
    admin = admin or AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
    new = NewTopic(
        TOPIC,
        num_partitions=TOPIC_PARTITIONS,
        replication_factor=TOPIC_REPLICATION,
        config=TOPIC_CONFIG,
    )
    futures = admin.create_topics([new])
    try:
        futures[TOPIC].result(timeout=15)
        logger.info("created topic %s with config %s", TOPIC, TOPIC_CONFIG)
    except KafkaException as exc:
        err = exc.args[0] if exc.args else None
        code = err.code() if hasattr(err, "code") else None
        if code == KafkaError.TOPIC_ALREADY_EXISTS:
            logger.info("topic %s already exists — enforcing config", TOPIC)
            _alter_topic_config(admin)
        else:
            raise


def _alter_topic_config(admin: AdminClient) -> None:
    cr = ConfigResource(ConfigResource.Type.TOPIC, TOPIC, set_config=TOPIC_CONFIG)
    fs = admin.alter_configs([cr])
    try:
        fs[cr].result(timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not alter topic config (%s) — continuing", exc)


def make_producer() -> Producer:
    return Producer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "compression.type": "zstd",
            "linger.ms": 20,
            "batch.size": 65536,
            "acks": "1",
            "queue.buffering.max.messages": 1_000_000,
            "client.id": "tx-simulator",
        }
    )


def make_avro_serializer() -> AvroSerializer:
    sr = SchemaRegistryClient({"url": SCHEMA_REGISTRY})
    return AvroSerializer(sr, _load_avro_schema_str())


def _delivery_report_factory(counter: dict) -> callable:
    def _on_delivery(err, msg):
        if err is not None:
            counter["err"] += 1
            if counter["err"] <= 5:
                logger.warning("delivery failed: %s", err)
        else:
            counter["ok"] += 1
    return _on_delivery


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------
@contextmanager
def pg_conn() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(POSTGRES_DSN)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_users(profiles: list[UserProfile]) -> None:
    sql = (
        "INSERT INTO users (user_id, external_id, segment_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (external_id) DO NOTHING"
    )
    rows = [(p.user_id, p.external_id, p.segment_id) for p in profiles]
    with pg_conn() as conn, conn.cursor() as cur:
        execute_batch(cur, sql, rows, page_size=1000)


# ---------------------------------------------------------------------------
# Pacing utility
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, rate_per_sec: float) -> None:
        self.interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._next = time.monotonic()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + self.interval


# ---------------------------------------------------------------------------
# Simulator class — orchestrates the pieces above
# ---------------------------------------------------------------------------
class TransactionSimulator:
    def __init__(self, seed: int | None = None) -> None:
        self.rng = np.random.default_rng(seed)
        self._serializer: AvroSerializer | None = None
        self._producer: Producer | None = None
        self._string_serializer = StringSerializer("utf_8")

    def producer(self) -> Producer:
        if self._producer is None:
            self._producer = make_producer()
        return self._producer

    def serializer(self) -> AvroSerializer:
        if self._serializer is None:
            self._serializer = make_avro_serializer()
        return self._serializer

    def publish(self, event: dict, on_delivery=None) -> None:
        ctx = SerializationContext(TOPIC, MessageField.VALUE)
        value = self.serializer()(event, ctx)
        key = self._string_serializer(event["user_id"])
        while True:
            try:
                self.producer().produce(
                    topic=TOPIC,
                    key=key,
                    value=value,
                    on_delivery=on_delivery,
                )
                break
            except BufferError:
                self.producer().poll(0.5)

    def stream_events(self, events: Iterable[dict], rate: float) -> tuple[int, int]:
        limiter = RateLimiter(rate)
        counter = {"ok": 0, "err": 0}
        report = _delivery_report_factory(counter)
        last_log = time.monotonic()
        for i, evt in enumerate(events, start=1):
            limiter.wait()
            self.publish(evt, on_delivery=report)
            if i % 5000 == 0:
                self.producer().poll(0)
            now = time.monotonic()
            if now - last_log > 5:
                logger.info("produced=%d ok=%d err=%d", i, counter["ok"], counter["err"])
                last_log = now
        self.producer().flush(30)
        return counter["ok"], counter["err"]


# ---------------------------------------------------------------------------
# Backfill / stream generators
# ---------------------------------------------------------------------------
def iter_backfill_events(
    profiles: list[UserProfile],
    days: int,
    rng: np.random.Generator,
) -> Iterator[dict]:
    """Yield events with timestamps spread uniformly across `days` days,
    weighted by hour-of-day and Fri-Sat boost.
    """
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    span_seconds = days * 86400

    for profile in profiles:
        n = int(rng.poisson(profile.freq_per_day * days))
        if n == 0:
            continue
        mcc_probs = user_mcc_probs(profile)
        offsets = rng.uniform(0.0, span_seconds, size=n)
        offsets.sort()
        for off in offsets:
            ts = start + timedelta(seconds=float(off))
            # Reweight: rejection-resample hour with the joint weight.
            h, m, s = sample_hour_minute_second(rng, ts.weekday())
            ts = ts.replace(hour=h, minute=m, second=s)
            yield build_transaction(profile, ts, rng, mcc_probs)


def iter_stream_events(
    profiles: list[UserProfile],
    rng: np.random.Generator,
) -> Iterator[dict]:
    """Infinite generator picking a random user weighted by freq_per_day."""
    weights = np.array([p.freq_per_day for p in profiles], dtype=np.float64)
    weights /= weights.sum()
    indices = np.arange(len(profiles))
    while True:
        idx = int(rng.choice(indices, p=weights))
        profile = profiles[idx]
        now = datetime.now(timezone.utc)
        yield build_transaction(profile, now, rng)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.group()
def cli() -> None:
    """TX Simulator — synthetic transactions for the CashBack stack."""


@cli.command("init-users")
@click.option("--count", default=10_000, show_default=True, type=int)
@click.option("--seed", default=42, show_default=True, type=int)
@click.option("--profiles-path", default=str(PROFILES_PATH), show_default=True)
@click.option("--skip-postgres", is_flag=True, help="Don't write to Postgres.")
def init_users_cmd(count: int, seed: int, profiles_path: str, skip_postgres: bool) -> None:
    """Generate `count` synthetic users + persist profiles + Postgres rows."""
    rng = np.random.default_rng(seed)
    target = Path(profiles_path)

    logger.info("generating %d user profiles...", count)
    profiles = [generate_profile(i, rng) for i in range(count)]

    if not skip_postgres:
        logger.info("inserting users into Postgres...")
        insert_users(profiles)

    logger.info("writing profiles avro to %s", target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as f:
        avro_writer(f, PROFILE_AVRO_SCHEMA,
                    (p.to_dict() for p in profiles), codec="deflate")
    logger.info("done — %d profiles persisted", count)


@cli.command("backfill")
@click.option("--days", default=90, show_default=True, type=int)
@click.option("--rate", default=1000.0, show_default=True, type=float,
              help="Events per second (producer pacing).")
@click.option("--seed", default=None, type=int)
@click.option("--profiles-path", default=str(PROFILES_PATH), show_default=True)
def backfill_cmd(days: int, rate: float, seed: int | None, profiles_path: str) -> None:
    """Generate `days` days of historical transactions."""
    rng = np.random.default_rng(seed)
    profiles = load_profiles(Path(profiles_path))
    logger.info("loaded %d profiles", len(profiles))

    ensure_topic()
    sim = TransactionSimulator(seed=seed)
    events = iter_backfill_events(profiles, days=days, rng=rng)
    ok, err = sim.stream_events(events, rate=rate)
    logger.info("backfill done: ok=%d err=%d", ok, err)


@cli.command("stream")
@click.option("--rate", default=50.0, show_default=True, type=float,
              help="Events per second.")
@click.option("--seed", default=None, type=int)
@click.option("--profiles-path", default=str(PROFILES_PATH), show_default=True)
def stream_cmd(rate: float, seed: int | None, profiles_path: str) -> None:
    """Continuous live stream of transactions."""
    rng = np.random.default_rng(seed)
    profiles = load_profiles(Path(profiles_path))
    logger.info("loaded %d profiles — starting stream at %.1f tx/s", len(profiles), rate)

    ensure_topic()
    sim = TransactionSimulator(seed=seed)

    stop = {"flag": False}

    def _handle(*_: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    def gen() -> Iterator[dict]:
        for evt in iter_stream_events(profiles, rng):
            if stop["flag"]:
                break
            yield evt

    ok, err = sim.stream_events(gen(), rate=rate)
    logger.info("stream stopped: ok=%d err=%d", ok, err)


@cli.command("ensure-topic")
def ensure_topic_cmd() -> None:
    """Create transactions.raw with table-14 settings (idempotent)."""
    ensure_topic()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
