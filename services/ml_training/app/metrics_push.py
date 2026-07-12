"""Публикация ML-метрик в Prometheus Pushgateway (фаза 18).

ml_training — одноразовый CLI-контейнер: scrape-эндпоинт держать некому,
поэтому метрики batch-джоба уходят push-моделью. Pushgateway поднимается
observability-стеком (`docker-compose.observability.yml`), Prometheus
скрейпит его с ``honor_labels: true``.

Отсутствие gateway не должно ронять обучение — все ошибки глотаются
с warning-логом.
"""
from __future__ import annotations

import os

import structlog
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

log = structlog.get_logger("ml.metrics")

JOB_NAME = "ml_training"


def push_ml_metrics(
    *,
    max_psi: float | None,
    promoted: bool,
    model_version: str | None = None,
    gateway_url: str | None = None,
) -> bool:
    """Отправить итог промоушена: PSI (алерт ModelDriftDetected) и вердикт."""
    url = gateway_url or os.getenv("PUSHGATEWAY_URL", "http://pushgateway:9091")
    registry = CollectorRegistry()

    if max_psi is not None:
        Gauge(
            "ml_last_psi",
            "max per-feature PSI of the candidate run vs production",
            registry=registry,
        ).set(round(float(max_psi), 6))

    Gauge(
        "ml_last_promotion_result",
        "1 if the last candidate was promoted to Production, else 0",
        registry=registry,
    ).set(1.0 if promoted else 0.0)

    if model_version:
        Gauge(
            "ml_production_model_version",
            "numeric version of the current Production model",
            registry=registry,
        ).set(float(model_version))

    try:
        push_to_gateway(url, job=JOB_NAME, registry=registry)
        log.info("ml_metrics_pushed", gateway=url,
                 max_psi=max_psi, promoted=promoted)
        return True
    except Exception as exc:  # noqa: BLE001 — метрики не важнее обучения
        log.warning("pushgateway_unavailable", gateway=url, error=str(exc))
        return False
