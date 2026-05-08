# 0007 — LightGBM as the production ranker

* **Status:** Accepted
* **Date:** 2025-12-19
* **Driver:** chapter 2.1 — algorithm comparison

## Context

The system needs a ranker that consumes ~108-dimensional dense feature
vectors plus a categorical MCC, and predicts the probability that a
user will accept a cashback recommendation. The ranker is on the hot
path (called on every `/recommendations/{user_id}` request) so its
inference must finish well under 50 ms for 20 candidates. Training is
nightly / weekly on labelled `recommendations.response_status` data.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **LightGBM 4.3** (GBDT) | Industry-standard for tabular ranking; fast inference; handles missing values; `early_stopping`; SHAP-friendly; PyPI binary wheel | Tree-based — needs careful CV; large model size for many trees |
| XGBoost | Same family; well documented. | Slower training; comparable quality. |
| CatBoost | Native categorical handling; Russian engineering. | Slower inference; bigger memory footprint. |
| Logistic regression | Interpretable; trivial inference. | Doesn't capture interactions of 108 features without manual feature crosses. |
| TabNet / DLRM (deep) | State-of-the-art on some tabular benchmarks. | Operational cost; longer training; harder to explain to a credit committee. |

## Decision

**LightGBM 4.3** with the parameter set from listing 3.7 of the thesis:

```python
PARAMS = {
    "objective":      "binary",
    "metric":         "auc",
    "boosting_type":  "gbdt",
    "num_leaves":     127,
    "max_depth":      8,
    "learning_rate":  0.05,
    "n_estimators":   800,
    "min_child_samples": 50,
    "subsample":      0.8,
    "colsample_bytree": 0.7,
    "reg_alpha":      0.1,
    "reg_lambda":     1.0,
}
```

Training pipeline (`services/ml_training/app/trainer.py`):

* `StratifiedKFold(n_splits=5, shuffle=True, random_state=42)`
* `early_stopping(50)` per fold
* `scale_pos_weight = neg / pos` (auto-balance)
* final fit on the full dataset
* `mlflow.lightgbm.log_model(registered_model_name="cashback_lgbm_ranker")`
* SHAP top-20 attributions logged as `shap_top20.json`
* `training_fingerprint.json` with feature columns for PSI gate

Promotion: `promote_if_better` (listing 3.8) only swaps to `Production`
if `cv_roc_auc - prev_cv_roc_auc >= 0.005` AND
`max(PSI_per_feature) <= 0.2`.

## Consequences

* + Inference is microseconds per row → a batch of 20 candidates is
  comfortably under 1 ms; the bottleneck of the API is feature-store
  lookup (~3 ms warm), not the model.
* + SHAP gives explainability — surfaced verbatim in the
  `/recommendations/{rec_id}/explain` page (frontend ShapExplanationCard).
* + LightGBM models pickle small (≤ 5 MB) — fast pull from MLflow
  registry on `ModelWatcher` refresh.
* − Tree-based ⇒ no native online updates (we retrain nightly /
  weekly). Acceptable: cashback dynamics are slow.
* − Hyperparameter search is offline; no Optuna in production loop.
  We ship Optuna in `pyproject.toml` for future experimentation.

## References

* `services/ml_training/app/trainer.py` — listing 3.7 verbatim.
* `services/ml_training/app/promote_model.py` — listing 3.8.
* `services/ml_training/tests/unit/test_trainer.py` — 13 unit tests
  pinning `PARAMS` to the thesis values (regression guard).
* `services/recommendation_api/app/model_registry.py` — `ModelWatcher`.
