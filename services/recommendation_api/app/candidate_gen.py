"""CandidateGenerator — FAISS-backed retrieval over the SVD++ artifacts.

Loads ``user_embeddings.npy``, ``item_embeddings.npy``, ``user_ids.txt``,
``item_codes.txt`` and ``items.index`` from the latest SVD++ MLflow run
during application startup.  At inference time, ``retrieve(user_id, k)``
returns the top-``k`` MCC codes whose item-embedding has the highest
inner product with the user's user-embedding.

A safe fall-back returns the ``POPULAR_FALLBACK`` MCC list for users not
seen during SVD++ training.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import faiss
import numpy as np

log = logging.getLogger(__name__)


# Default popular MCC fallback — overlaps with the explicit weights in the
# TX simulator so cold-start users get a reasonable list out of the box.
POPULAR_FALLBACK: tuple[str, ...] = (
    "5411", "5812", "5541", "5912", "5732", "5311", "5814", "5651",
    "5921", "5942", "4111", "4121", "5462", "5499", "7011", "7832",
    "5611", "5641", "5722", "5712",
)


class CandidateGenerator:
    """Retrieves top-k candidate MCCs for a user."""

    def __init__(self, popular: tuple[str, ...] = POPULAR_FALLBACK) -> None:
        self._user_emb: np.ndarray | None = None
        self._item_emb: np.ndarray | None = None
        self._index: faiss.Index | None = None
        self._user_to_idx: dict[str, int] = {}
        self._items: list[str] = []
        self._popular: tuple[str, ...] = popular
        self._loaded: bool = False

    # ------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def n_users(self) -> int:
        return len(self._user_to_idx)

    @property
    def n_items(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------------
    def _load_from_dir(self, root: Path) -> None:
        user_emb = np.load(root / "embeddings" / "user_embeddings.npy")
        item_emb = np.load(root / "embeddings" / "item_embeddings.npy")
        users = (root / "embeddings" / "user_ids.txt").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        items = (root / "embeddings" / "item_codes.txt").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        index = faiss.read_index(str(root / "index" / "items.index"))

        self._user_emb = user_emb.astype(np.float32, copy=False)
        self._item_emb = item_emb.astype(np.float32, copy=False)
        self._index = index
        self._user_to_idx = {u: i for i, u in enumerate(users)}
        self._items = items
        self._loaded = True
        log.info(
            "candidate_gen_loaded users=%d items=%d dim=%d",
            len(self._user_to_idx), len(self._items), self._user_emb.shape[1],
        )

    async def load_from_mlflow(
        self,
        mlflow_client: Any,
        run_id: str,
    ) -> None:
        """Download the SVD++ artefacts of ``run_id`` and warm the index."""
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(mlflow_client.download_artifacts(run_id, "", tmp))
            self._load_from_dir(local)

    # ------------------------------------------------------------------
    def retrieve(self, user_id: str, k: int = 20) -> list[str]:
        """Return the top-``k`` candidate MCC codes for ``user_id``.

        Cold-start path: returns the configured popular fallback.
        """
        if not self._loaded or self._index is None or self._user_emb is None:
            return list(self._popular[:k])

        idx = self._user_to_idx.get(str(user_id))
        if idx is None:
            return list(self._popular[:k])

        vec = self._user_emb[idx : idx + 1].copy()
        # Normalise to match the FAISS index that uses inner-product search.
        faiss.normalize_L2(vec)
        n = min(k, self._index.ntotal)
        _scores, ids = self._index.search(vec, n)
        return [self._items[i] for i in ids[0] if 0 <= i < len(self._items)]
