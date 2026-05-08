"""Unit tests for the TX simulator distributions."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timezone
from decimal import Decimal

import numpy as np
import pytest
from app import simulator as sim


# ---------------------------------------------------------------------------
# MCC distribution
# ---------------------------------------------------------------------------
def test_mcc_codes_are_unique_and_top30():
    assert len(sim.TOP30_MCC) == 30
    assert len(set(sim.TOP30_MCC)) == 30


def test_mcc_probs_sum_to_one():
    assert sim.MCC_BASE_PROBS.sum() == pytest.approx(1.0)


def test_mcc_explicit_weights_dominate_other_top30():
    """5411/5812/5541/5912 should each be much larger than the rest weight."""
    rest_codes = [m for m in sim.TOP30_MCC if m not in sim.EXPLICIT_MCC_WEIGHTS]
    rest_w = (1.0 - sum(sim.EXPLICIT_MCC_WEIGHTS.values())) / len(rest_codes)
    for code, expected in sim.EXPLICIT_MCC_WEIGHTS.items():
        idx = list(sim.MCC_CODES).index(code)
        assert sim.MCC_BASE_PROBS[idx] == pytest.approx(expected)
    # Rest probabilities are equal.
    rest_probs = [
        p for c, p in zip(sim.MCC_CODES, sim.MCC_BASE_PROBS)
        if int(c) not in sim.EXPLICIT_MCC_WEIGHTS
    ]
    assert all(p == pytest.approx(rest_w) for p in rest_probs)


def test_mcc_sampling_matches_expected_distribution():
    """Empirical distribution over many samples should be close to the spec."""
    rng = np.random.default_rng(123)
    n = 200_000
    samples = rng.choice(sim.MCC_CODES, size=n, p=sim.MCC_BASE_PROBS)
    cnt = Counter(int(x) for x in samples)
    # 1.5% absolute tolerance on each leading category.
    assert abs(cnt[5411] / n - 0.25) < 0.015
    assert abs(cnt[5812] / n - 0.15) < 0.015
    assert abs(cnt[5541] / n - 0.10) < 0.012
    assert abs(cnt[5912] / n - 0.08) < 0.012


# ---------------------------------------------------------------------------
# Hour-of-day weights
# ---------------------------------------------------------------------------
def test_hour_weights_have_two_peaks():
    w = sim.HOUR_WEIGHTS
    # Lunch peak.
    assert w[12] >= 1.4 and w[13] >= 1.4
    # Evening peak strictly higher than lunch.
    assert w[19] > w[12]
    assert w[20] > w[13]
    # Quiet pre-dawn hours.
    assert w[3] <= 0.1
    assert w[4] <= 0.1


def test_friday_evening_boost():
    base = sim.hour_dow_weight(20, 1)         # Tuesday 20:00
    boosted = sim.hour_dow_weight(20, 4)      # Friday 20:00
    assert boosted == pytest.approx(base * 1.5)
    # Saturday is also boosted.
    assert sim.hour_dow_weight(21, 5) == pytest.approx(sim.HOUR_WEIGHTS[21] * 1.5)
    # Saturday morning (10:00) is NOT boosted.
    assert sim.hour_dow_weight(10, 5) == pytest.approx(sim.HOUR_WEIGHTS[10])


def test_sample_hour_minute_second_in_range():
    rng = np.random.default_rng(0)
    for _ in range(100):
        h, m, s = sim.sample_hour_minute_second(rng, dow=2)
        assert 0 <= h < 24
        assert 0 <= m < 60
        assert 0 <= s < 60


def test_hour_distribution_peaks_in_target_windows():
    rng = np.random.default_rng(0)
    n = 50_000
    hours = [sim.sample_hour_minute_second(rng, dow=2)[0] for _ in range(n)]
    cnt = Counter(hours)
    lunch = sum(cnt[h] for h in (12, 13, 14)) / n
    evening = sum(cnt[h] for h in (18, 19, 20, 21)) / n
    night = sum(cnt[h] for h in (1, 2, 3, 4)) / n
    assert lunch > 0.15
    assert evening > 0.30
    assert evening > lunch
    assert night < 0.05


# ---------------------------------------------------------------------------
# Amount sampling
# ---------------------------------------------------------------------------
def test_sample_amount_is_positive_decimal():
    rng = np.random.default_rng(7)
    for mcc in (5411, 5812, 5541, 5912, 9999):
        a = sim.sample_amount(mcc, rng)
        assert isinstance(a, Decimal)
        assert a > Decimal("0")
        # Two decimal places preserved.
        assert -a.as_tuple().exponent == 2


def test_sample_amount_respects_mcc_scale():
    """Gas-station amounts should typically be > pharmacy amounts."""
    rng = np.random.default_rng(11)
    n = 5_000
    gas = [float(sim.sample_amount(5541, rng)) for _ in range(n)]
    pharm = [float(sim.sample_amount(5912, rng)) for _ in range(n)]
    assert np.median(gas) > np.median(pharm) * 2


# ---------------------------------------------------------------------------
# Profile + transaction
# ---------------------------------------------------------------------------
def test_generate_profile_shape():
    rng = np.random.default_rng(0)
    p = sim.generate_profile(0, rng)
    assert isinstance(p.user_id, str)
    assert p.external_id == "sim_00000000"
    assert 1 <= p.segment_id <= 10
    assert len(p.favorite_mccs) == 3
    assert len(set(p.favorite_mccs)) == 3
    assert all(m in sim.TOP30_MCC for m in p.favorite_mccs)
    assert p.avg_ticket > 0
    assert 0.2 <= p.freq_per_day <= 12.0


def test_user_mcc_probs_boost_favourites():
    rng = np.random.default_rng(1)
    p = sim.generate_profile(0, rng)
    probs = sim.user_mcc_probs(p, fav_boost=3.0)
    assert probs.sum() == pytest.approx(1.0)
    fav_probs = [
        probs[list(sim.MCC_CODES).index(m)] for m in p.favorite_mccs
    ]
    base_probs = [
        sim.MCC_BASE_PROBS[list(sim.MCC_CODES).index(m)] for m in p.favorite_mccs
    ]
    # Each favourite gets a strictly higher probability than its base.
    for fp, bp in zip(fav_probs, base_probs):
        assert fp > bp


def test_build_transaction_matches_avro_field_names():
    rng = np.random.default_rng(2)
    p = sim.generate_profile(0, rng)
    ts = datetime(2026, 4, 28, 19, 30, tzinfo=UTC)
    evt = sim.build_transaction(p, ts, rng)
    # All required Avro fields present and string-typed where the schema demands.
    for field in ("transaction_id", "user_id", "mcc_code", "amount",
                  "currency", "transaction_date", "channel"):
        assert field in evt
    assert evt["currency"] == "RUB"
    assert evt["channel"] in sim.CHANNELS
    assert len(evt["mcc_code"]) == 4
    # transaction_date must be ISO-8601.
    datetime.fromisoformat(evt["transaction_date"])


def test_channel_distribution_is_valid_channel():
    rng = np.random.default_rng(3)
    for mcc in (5411, 4111, 4900, 9999):
        ch = sim.sample_channel(mcc, rng)
        assert ch in sim.CHANNELS


# ---------------------------------------------------------------------------
# Avro schema sanity
# ---------------------------------------------------------------------------
def test_avro_schema_file_exists_and_parses(tmp_path, monkeypatch):
    """The .avsc shipped with the repo should match TransactionEvent fields."""
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    schema_path = repo_root / "infrastructure" / "kafka" / "schemas" / "transaction_event.avsc"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["name"] == "TransactionEvent"
    assert schema["namespace"] == "ru.fintech.cashback"
    field_names = [f["name"] for f in schema["fields"]]
    expected = ["transaction_id", "user_id", "mcc_code", "amount", "currency",
                "transaction_date", "channel", "merchant_id", "metadata"]
    assert field_names == expected


# ---------------------------------------------------------------------------
# Topic config wiring
# ---------------------------------------------------------------------------
def test_topic_config_matches_table_14():
    assert sim.TOPIC_CONFIG["retention.ms"] == "604800000"
    assert sim.TOPIC_CONFIG["compression.type"] == "zstd"
    assert sim.TOPIC_CONFIG["max.message.bytes"] == "1048576"
