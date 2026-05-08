# 0009 — MLflow for the ML lifecycle

* **Status:** Accepted
* **Date:** 2025-12-19

## Context

We need to track experiment runs (parameters / metrics / artefacts),
register trained models with stage transitions (`Staging` →
`Production`), serve them to the recommendation API in production with
zero-downtime swap, and audit which model produced which decisions.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **MLflow 2.13** | Open-source, single-binary tracking + registry, native LightGBM flavour, sklearn-flavour for SVD++ when needed, REST API | UX of the Production tag transitions feels dated. |
| Weights & Biases | Polished UX. | Hosted-only / paid; not deployable on-prem easily. |
| Custom Postgres table | Full control. | We'd reimplement everything; no UI. |
| ClearML / Neptune | Comparable to W&B. | Same lock-in / cost concerns. |

## Decision

**MLflow 2.13** as a single tracking + registry server. Local stack runs
`ghcr.io/mlflow/mlflow:v2.13.0` with sqlite backend on a named volume.
Production deployments point at a managed MLflow with Postgres backend +
S3 artefact store.

What we log per training run (`services/ml_training/app/trainer.py`):

* hyperparameters (`PARAMS` from listing 3.7);
* metrics: `cv_roc_auc`, `cv_roc_auc_std`, `cv_fold_<k>_roc_auc`,
  `train_roc_auc`;
* artefacts: `lgbm_ranker/` model, `shap_top20.json`,
  `training_fingerprint.json`;
* `registered_model_name = cashback_lgbm_ranker`;
* SVD++ runs publish `embeddings/` + `index/items.index`.

Promotion: `promote_if_better` (listing 3.8) issues
`client.transition_model_version_stage(stage="Production",
archive_existing_versions=True)` once both AUC delta and PSI gates pass.

Inference side (`services/recommendation_api/app/model_registry.py`):

* `ModelWatcher` is an asyncio-task that polls
  `MlflowClient.get_latest_versions(model_name, stages=["Production"])`
  every 60 s.
* Atomic swap under `asyncio.Lock` — old workers continue serving
  inflight requests, then pick up the new model on next call.

## Consequences

* + Auditable: every recommendation can be linked to a specific
  `model_version` (returned in `RecommendationResponse.model_version`).
* + Zero-downtime model rollouts (60-s convergence in the worst case).
* + ML training is a CronJob (`helm/cashback/templates/cronjob-ml-retrain.yaml`)
  with `concurrencyPolicy: Forbid` — never two trainings at once.
* − Sqlite backend is dev-only; we explicitly mark this in the README.
* − Artefact store uses local volume in dev; needs S3 in production.

## References

* `services/ml_training/app/trainer.py` (logs) +
  `services/ml_training/app/promote_model.py` (transitions).
* `services/recommendation_api/app/model_registry.py` — `ModelWatcher`.
* `helm/cashback/templates/cronjob-ml-retrain.yaml` — production schedule.
* `services/etl/dags/ml_retrain_pipeline.py` — Airflow-driven retrain.
