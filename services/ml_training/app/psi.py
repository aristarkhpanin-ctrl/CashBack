"""Population Stability Index — feature-level distribution drift metric."""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

EPS = 1e-6


def compute_psi(
    reference: Iterable[float] | np.ndarray | pd.Series,
    current: Iterable[float] | np.ndarray | pd.Series,
    bins: int = 10,
) -> float:
    """PSI = Σ (cur_pct − ref_pct) · ln(cur_pct / ref_pct).

    Both samples are bucketed using quantile edges from ``reference`` so
    that each bucket starts with ~1/bins of the reference distribution.
    """
    ref = np.asarray(list(reference) if not hasattr(reference, "__array__")
                     else reference, dtype=float)
    cur = np.asarray(list(current)   if not hasattr(current, "__array__")
                     else current,   dtype=float)
    ref = ref[~np.isnan(ref)]
    cur = cur[~np.isnan(cur)]

    if ref.size == 0 or cur.size == 0:
        return 0.0
    if np.allclose(ref, ref[0]) and np.allclose(cur, cur[0]):
        return 0.0

    quantiles = np.linspace(0.0, 1.0, bins + 1)
    edges = np.unique(np.quantile(ref, quantiles))
    if len(edges) < 2:
        return 0.0
    edges[0] = -np.inf
    edges[-1] = np.inf

    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)

    ref_pct = ref_counts / max(ref.size, 1)
    cur_pct = cur_counts / max(cur.size, 1)

    ref_pct = np.where(ref_pct == 0, EPS, ref_pct)
    cur_pct = np.where(cur_pct == 0, EPS, cur_pct)

    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def compute_psi_per_feature(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    bins: int = 10,
) -> dict[str, float]:
    """Per-column PSI over the intersection of numeric columns."""
    out: dict[str, float] = {}
    common = [c for c in reference.columns if c in current.columns]
    for col in common:
        ref_col = pd.to_numeric(reference[col], errors="coerce").dropna()
        cur_col = pd.to_numeric(current[col],   errors="coerce").dropna()
        if ref_col.empty or cur_col.empty:
            out[col] = 0.0
            continue
        out[col] = compute_psi(ref_col, cur_col, bins=bins)
    return out

def compute_psi_from_quantiles(
    ref_quantiles: list[float],
    cur_quantiles: list[float],
    bins: int = 10,
) -> float:
    """PSI по квантильным сводкам (фаза 18) — без сырых данных.

    ``*_quantiles`` — значения перцентилей p0..p100 (101 точка,
    артефакт ``feature_quantiles.json`` тренировочного рана).
    Референсные бины — децили reference-распределения (по построению
    в каждом ~10% reference); доля current в бине восстанавливается
    интерполяцией эмпирической CDF по его перцентилям.
    """
    ref_q = np.asarray(ref_quantiles, dtype=float)
    cur_q = np.asarray(cur_quantiles, dtype=float)
    if ref_q.size < 2 or cur_q.size < 2:
        return 0.0
    if np.allclose(ref_q, ref_q[0]) and np.allclose(cur_q, cur_q[0]):
        return 0.0

    grid = np.linspace(0.0, 1.0, ref_q.size)
    edges = np.interp(np.linspace(0.0, 1.0, bins + 1), grid, ref_q)

    # CDF current: перцентиль-функция обратима интерполяцией value→prob.
    # np.interp требует возрастающий x — схлопываем дубликаты значений.
    cur_vals, idx = np.unique(cur_q, return_index=True)
    cur_probs = np.linspace(0.0, 1.0, cur_q.size)[idx]

    def cdf_cur(x: float) -> float:
        return float(np.interp(x, cur_vals, cur_probs, left=0.0, right=1.0))

    psi = 0.0
    ref_pct = 1.0 / bins
    for i in range(bins):
        lo = cdf_cur(edges[i]) if i > 0 else 0.0
        hi = cdf_cur(edges[i + 1]) if i < bins - 1 else 1.0
        cur_pct = max(hi - lo, EPS)
        psi += (cur_pct - ref_pct) * np.log(cur_pct / ref_pct)
    return float(psi)


def compute_psi_per_feature_from_quantiles(
    ref: dict[str, list[float]],
    cur: dict[str, list[float]],
    bins: int = 10,
) -> dict[str, float]:
    """PSI по каждому общему признаку двух квантильных сводок."""
    return {
        col: compute_psi_from_quantiles(ref[col], cur[col], bins=bins)
        for col in ref
        if col in cur
    }
