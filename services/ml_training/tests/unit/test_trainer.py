"""Unit tests for LGBMTrainer / SVDPPTrainer / PSI / promote.

MLflow is fully mocked so the tests don't need a tracking server.
LightGBM is exercised on a tiny synthetic dataset to keep the suite fast.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def tiny_dataset() -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(42)
    n = 240
    X = pd.DataFrame(rng.random((n, 6)),
                     columns=[f"f{i}" for i in range(6)])
    # Slight signal so LightGBM converges and AUC > 0.5.
    y = pd.Series(((X["f0"] + X["f1"] - rng.random(n)) > 0.6).astype(int))
    return X, y


# ---------------------------------------------------------------------------
# PARAMS sanity (Listing 3.7 verbatim)
# ---------------------------------------------------------------------------
def test_params_match_listing_3_7():
    from app.trainer import PARAMS

    assert PARAMS["objective"] == "binary"
    assert PARAMS["metric"] == "auc"
    assert PARAMS["boosting_type"] == "gbdt"
    assert PARAMS["num_leaves"] == 127
    assert PARAMS["max_depth"] == 8
    assert PARAMS["learning_rate"] == 0.05
    assert PARAMS["n_estimators"] == 800
    assert PARAMS["min_child_samples"] == 50
    assert PARAMS["subsample"] == 0.8
    assert PARAMS["colsample_bytree"] == 0.7
    assert PARAMS["reg_alpha"] == 0.1
    assert PARAMS["reg_lambda"] == 1.0


def test_trainer_constants():
    from app.trainer import LGBMTrainer

    assert LGBMTrainer.CV_FOLDS == 5
    assert LGBMTrainer.EARLY_STOPPING_ROUNDS == 50
    assert LGBMTrainer.SHAP_SAMPLE_SIZE == 1000
    assert LGBMTrainer.REGISTERED_MODEL_NAME == "cashback_lgbm_ranker"


def test_scale_pos_weight_for_imbalanced_set():
    from app.trainer import LGBMTrainer

    y = pd.Series([0] * 90 + [1] * 10)
    weight = LGBMTrainer._scale_pos_weight(y)
    assert weight == pytest.approx(9.0)
    # All-positive degenerate case — falls back to 1.0.
    assert LGBMTrainer._scale_pos_weight(pd.Series([1, 1, 1])) == 1.0


# ---------------------------------------------------------------------------
# Full pipeline with mocked MLflow / SHAP
# ---------------------------------------------------------------------------
@patch("app.trainer.shap")
@patch("app.trainer.mlflow")
def test_train_invokes_full_mlflow_pipeline(mock_mlflow, mock_shap, tiny_dataset):
    from app.trainer import LGBMTrainer

    X, y = tiny_dataset

    run_id = "run-xyz-123"
    mock_run = MagicMock()
    mock_run.info.run_id = run_id
    mock_mlflow.start_run.return_value.__enter__.return_value = mock_run

    explainer = MagicMock()
    explainer.shap_values.return_value = np.random.default_rng(0).random((50, X.shape[1]))
    mock_shap.TreeExplainer.return_value = explainer

    trainer = LGBMTrainer()
    out = trainer.train(X, y, experiment="exp-test")

    assert out == run_id
    mock_mlflow.set_experiment.assert_called_once_with("exp-test")
    mock_mlflow.start_run.assert_called_once()
    mock_mlflow.log_params.assert_called()
    # cv_roc_auc / cv_roc_auc_std / per-fold metrics + train_roc_auc.
    metric_names = {
        c.args[0] for c in mock_mlflow.log_metric.call_args_list
    }
    assert "cv_roc_auc" in metric_names
    assert "cv_roc_auc_std" in metric_names
    assert "train_roc_auc" in metric_names
    # 5 fold-level metrics logged.
    assert sum(1 for n in metric_names if n.startswith("cv_fold_")) == 5

    mock_mlflow.lightgbm.log_model.assert_called_once()
    kwargs = mock_mlflow.lightgbm.log_model.call_args.kwargs
    assert kwargs["registered_model_name"] == "cashback_lgbm_ranker"
    assert kwargs["artifact_path"] == "lgbm_ranker"

    # SHAP top-20 logged as a JSON artefact.
    log_dict_calls = mock_mlflow.log_dict.call_args_list
    paths = [c.args[1] for c in log_dict_calls]
    assert "shap_top20.json" in paths
    assert "training_fingerprint.json" in paths


# ---------------------------------------------------------------------------
# PSI
# ---------------------------------------------------------------------------
def test_psi_zero_for_identical_distributions():
    from app.psi import compute_psi

    rng = np.random.default_rng(0)
    sample = rng.normal(size=2000)
    assert compute_psi(sample, sample) == pytest.approx(0.0, abs=1e-6)


def test_psi_positive_when_distribution_shifts():
    from app.psi import compute_psi

    rng = np.random.default_rng(0)
    ref = rng.normal(loc=0.0, size=2000)
    cur = rng.normal(loc=2.0, size=2000)   # shifted right
    assert compute_psi(ref, cur) > 0.2


def test_psi_handles_constant_arrays():
    from app.psi import compute_psi

    assert compute_psi([1.0] * 100, [1.0] * 100) == 0.0


# ---------------------------------------------------------------------------
# SVDPPTrainer.build_matrix
# ---------------------------------------------------------------------------
def test_svd_build_matrix_shape_and_nnz():
    from app.svd_trainer import SVDPPTrainer, TOP50_MCC

    n_users = 5
    cols = ["user_id"] + [f"freq_{m}" for m in TOP50_MCC] + [f"amt_{m}" for m in TOP50_MCC]
    df = pd.DataFrame(0, index=range(n_users), columns=cols, dtype=float)
    df["user_id"] = [f"u-{i}" for i in range(n_users)]
    # Three users have a non-zero (freq, amt) pair on different MCCs.
    df.loc[0, ["freq_5411", "amt_5411"]] = [3, 1500.0]
    df.loc[1, ["freq_5812", "amt_5812"]] = [2, 600.0]
    df.loc[2, ["freq_5541", "amt_5541"]] = [1, 2500.0]

    matrix, users, items = SVDPPTrainer.build_matrix(df)
    assert matrix.shape == (n_users, len(TOP50_MCC))
    assert matrix.nnz == 3
    assert users == ["u-0", "u-1", "u-2", "u-3", "u-4"]
    assert "5411" in items and "5812" in items and "5541" in items


def test_svd_build_matrix_empty_df_raises():
    from app.svd_trainer import SVDPPTrainer

    with pytest.raises(ValueError):
        SVDPPTrainer.build_matrix(pd.DataFrame())


# ---------------------------------------------------------------------------
# promote_if_better — pure orchestration, MlflowClient mocked
# ---------------------------------------------------------------------------
@patch("app.promote_model.MlflowClient")
def test_promote_when_no_production_model(mock_client_cls):
    from app.promote_model import promote_if_better

    client = MagicMock()
    mock_client_cls.return_value = client
    client.get_run.return_value = SimpleNamespace(
        data=SimpleNamespace(metrics={"cv_roc_auc": 0.74})
    )
    client.search_model_versions.return_value = [
        SimpleNamespace(name="cashback_lgbm_ranker", version="1", run_id="r1")
    ]
    client.get_latest_versions.return_value = []      # no production yet

    promoted = promote_if_better("r1")
    assert promoted is True
    client.transition_model_version_stage.assert_called_once()
    kwargs = client.transition_model_version_stage.call_args.kwargs
    assert kwargs["stage"] == "Production"
    assert kwargs["archive_existing_versions"] is False


@patch("app.promote_model.MlflowClient")
def test_promote_skipped_when_auc_delta_too_small(mock_client_cls):
    from app.promote_model import promote_if_better

    client = MagicMock()
    mock_client_cls.return_value = client
    client.get_run.side_effect = [
        SimpleNamespace(data=SimpleNamespace(metrics={"cv_roc_auc": 0.7510})),  # new
        SimpleNamespace(data=SimpleNamespace(metrics={"cv_roc_auc": 0.7500})),  # prod
    ]
    client.search_model_versions.return_value = [
        SimpleNamespace(name="cashback_lgbm_ranker", version="2", run_id="r2")
    ]
    client.get_latest_versions.return_value = [
        SimpleNamespace(name="cashback_lgbm_ranker", version="1", run_id="r1")
    ]

    promoted = promote_if_better("r2")  # delta=0.001, threshold=0.005
    assert promoted is False
    client.transition_model_version_stage.assert_not_called()


@patch("app.promote_model._load_fingerprint", return_value=pd.DataFrame())
@patch("app.promote_model.MlflowClient")
def test_promote_when_auc_beats_threshold(mock_client_cls, _mock_fp):
    from app.promote_model import promote_if_better

    client = MagicMock()
    mock_client_cls.return_value = client
    client.get_run.side_effect = [
        SimpleNamespace(data=SimpleNamespace(metrics={"cv_roc_auc": 0.78})),  # new
        SimpleNamespace(data=SimpleNamespace(metrics={"cv_roc_auc": 0.75})),  # prod
    ]
    client.search_model_versions.return_value = [
        SimpleNamespace(name="cashback_lgbm_ranker", version="3", run_id="r3")
    ]
    client.get_latest_versions.return_value = [
        SimpleNamespace(name="cashback_lgbm_ranker", version="1", run_id="r-prev")
    ]

    promoted = promote_if_better("r3")
    assert promoted is True
    kwargs = client.transition_model_version_stage.call_args.kwargs
    assert kwargs["stage"] == "Production"
    assert kwargs["archive_existing_versions"] is True


def test_promote_constants():
    from app.promote_model import AUC_DELTA_MIN, MODEL_NAME, PSI_THRESHOLD

    assert MODEL_NAME == "cashback_lgbm_ranker"
    assert AUC_DELTA_MIN == 0.005
    assert PSI_THRESHOLD == 0.2
