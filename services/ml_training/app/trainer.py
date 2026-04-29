"""LGBMTrainer — chapter 3.1, listing 3.7.

Cross-validated LightGBM ranker training with MLflow tracking and
SHAP-based feature attribution.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

import numpy as np
import pandas as pd
import structlog

import mlflow
import mlflow.lightgbm
import shap
import lightgbm as lgb
from lightgbm import LGBMClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold

log = structlog.get_logger("ml.trainer")


# LightGBM hyperparameters straight from listing 3.7.
PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "auc",
    "boosting_type": "gbdt",
    "num_leaves": 127,
    "max_depth": 8,
    "learning_rate": 0.05,
    "n_estimators": 800,
    "min_child_samples": 50,
    "subsample": 0.8,
    "colsample_bytree": 0.7,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "random_state": 42,
    "n_jobs": -1,
    "verbose": -1,
}

REGISTERED_MODEL_NAME = "cashback_lgbm_ranker"
ARTIFACT_PATH = "lgbm_ranker"


class LGBMTrainer:
    PARAMS = PARAMS
    REGISTERED_MODEL_NAME = REGISTERED_MODEL_NAME
    EARLY_STOPPING_ROUNDS: int = 50
    CV_FOLDS: int = 5
    SHAP_SAMPLE_SIZE: int = 1000
    SHAP_TOP_N: int = 20

    def __init__(
        self,
        mlflow_tracking_uri: str | None = None,
        registered_model_name: str = REGISTERED_MODEL_NAME,
    ) -> None:
        self.mlflow_tracking_uri = mlflow_tracking_uri
        self.registered_model_name = registered_model_name

    # ------------------------------------------------------------------
    @staticmethod
    def _scale_pos_weight(y: pd.Series) -> float:
        positives = int((y == 1).sum())
        negatives = int((y == 0).sum())
        if positives == 0 or negatives == 0:
            return 1.0
        return float(negatives) / float(positives)

    @classmethod
    def _build_params(cls, y: pd.Series) -> dict[str, Any]:
        params = dict(cls.PARAMS)
        params["scale_pos_weight"] = cls._scale_pos_weight(y)
        return params

    # ------------------------------------------------------------------
    def _run_cv(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        params: dict[str, Any],
    ) -> list[float]:
        skf = StratifiedKFold(
            n_splits=self.CV_FOLDS, shuffle=True, random_state=42
        )
        aucs: list[float] = []
        for fold, (tr_idx, va_idx) in enumerate(skf.split(X, y), start=1):
            X_tr, X_va = X.iloc[tr_idx], X.iloc[va_idx]
            y_tr, y_va = y.iloc[tr_idx], y.iloc[va_idx]
            model = LGBMClassifier(**params)
            model.fit(
                X_tr, y_tr,
                eval_set=[(X_va, y_va)],
                eval_metric="auc",
                callbacks=[lgb.early_stopping(self.EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            preds = model.predict_proba(X_va)[:, 1]
            auc = float(roc_auc_score(y_va, preds))
            aucs.append(auc)
            log.info("cv_fold", fold=fold, roc_auc=auc)
        return aucs

    # ------------------------------------------------------------------
    def _shap_top_features(
        self, model: LGBMClassifier, X: pd.DataFrame,
    ) -> dict[str, float]:
        sample_n = min(self.SHAP_SAMPLE_SIZE, len(X))
        sample = X.sample(sample_n, random_state=0)
        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(sample)
        # For binary models, shap_values may be a 2-element list (neg, pos).
        if isinstance(shap_values, list):
            shap_values = shap_values[1]
        importance = np.abs(shap_values).mean(axis=0)
        ranked = sorted(
            zip(X.columns.tolist(), importance.tolist()),
            key=lambda kv: kv[1],
            reverse=True,
        )
        top = dict(ranked[: self.SHAP_TOP_N])
        return {k: float(v) for k, v in top.items()}

    # ------------------------------------------------------------------
    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        experiment: str = "cashback_ranker",
    ) -> str:
        if self.mlflow_tracking_uri:
            mlflow.set_tracking_uri(self.mlflow_tracking_uri)
        mlflow.set_experiment(experiment)

        params = self._build_params(y)
        log.info(
            "training_start",
            experiment=experiment,
            rows=len(X),
            features=X.shape[1],
            positive_rate=float(y.mean()),
        )

        with mlflow.start_run() as run:
            run_id = run.info.run_id

            # ---------- 5-fold CV ---------------------------------------------
            aucs = self._run_cv(X, y, params)
            cv_mean = float(np.mean(aucs))
            cv_std = float(np.std(aucs))
            log.info("cv_done", roc_auc_mean=cv_mean, roc_auc_std=cv_std)

            # ---------- final fit on full set ---------------------------------
            final_model = LGBMClassifier(**params)
            final_model.fit(X, y, eval_metric="auc")
            train_preds = final_model.predict_proba(X)[:, 1]
            train_auc = float(roc_auc_score(y, train_preds))

            # ---------- MLflow tracking ---------------------------------------
            mlflow.log_params(params)
            mlflow.log_param("cv_folds", self.CV_FOLDS)
            mlflow.log_param("early_stopping_rounds", self.EARLY_STOPPING_ROUNDS)
            mlflow.log_param("n_features", X.shape[1])
            mlflow.log_param("n_samples", len(X))
            mlflow.log_metric("cv_roc_auc", cv_mean)
            mlflow.log_metric("cv_roc_auc_std", cv_std)
            mlflow.log_metric("train_roc_auc", train_auc)
            for i, auc in enumerate(aucs, start=1):
                mlflow.log_metric(f"cv_fold_{i}_roc_auc", auc)

            mlflow.lightgbm.log_model(
                final_model,
                artifact_path=ARTIFACT_PATH,
                registered_model_name=self.registered_model_name,
            )

            # ---------- SHAP top-20 -------------------------------------------
            top20 = self._shap_top_features(final_model, X)
            mlflow.log_dict(top20, "shap_top20.json")
            log.info("shap_top20_logged", n=len(top20))

            # ---------- training-data fingerprint -----------------------------
            fingerprint = {
                "n_samples": int(len(X)),
                "n_features": int(X.shape[1]),
                "positive_rate": float(y.mean()),
                "scale_pos_weight": params["scale_pos_weight"],
                "feature_columns": list(X.columns),
            }
            mlflow.log_dict(fingerprint, "training_fingerprint.json")

            log.info("training_complete", run_id=run_id, cv_roc_auc=cv_mean)
        return run_id
