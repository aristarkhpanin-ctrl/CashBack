"""Unit tests for A/B testing helpers (assignment + z-test)."""
from __future__ import annotations

import pytest
from app.api.ab_testing import (
    _significance_badge,
    deterministic_bucket,
    pick_variant,
    two_proportion_z_test,
)


class _StubVariant:
    def __init__(self, name: str, traffic_weight: float) -> None:
        self.name = name
        self.traffic_weight = traffic_weight


# ---------------------------------------------------------------------------
# Deterministic bucketing — same inputs ⇒ same bucket; in [0, 1).
# ---------------------------------------------------------------------------
def test_deterministic_bucket_is_stable():
    a = deterministic_bucket("user-1", "exp-1")
    b = deterministic_bucket("user-1", "exp-1")
    assert a == b
    assert 0.0 <= a < 1.0


def test_deterministic_bucket_differs_per_user():
    assert deterministic_bucket("u1", "exp") != deterministic_bucket("u2", "exp")


def test_pick_variant_respects_weights():
    """Roughly 50/50 split given uniform users."""
    variants = [_StubVariant("A", 0.5), _StubVariant("B", 0.5)]
    a = b = 0
    for i in range(20_000):
        bucket = deterministic_bucket(f"u-{i}", "exp")
        v = pick_variant(bucket, variants)
        if v.name == "A":
            a += 1
        else:
            b += 1
    assert abs(a - b) < 600   # < 3% drift on 20k samples


def test_pick_variant_skewed_weights():
    variants = [_StubVariant("A", 0.9), _StubVariant("B", 0.1)]
    counts = {"A": 0, "B": 0}
    for i in range(20_000):
        v = pick_variant(deterministic_bucket(f"u-{i}", "x"), variants)
        counts[v.name] += 1
    assert counts["A"] > counts["B"] * 5


# ---------------------------------------------------------------------------
# Two-proportion z-test
# ---------------------------------------------------------------------------
def test_z_test_significant_difference():
    res = two_proportion_z_test(50, 1000, 80, 1000)
    assert res is not None
    assert res["p_value"] < 0.01
    assert res["diff"] == pytest.approx(0.03, abs=1e-6)


def test_z_test_no_difference_returns_high_pvalue():
    res = two_proportion_z_test(50, 1000, 51, 1000)
    assert res is not None
    assert res["p_value"] > 0.5


def test_z_test_zero_observations_returns_none():
    assert two_proportion_z_test(0, 0, 0, 0) is None


def test_significance_badge():
    assert _significance_badge(
        0.001, 1_000, alpha_sig=0.05, alpha_trend=0.20, min_n=200,
    ) == "significant"
    assert _significance_badge(
        0.10, 1_000, alpha_sig=0.05, alpha_trend=0.20, min_n=200,
    ) == "trending"
    assert _significance_badge(
        0.30, 1_000, alpha_sig=0.05, alpha_trend=0.20, min_n=200,
    ) == "no_data"
    # Below sample-size floor → no_data regardless of p-value.
    assert _significance_badge(
        0.001, 50, alpha_sig=0.05, alpha_trend=0.20, min_n=200,
    ) == "no_data"
    assert _significance_badge(
        None, 1_000, alpha_sig=0.05, alpha_trend=0.20, min_n=200,
    ) == "no_data"
