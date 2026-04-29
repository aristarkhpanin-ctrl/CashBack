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
