"""ModelWatcher — polls the MLflow registry for the @Production version
and atomically swaps the in-memory model + SHAP explainer.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class LoadedModel:
    model: Any                # lightgbm.LGBMClassifier
    explainer: Any            # shap.TreeExplainer
    version: str
    feature_columns: list[str]


class ModelWatcher:
    """Background asyncio task that keeps the live model up-to-date."""

    PRODUCTION_STAGE: str = "Production"

    def __init__(
        self,
        mlflow_uri: str,
        model_name: str,
        *,
        poll_interval_seconds: int = 60,
    ) -> None:
        self._uri = mlflow_uri
        self._name = model_name
        self._poll = poll_interval_seconds
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._loaded: Optional[LoadedModel] = None

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Initial load + spawn the polling task."""
        try:
            await self._refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("initial_model_load_failed: %s", exc)
        self._task = asyncio.create_task(self._loop(), name="model-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def loaded(self) -> bool:
        return self._loaded is not None

    @property
    def version(self) -> str | None:
        return None if self._loaded is None else self._loaded.version

    async def get(self) -> LoadedModel | None:
        async with self._lock:
            return self._loaded

    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                return  # stop requested
            except TimeoutError:
                pass
            try:
                await self._refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("model_refresh_failed: %s", exc)

    async def _refresh(self) -> None:
        latest = await asyncio.to_thread(self._fetch_production_version)
        if latest is None:
            return
        if self._loaded is not None and latest.version == self._loaded.version:
            return
        log.info("model_refresh starting load version=%s", latest.version)
        loaded = await asyncio.to_thread(self._load_version, latest)
        async with self._lock:
            self._loaded = loaded
        log.info("model_refresh done version=%s", loaded.version)

    # ------------------------------------------------------------------
    def _client(self):
        import mlflow
        from mlflow.tracking import MlflowClient

        mlflow.set_tracking_uri(self._uri)
        return MlflowClient()

    def _fetch_production_version(self):
        client = self._client()
        versions = client.get_latest_versions(self._name, stages=[self.PRODUCTION_STAGE])
        return versions[0] if versions else None

    def _load_version(self, version) -> LoadedModel:
        import mlflow.lightgbm
        import shap

        model_uri = f"models:/{self._name}/{version.version}"
        model = mlflow.lightgbm.load_model(model_uri)

        # Pull the training fingerprint so we know the feature order.
        feature_columns: list[str] = []
        try:
            client = self._client()
            with tempfile.TemporaryDirectory() as tmp:
                local = client.download_artifacts(
                    version.run_id, "training_fingerprint.json", tmp
                )
                import json
                payload = json.loads(Path(local).read_text(encoding="utf-8"))
                feature_columns = list(payload.get("feature_columns", []))
        except Exception as exc:  # noqa: BLE001
            log.warning("fingerprint_unavailable: %s", exc)

        explainer = shap.TreeExplainer(model)
        return LoadedModel(
            model=model,
            explainer=explainer,
            version=str(version.version),
            feature_columns=feature_columns,
        )
