"""Unit tests for DELETE /campaigns/{id} (phase 23).

RBAC (только ADMIN) обеспечивает зависимость ``require_role`` — она покрыта
в test_security. Здесь — доменная логика хендлера: 404 для отсутствующей,
409 для ACTIVE, успех для остальных статусов (с каскадным DELETE + commit).
"""
from __future__ import annotations

import uuid

import pytest
from app.api.campaigns import delete_campaign
from fastapi import HTTPException


class _Row:
    def __init__(self, status: str):
        self.status = status
        self.campaign_id = uuid.uuid4()


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.executed = False
        self.committed = False

    async def get(self, _model, _cid):
        return self._row

    async def execute(self, _stmt):
        self.executed = True

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_delete_missing_returns_404():
    session = _FakeSession(None)
    with pytest.raises(HTTPException) as exc:
        await delete_campaign(uuid.uuid4(), session=session)
    assert exc.value.status_code == 404
    assert not session.executed


@pytest.mark.asyncio
async def test_delete_active_returns_409():
    session = _FakeSession(_Row("ACTIVE"))
    with pytest.raises(HTTPException) as exc:
        await delete_campaign(uuid.uuid4(), session=session)
    assert exc.value.status_code == 409
    assert not session.executed        # ничего не удалено
    assert not session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["DRAFT", "PAUSED", "COMPLETED"])
async def test_delete_non_active_succeeds(status):
    row = _Row(status)
    session = _FakeSession(row)
    result = await delete_campaign(row.campaign_id, session=session)
    assert result is None
    assert session.executed            # каскадный DELETE выполнен
    assert session.committed
