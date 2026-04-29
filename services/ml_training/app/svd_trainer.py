"""SVD++-style retrieval via implicit ALS over (user × MCC) confidences.

The matrix is constructed from the wide ``user_rfm_features`` table:
each user contributes one row of (freq_<mcc>, amt_<mcc>) pairs, which we
collapse into a confidence value ``freq * log(1 + amt)``.  The resulting
sparse matrix is decomposed into ``SVD_FACTORS``-dimensional embeddings
for users and items; an in-memory FAISS index over the item embeddings
is then persisted alongside as a retrieval index.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import faiss
import numpy as np
import pandas as pd
import structlog
from implicit.als import AlternatingLeastSquares
from scipy.sparse import csr_matrix

import mlflow

log = structlog.get_logger("ml.svd_trainer")


# Must match infrastructure/clickhouse/migrations/002 + services/etl/app/feature_eng.py
TOP50_MCC: tuple[str, ...] = (
    "5411", "5812", "5814", "5541", "5499", "5912", "5311", "5651",
    "5732", "5942", "5921", "5462", "4111", "4121", "4131", "4814",
    "4829", "4900", "7832", "7011", "7299", "7211", "7230", "7538",
    "7995", "8011", "8021", "8062", "8099", "8211", "8398", "8999",
    "5993", "5995", "5970", "5945", "5947", "5946", "5641", "5611",
    "5621", "5712", "5722", "5933", "5200", "5599", "5331", "5300",
    "4511", "5691",
)


class SVDPPTrainer:
    """ALS-based retrieval; outputs USER_EMB, ITEM_EMB and a FAISS index."""

    def __init__(
        self,
        factors: int = 64,
        iterations: int = 15,
        regularization: float = 0.05,
        mlflow_tracking_uri: str | None = None,
    ) -> None:
        self.factors = factors
        self.iterations = iterations
        self.regularization = regularization
        self.mlflow_tracking_uri = mlflow_tracking_uri

    # ------------------------------------------------------------------
    @staticmethod
    def build_matrix(
        df: pd.DataFrame,
        item_codes: tuple[str, ...] = TOP50_MCC,
    ) -> tuple[csr_matrix, list[str], list[str]]:
        """Construct a CSR ``(n_users × n_items)`` confidence matrix."""
        if df.empty:
            raise ValueError("RFM dataframe is empty")

        # Some columns may be missing if migration evolved — restrict to the
        # intersection.
        codes = [c for c in item_codes
                 if f"freq_{c}" in df.columns and f"amt_{c}" in df.columns]
        if not codes:
            raise ValueError("no freq_/amt_ columns found in RFM dataframe")

        users = df["user_id"].astype(str).tolist()
        n_users, n_items = len(users), len(codes)

        rows: list[int] = []
        cols: list[int] = []
        data: list[float] = []
        for ii, code in enumerate(codes):
            freq = pd.to_numeric(df[f"freq_{code}"], errors="coerce").fillna(0).to_numpy()
            amt  = pd.to_numeric(df[f"amt_{code}"],  errors="coerce").fillna(0).to_numpy()
            confidence = freq * np.log1p(amt)
            for ui in np.nonzero(confidence > 0)[0]:
                rows.append(int(ui))
                cols.append(int(ii))
                data.append(float(confidence[ui]))

        matrix = csr_matrix(
            (data, (rows, cols)),
            shape=(n_users, n_items),
            dtype=np.float32,
        )
        log.info(
            "matrix_built",
            users=n_users, items=n_items, nnz=matrix.nnz,
            density=float(matrix.nnz) / max(1, n_users * n_items),
        )
        return matrix, users, codes

    # ------------------------------------------------------------------
    def _fit_als(self, matrix: csr_matrix) -> AlternatingLeastSquares:
        model = AlternatingLeastSquares(
            factors=self.factors,
            iterations=self.iterations,
            regularization=self.regularization,
            calculate_training_loss=False,
            use_gpu=False,
        )
        model.fit(matrix, show_progress=False)
        return model

    # ------------------------------------------------------------------
    @staticmethod
    def _build_faiss_index(item_emb: np.ndarray) -> faiss.Index:
        emb = np.ascontiguousarray(item_emb, dtype=np.float32)
        faiss.normalize_L2(emb)
        index = faiss.IndexFlatIP(emb.shape[1])
        index.add(emb)
        return index

    # ------------------------------------------------------------------
    def train(
        self,
        rfm_df: pd.DataFrame,
        experiment: str = "cashback_retrieval",
    ) -> str:
        if self.mlflow_tracking_uri:
            mlflow.set_tracking_uri(self.mlflow_tracking_uri)
        mlflow.set_experiment(experiment)

        matrix, users, items = self.build_matrix(rfm_df)
        log.info(
            "training_start",
            factors=self.factors, iterations=self.iterations,
            users=len(users), items=len(items),
        )

        with mlflow.start_run() as run:
            run_id = run.info.run_id
            mlflow.log_params(
                {
                    "factors": self.factors,
                    "iterations": self.iterations,
                    "regularization": self.regularization,
                    "n_users": len(users),
                    "n_items": len(items),
                    "nnz": int(matrix.nnz),
                    "algorithm": "implicit.AlternatingLeastSquares",
                }
            )

            model = self._fit_als(matrix)
            user_emb = np.asarray(model.user_factors)
            item_emb = np.asarray(model.item_factors)
            log.info(
                "als_done",
                user_emb=tuple(user_emb.shape),
                item_emb=tuple(item_emb.shape),
            )

            # ---------- artefacts ---------------------------------------
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                user_path = tmp_dir / "user_embeddings.npy"
                item_path = tmp_dir / "item_embeddings.npy"
                users_path = tmp_dir / "user_ids.txt"
                items_path = tmp_dir / "item_codes.txt"
                index_path = tmp_dir / "items.index"

                np.save(user_path, user_emb)
                np.save(item_path, item_emb)
                users_path.write_text("\n".join(users), encoding="utf-8")
                items_path.write_text("\n".join(items), encoding="utf-8")

                index = self._build_faiss_index(item_emb)
                faiss.write_index(index, str(index_path))

                mlflow.log_artifact(str(user_path),  "embeddings")
                mlflow.log_artifact(str(item_path),  "embeddings")
                mlflow.log_artifact(str(users_path), "embeddings")
                mlflow.log_artifact(str(items_path), "embeddings")
                mlflow.log_artifact(str(index_path), "index")

            mlflow.log_metric("user_emb_norm_mean", float(np.linalg.norm(user_emb, axis=1).mean()))
            mlflow.log_metric("item_emb_norm_mean", float(np.linalg.norm(item_emb, axis=1).mean()))
            log.info("svdpp_complete", run_id=run_id)

        return run_id
