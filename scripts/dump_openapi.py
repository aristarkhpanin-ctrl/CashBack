#!/usr/bin/env python3
"""Дамп OpenAPI-схемы сервиса БЕЗ запуска инфраструктуры и тяжёлых зависимостей.

``app.openapi()`` не поднимает lifespan (нет DB/Redis/Kafka) — нужны
только импортируемые модули сервиса. Тяжёлые рантайм-зависимости
(``faiss`` в recommendation_api, ``tenacity`` в mobile_api) для СХЕМЫ не
нужны — они лишь `import`-ятся на уровне модуля, но вызываются в рантайме.
Поэтому здесь они подменяются лёгкими стабами: CI-джоб api-types-drift
генерирует типы всех трёх сервисов без установки faiss/tenacity.

Usage:
    python scripts/dump_openapi.py campaign_manager > out.json
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_stubs() -> None:
    """Подменяем тяжёлые модули на no-op стабы (только для генерации схемы)."""
    if "faiss" not in sys.modules:
        faiss = types.ModuleType("faiss")
        for name in ("IndexFlatIP", "read_index", "write_index",
                     "normalize_L2", "IndexIDMap"):
            setattr(faiss, name, lambda *a, **k: None)
        sys.modules["faiss"] = faiss
    if "tenacity" not in sys.modules:
        tenacity = types.ModuleType("tenacity")
        for name in ("AsyncRetrying", "retry_if_exception_type",
                     "stop_after_attempt", "wait_exponential_jitter",
                     "Retrying", "retry", "stop_after_delay", "wait_fixed"):
            setattr(tenacity, name, lambda *a, **k: None)
        sys.modules["tenacity"] = tenacity


def dump(service: str) -> dict:
    svc_dir = ROOT / "services" / service
    if not svc_dir.exists():
        raise SystemExit(f"service not found: {svc_dir}")
    _install_stubs()
    sys.path.insert(0, str(svc_dir))
    from app.main import create_app  # noqa: E402

    return create_app().openapi()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    spec = dump(sys.argv[1])
    json.dump(spec, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
