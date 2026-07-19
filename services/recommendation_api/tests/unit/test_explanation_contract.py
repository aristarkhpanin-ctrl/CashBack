"""Unit tests for the extended ML-explanation contract (phase 25)."""
from __future__ import annotations

import math

import numpy as np
from app.api.recommendations import (
    RecommendationItem,
    _base_value,
    _confidence,
    _expected_roi,
    _interpretation,
    _rationale,
    _to_prob,
)


# ── _to_prob ──────────────────────────────────────────────────────────────────
def test_to_prob_passes_through_probabilities():
    assert _to_prob(0.18) == 0.18
    assert _to_prob(0.9) == 0.9


def test_to_prob_sigmoids_log_odds():
    # Значения вне [0,1] трактуются как лог-оддсы → sigmoid.
    # Реальная база модели ~0.18 = sigmoid(-1.5).
    assert abs(_to_prob(-1.5) - (1 / (1 + math.exp(1.5)))) < 1e-9
    assert abs(_to_prob(2.0) - (1 / (1 + math.exp(-2.0)))) < 1e-9
    assert _to_prob(10.0) == 0.99
    assert _to_prob(-10.0) == 0.01
    # В диапазоне [0,1] — уже вероятность, берём как есть.
    assert _to_prob(0.5) == 0.5


def test_to_prob_clamps_and_handles_garbage():
    assert 0.01 <= _to_prob(math.nan) <= 0.99 or _to_prob(math.nan) == 0.18


# ── _base_value ───────────────────────────────────────────────────────────────
class _Explainer:
    def __init__(self, ev):
        self.expected_value = ev


def test_base_value_scalar_and_array():
    assert 0.0 < _base_value(_Explainer(0.0)) < 1.0
    # массив [class0, class1] → берём класс 1
    v = _base_value(_Explainer(np.array([-2.0, 1.5])))
    assert abs(v - _to_prob(1.5)) < 1e-9


# ── _confidence ───────────────────────────────────────────────────────────────
def _item(score):
    return RecommendationItem(mcc_code="5411", score=score)


def test_confidence_grows_with_margin():
    wide = _confidence([_item(0.9), _item(0.4)])   # margin 0.5
    narrow = _confidence([_item(0.9), _item(0.85)])  # margin 0.05
    assert wide > narrow
    assert 0.5 <= narrow <= 0.99 and 0.5 <= wide <= 0.99


def test_confidence_single_item_uses_default_margin():
    assert _confidence([_item(0.7)]) == round(0.5 + 0.2 * 2.0, 4)


# ── _expected_roi ─────────────────────────────────────────────────────────────
def test_expected_roi_bounded_and_rate_sensitive():
    hi = _expected_roi(0.9, {"cashback_rate": 5})
    lo = _expected_roi(0.9, {"cashback_rate": 20})   # дороже ставка → ниже ROI
    assert hi > lo
    assert 1.0 <= lo <= 5.5


# ── _interpretation / _rationale ─────────────────────────────────────────────
def test_interpretation_strength_and_direction():
    assert "сильно" in _interpretation(0.2) and "повышает" in _interpretation(0.2)
    assert "слегка" in _interpretation(0.01) and "снижает" in _interpretation(-0.01)


def test_rationale_mentions_probability_and_is_nonempty():
    top = RecommendationItem(mcc_code="5912", score=0.73,
                             top_factors={"a": 0.2, "b": -0.05})
    r = _rationale(top)
    assert "73%" in r and len(r) > 10
