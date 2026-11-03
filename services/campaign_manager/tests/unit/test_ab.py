"""Unit tests for A/B testing helpers (assignment + z-test).

Этап D (B34): z-тест не падает на доле > 1 и не выходит за [0, 1], вердикт
несёт направление (winner), контроль задаётся при создании (ровно один).
Сам расчёт результатов, назначение и 409 — на настоящем Postgres
(tests/integration/test_analytics_ab.py).
"""
from __future__ import annotations

import pytest
from app.api.ab_testing import (
    _significance_badge,
    _winner,
    deterministic_bucket,
    pick_variant,
    two_proportion_z_test,
)
from app.schemas import ABExperimentCreate
from pydantic import ValidationError


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


# ---------------------------------------------------------------------------
# Этап D (B34)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("args", [
    (30, 20, 5, 20),        # событий конверсии больше назначений: была ValueError
    (5, 20, 21, 20),
    (-1, 20, 5, 20),
])
def test_z_test_rejects_rates_outside_unit_interval(args):
    assert two_proportion_z_test(*args) is None


@pytest.mark.parametrize("args", [
    (0, 1000, 1000, 1000),  # |z| огромный
    (1, 10, 2, 10),
    (500, 1000, 500, 1000),
])
def test_z_test_p_value_is_a_probability(args):
    res = two_proportion_z_test(*args)
    assert res is not None and 0.0 <= res["p_value"] <= 1.0


def test_z_test_zero_variance_returns_none():
    assert two_proportion_z_test(0, 100, 0, 100) is None
    assert two_proportion_z_test(100, 100, 100, 100) is None


def test_z_test_direction_follows_treatment_minus_control():
    worse = two_proportion_z_test(104, 1000, 82, 1000)     # control лучше
    assert worse["diff"] < 0 and worse["z"] < 0 and worse["p_value"] < 0.1


@pytest.mark.parametrize(("significance", "diff", "winner"), [
    ("significant", 0.02, "treatment"),
    ("significant", -0.02, "control"),     # тест значимо хуже — не катить
    ("trending", -0.01, "control"),
    ("trending", 0.01, "treatment"),
    ("no_data", 0.5, None),
    ("significant", 0.0, None),
])
def test_winner_is_the_direction_of_the_effect(significance, diff, winner):
    assert _winner(significance, diff) == winner


def _payload(*variants):
    return {"name": "exp", "target_metric": "acceptance_rate",
            "start_date": "2026-10-01T00:00:00Z", "variants": list(variants)}


def _v(name, weight=0.5, **extra):
    return {"name": name, "traffic_weight": weight, "strategy_class": "S", **extra}


def test_first_variant_is_the_control_by_default():
    exp = ABExperimentCreate.model_validate(_payload(_v("zeta"), _v("alpha")))
    assert [(v.name, v.is_control) for v in exp.variants] == [
        ("zeta", True), ("alpha", False)]


def test_explicit_control_flag_wins_over_order():
    exp = ABExperimentCreate.model_validate(
        _payload(_v("test"), _v("base", is_control=True)))
    assert [(v.name, v.is_control) for v in exp.variants] == [
        ("test", False), ("base", True)]


@pytest.mark.parametrize("variants", [
    [_v("a", is_control=True), _v("b", is_control=True)],
    [_v("a", is_control=False), _v("b", is_control=False)],
    [_v("a", is_control=False), _v("b")],
])
def test_exactly_one_control_or_422(variants):
    with pytest.raises(ValidationError, match="exactly one variant must be the control"):
        ABExperimentCreate.model_validate(_payload(*variants))


def test_duplicate_variant_names_are_rejected():
    with pytest.raises(ValidationError, match="variant names must be unique"):
        ABExperimentCreate.model_validate(_payload(_v("a"), _v("a")))
