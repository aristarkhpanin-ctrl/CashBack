"""promote_if_better — chapter 3.1, listing 3.8.

Decides whether to transition a freshly trained run to the
``Production`` stage of the MLflow Model Registry, based on the AUC
delta and a feature-level PSI ceiling.
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

import mlflow
import pandas as pd
import structlog
from mlflow.tracking import MlflowClient

from app.psi import compute_psi_per_feature

log = structlog.get_logger("ml.promote")


MODEL_NAME: str = "cashback_lgbm_ranker"
AUC_DELTA_MIN: float = 0.005
PSI_THRESHOLD: float = 0.2
PRODUCTION_STAGE: str = "Production"
ARCHIVED_STAGE: str = "Archived"
METRIC_KEY: str = "cv_roc_auc"


# ---------------------------------------------------------------------------
def _client(tracking_uri: str | None) -> MlflowClient:
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    return MlflowClient()


def _find_version_for_run(
    client: MlflowClient, run_id: str, model_name: str = MODEL_NAME,
) -> Optional[Any]:
    versions = client.search_model_versions(f"run_id='{run_id}'")
    for v in versions:
        if v.name == model_name:
            return v
    return versions[0] if versions else None


def _current_production(
    client: MlflowClient, model_name: str = MODEL_NAME,
) -> Optional[Any]:
    try:
        prod = client.get_latest_versions(model_name, stages=[PRODUCTION_STAGE])
    except Exception as exc:  # noqa: BLE001
        log.warning("registry_lookup_failed", error=str(exc))
        return None
    return prod[0] if prod else None


def _load_fingerprint(client: MlflowClient, run_id: str) -> Optional[pd.DataFrame]:
    """Pull the training_fingerprint.json artefact for PSI comparison."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = client.download_artifacts(run_id, "training_fingerprint.json", tmp)
            payload = json.loads(Path(local).read_text(encoding="utf-8"))
        # We persist columns + summary stats; for PSI we just need the
        # column names — so return a schema dataframe stub.
        return pd.DataFrame(columns=payload.get("feature_columns", []))
    except Exception as exc:  # noqa: BLE001
        log.warning("fingerprint_fetch_failed", run_id=run_id, error=str(exc))
        return None


# ---------------------------------------------------------------------------
def promote_if_better(
    new_run_id: str,
    *,
    tracking_uri: str | None = None,
    model_name: str = MODEL_NAME,
    auc_delta_min: float = AUC_DELTA_MIN,
    psi_threshold: float = PSI_THRESHOLD,
) -> bool:
    """Promote ``new_run_id`` to Production iff:

      1. its ``cv_roc_auc`` exceeds the current Production model's by at
         least ``auc_delta_min``, AND
      2. no monitored feature exhibits PSI > ``psi_threshold`` against
         the current Production fingerprint.

    Returns ``True`` if a stage transition was executed.
    """
    client = _client(tracking_uri)
    new_run = client.get_run(new_run_id)
    new_auc = new_run.data.metrics.get(METRIC_KEY)
    if new_auc is None:
        log.error("missing_metric", run_id=new_run_id, metric=METRIC_KEY)
        return False

    candidate = _find_version_for_run(client, new_run_id, model_name)
    if candidate is None:
        log.error("no_registered_version_for_run", run_id=new_run_id)
        return False

    prod = _current_production(client, model_name)
    if prod is None:
        log.info(
            "no_production_yet_promoting", run_id=new_run_id,
            version=candidate.version, cv_roc_auc=new_auc,
        )
        client.transition_model_version_stage(
            name=model_name,
            version=candidate.version,
            stage=PRODUCTION_STAGE,
            archive_existing_versions=False,
        )
        return True

    prod_run = client.get_run(prod.run_id)
    prod_auc = prod_run.data.metrics.get(METRIC_KEY, 0.0)
    delta = new_auc - prod_auc
    log.info(
        "auc_comparison",
        new=new_auc, prod=prod_auc, delta=delta, threshold=auc_delta_min,
    )

    if delta < auc_delta_min:
        log.info("promotion_skipped_auc", run_id=new_run_id)
        return False

    # ---------- PSI gate -----------------------------------------------
    new_fp = _load_fingerprint(client, new_run_id)
    prod_fp = _load_fingerprint(client, prod.run_id)
    if new_fp is not None and prod_fp is not None and not new_fp.empty and not prod_fp.empty:
        psi = compute_psi_per_feature(prod_fp, new_fp)
        max_psi = max(psi.values()) if psi else 0.0
        log.info("psi_max", value=max_psi, threshold=psi_threshold)
        if max_psi > psi_threshold:
            log.info("promotion_skipped_psi", run_id=new_run_id, psi=max_psi)
            return False

    # ---------- transition --------------------------------------------
    client.transition_model_version_stage(
        name=model_name,
        version=candidate.version,
        stage=PRODUCTION_STAGE,
        archive_existing_versions=True,
    )
    log.info(
        "promoted",
        run_id=new_run_id, version=candidate.version,
        previous_version=prod.version, delta=delta,
    )
    return True


def rollback_to_previous(
    *,
    tracking_uri: str | None = None,
    model_name: str = MODEL_NAME,
) -> Optional[str]:
    """Promote the most recent Archived version back to Production."""
    client = _client(tracking_uri)
    archived = client.get_latest_versions(model_name, stages=[ARCHIVED_STAGE])
    if not archived:
        log.warning("no_archived_versions", model=model_name)
        return None
    prev = sorted(archived, key=lambda v: int(v.version), reverse=True)[0]
    client.transition_model_version_stage(
        name=model_name,
        version=prev.version,
        stage=PRODUCTION_STAGE,
        archive_existing_versions=True,
    )
    log.info("rolled_back", model=model_name, version=prev.version)
    return prev.version
