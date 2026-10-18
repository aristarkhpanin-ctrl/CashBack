"""DELETE /campaigns/{id} и POST /campaigns/{id}/archive (этап C, B24).

Право ``campaigns_delete`` проверяет зависимость (test_route_permissions).
Здесь — доменная логика обработчиков: удаляется только черновик без
истории, всё остальное — 409 «архивируйте»; архив — только PAUSED и
COMPLETED, условие на статус — в самом UPDATE. Тот же путь на настоящем
Postgres (FK RESTRICT, журнал начислений цел) —
tests/integration/test_money_integrity.py.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from app.api.campaigns import archive_campaign, delete_campaign
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Delete

from tests.unit.fakes import ScriptedSession, sql

HISTORY = "campaign has history — archive it instead"


def _campaign(status: str, cid: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(campaign_id=cid or uuid.uuid4(), status=status,
                           archived_at=None)


async def _delete(session: ScriptedSession, cid: uuid.UUID) -> None:
    return await delete_campaign(cid, session=session)


def _deleted(session: ScriptedSession) -> bool:
    return any(isinstance(s, Delete) for s in session.statements)


# ── DELETE ────────────────────────────────────────────────────────────────────
async def test_delete_missing_returns_404():
    session = ScriptedSession(None)
    with pytest.raises(HTTPException) as exc:
        await _delete(session, uuid.uuid4())
    assert exc.value.status_code == 404
    assert not _deleted(session) and session.commits == 0


async def test_delete_draft_without_history_deletes_under_row_lock():
    cid = uuid.uuid4()
    session = ScriptedSession("DRAFT", False, None, rows={cid: _campaign("DRAFT", cid)})
    assert await _delete(session, cid) is None
    lock, history, remove = session.statements
    # статус и история читаются под блокировкой строки кампании
    assert sql(lock).endswith("FOR UPDATE")
    assert "FROM recommendations" in sql(history)
    assert "FROM cashback_accruals" in sql(history)
    assert isinstance(remove, Delete)
    assert session.commits == 1


@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED", "COMPLETED"])
async def test_delete_non_draft_is_409_even_without_history(status):
    """Раньше PAUSED/COMPLETED удалялись — вместе с журналом начислений."""
    cid = uuid.uuid4()
    session = ScriptedSession(status, False, rows={cid: _campaign(status, cid)})
    with pytest.raises(HTTPException) as exc:
        await _delete(session, cid)
    assert (exc.value.status_code, exc.value.detail) == (409, HISTORY)
    assert not _deleted(session) and session.commits == 0
    assert session.rollbacks == 1                  # блокировка отпущена


async def test_delete_draft_with_history_is_409():
    cid = uuid.uuid4()
    session = ScriptedSession("DRAFT", True, rows={cid: _campaign("DRAFT", cid)})
    with pytest.raises(HTTPException) as exc:
        await _delete(session, cid)
    assert (exc.value.status_code, exc.value.detail) == (409, HISTORY)
    assert not _deleted(session) and session.commits == 0


async def test_delete_rejected_by_restrict_fk_is_409_not_500():
    """История появилась в обход проверки → FK ON DELETE RESTRICT (009)."""
    cid = uuid.uuid4()
    fk = IntegrityError("DELETE FROM cashback_campaigns", {},
                        Exception("violates foreign key constraint"))
    session = ScriptedSession("DRAFT", False, fk, rows={cid: _campaign("DRAFT", cid)})
    with pytest.raises(HTTPException) as exc:
        await _delete(session, cid)
    assert (exc.value.status_code, exc.value.detail) == (409, HISTORY)
    assert session.commits == 0 and session.rollbacks == 1


# ── POST …/archive ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["PAUSED", "COMPLETED"])
async def test_archive_paused_or_completed_sets_archived_at(status):
    cid = uuid.uuid4()
    row = SimpleNamespace(
        campaign_id=cid, name="c", target_segment_ids=[1], cashback_rate=5,
        min_transaction_amount=None, budget_total=100, budget_spent=0,
        status=status, start_date="2026-01-01T00:00:00Z",
        end_date="2026-02-01T00:00:00Z", allowed_channels=[],
        require_existing_behavior=False, rate_tiers=None, daily_limit=None,
        auto_pause=True, rfm_min=None, rfm_max=None, created_by=None,
        archived_at="2026-01-15T00:00:00Z")
    session = ScriptedSession((cid,), [("5411", None)], rows={cid: row})
    resp = await archive_campaign(cid, session=session)
    assert resp.archived_at is not None and resp.mcc_codes == ["5411"]
    update = sql(session.statements[0])
    # условие на статус — в самом UPDATE; повторный архив не сдвигает дату
    assert "status IN ('PAUSED', 'COMPLETED')" in update
    assert "archived_at=coalesce(cashback_campaigns.archived_at, now())" in update
    assert session.commits == 1


@pytest.mark.parametrize(("status", "detail"), [
    ("ACTIVE", "pause or complete the campaign first"),
    ("DRAFT", "a DRAFT campaign is not archived — delete it instead"),
])
async def test_archive_active_or_draft_is_409(status, detail):
    cid = uuid.uuid4()
    session = ScriptedSession(None, rows={cid: _campaign(status, cid)})
    with pytest.raises(HTTPException) as exc:
        await archive_campaign(cid, session=session)
    assert (exc.value.status_code, exc.value.detail) == (409, detail)
    assert session.commits == 0 and session.rollbacks == 1


async def test_archive_missing_is_404():
    session = ScriptedSession(None)
    with pytest.raises(HTTPException) as exc:
        await archive_campaign(uuid.uuid4(), session=session)
    assert exc.value.status_code == 404
