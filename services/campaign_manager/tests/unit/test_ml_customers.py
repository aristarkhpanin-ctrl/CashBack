"""Unit tests for GET /ml/customers roster (phase 25)."""
from __future__ import annotations

import uuid

import pytest
from app.api.ml_explain import ml_customers


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _MappingsResult(self._rows)


@pytest.mark.asyncio
async def test_maps_segment_bucket_and_prediction():
    uid = str(uuid.uuid4())
    rows = [
        {"customer_id": uid, "external_id": "u-1", "segment_id": 10,
         "prediction": 0.812345},
        {"customer_id": str(uuid.uuid4()), "external_id": "u-2",
         "segment_id": 1, "prediction": 0.4},
    ]
    out = await ml_customers(limit=20, session=_FakeSession(rows))
    assert out[0].name == "u-1"
    assert out[0].segment == "Премиум"          # дециль 10 → premium
    assert out[0].prediction == 0.8123          # округление до 4 знаков
    assert out[1].segment == "Бизнес"           # дециль 1 → business


@pytest.mark.asyncio
async def test_missing_external_id_falls_back_to_short_uuid():
    uid = str(uuid.uuid4())
    rows = [{"customer_id": uid, "external_id": None,
             "segment_id": None, "prediction": None}]
    out = await ml_customers(limit=20, session=_FakeSession(rows))
    assert out[0].name == f"Клиент {uid[:8]}"
    assert out[0].segment == "—"                # нет сегмента
    assert out[0].prediction == 0.0
