"""Unit tests for quantile-sketch PSI (phase 18)."""
from __future__ import annotations

import numpy as np
from app.psi import (
    compute_psi_from_quantiles,
    compute_psi_per_feature_from_quantiles,
)

GRID = np.linspace(0.0, 1.0, 101)


def _quantiles(sample: np.ndarray) -> list[float]:
    return [float(v) for v in np.quantile(sample, GRID)]


def test_identical_distributions_have_near_zero_psi():
    rng = np.random.default_rng(42)
    a = _quantiles(rng.normal(100, 15, 20_000))
    b = _quantiles(rng.normal(100, 15, 20_000))
    assert compute_psi_from_quantiles(a, b) < 0.02


def test_shifted_distribution_exceeds_threshold():
    rng = np.random.default_rng(42)
    ref = _quantiles(rng.normal(100, 15, 20_000))
    cur = _quantiles(rng.normal(140, 15, 20_000))  # сдвиг ~2.7σ
    assert compute_psi_from_quantiles(ref, cur) > 0.2


def test_constant_features_do_not_blow_up():
    const = [5.0] * 101
    assert compute_psi_from_quantiles(const, const) == 0.0


def test_per_feature_uses_common_columns_only():
    rng = np.random.default_rng(7)
    q = _quantiles(rng.normal(0, 1, 5_000))
    shifted = _quantiles(rng.normal(3, 1, 5_000))
    ref = {"a": q, "b": q, "only_ref": q}
    cur = {"a": q, "b": shifted, "only_cur": q}
    psi = compute_psi_per_feature_from_quantiles(ref, cur)
    assert set(psi) == {"a", "b"}
    assert psi["a"] < 0.02
    assert psi["b"] > 0.2
