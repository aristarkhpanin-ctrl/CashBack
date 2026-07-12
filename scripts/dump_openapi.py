#!/usr/bin/env python3
"""Дамп OpenAPI-схемы сервиса БЕЗ запуска инфраструктуры (фаза 19).

``app.openapi()`` не поднимает lifespan (нет DB/Redis/Kafka) — нужны
только импортируемые модули сервиса. Используется CI-джобом api-types-drift
для генерации TS-типов и проверки рассинхрона контракта.

Пока офлайн-дамп ограничен campaign_manager: recommendation_api тянет
faiss, mobile_api — tenacity; фронтенд опирается прежде всего на
campaign_manager (кампании, аналитика, auth, ml-лимиты, эксперименты, SSE).
Живой fetch всех трёх схем остаётся в scripts/generate-api-types.sh.

Usage:
    python scripts/dump_openapi.py campaign_manager > out.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dump(service: str) -> dict:
    svc_dir = ROOT / "services" / service
    if not svc_dir.exists():
        raise SystemExit(f"service not found: {svc_dir}")
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
