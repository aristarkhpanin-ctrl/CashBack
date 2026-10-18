"""PATCH /campaigns/{id}/status — compare-and-set и окно дат (этап C).

B38: обработчик читал статус и писал новый безусловно — кампания, которую
между чтением и записью завершил планировщик, «оживала». Теперь UPDATE
пишет только из прочитанного статуса (0 строк → 409), а отказ триггера
``trg_campaign_status_guard`` (миграция 009) — тоже 409, не 500.
B25: ``activate`` после ``end_date`` → 409; до ``start_date`` можно, ответ
говорит ``starts_in_future``. Гонка с настоящим планировщиком на настоящем
Postgres — tests/integration/test_money_integrity.py.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api.campaigns import patch_status, status_guard_rejection
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.sql.dml import Update

from tests.unit.fakes import ScriptedSession, sql

NOW = datetime.now(UTC)


def _row(status: str, *, start=NOW - timedelta(days=1), end=NOW + timedelta(days=30),
         archived_at=None) -> SimpleNamespace:
    return SimpleNamespace(campaign_id=uuid.uuid4(), status=status, start_date=start,
                           end_date=end, archived_at=archived_at)


def _patch(row, action: str, *replies):
    session = ScriptedSession(*replies, rows={row.campaign_id: row})
    return session, patch_status(row.campaign_id, action=action, session=session)


class _PgError(Exception):
    """Как asyncpg.PostgresError: sqlstate, constraint_name, message."""

    def __init__(self, sqlstate: str, constraint: str | None, message: str):
        super().__init__(message)
        self.sqlstate, self.constraint_name, self.message = sqlstate, constraint, message


def _db_error(cls, sqlstate: str, constraint: str | None, message: str) -> DBAPIError:
    # SQLAlchemy кладёт исключение драйвера в .orig, asyncpg — в его __cause__
    orig = Exception(message)
    orig.__cause__ = _PgError(sqlstate, constraint, message)
    return cls("UPDATE cashback_campaigns …", {}, orig)


GUARD = "campaign status transition COMPLETED -> ACTIVE is not allowed"


# ── compare-and-set ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("status", "action", "new"), [
    ("DRAFT", "activate", "ACTIVE"), ("ACTIVE", "pause", "PAUSED"),
    ("ACTIVE", "complete", "COMPLETED"), ("PAUSED", "activate", "ACTIVE"),
    ("PAUSED", "complete", "COMPLETED"),
])
async def test_update_is_conditional_on_the_status_that_was_read(status, action, new):
    row = _row(status)
    session, call = _patch(row, action, SimpleNamespace(starts_in_future=False))
    resp = await call
    assert (resp.previous, resp.current, resp.action) == (status, new, action)
    (stmt,) = session.statements
    assert isinstance(stmt, Update)
    where = sql(stmt).split(" WHERE ", 1)[1]
    assert f"cashback_campaigns.status = '{status}'" in where
    assert "cashback_campaigns.archived_at IS NULL" in where
    # activate ещё и не после end_date — по часам БД, в том же UPDATE
    assert ("cashback_campaigns.end_date > now()" in where) == (new == "ACTIVE")
    assert session.commits == 1


async def test_status_changed_concurrently_is_409_not_a_revival():
    """Прочитали PAUSED, а планировщик уже завершил: UPDATE … WHERE
    status='PAUSED' не находит строку — 409, ничего не записано."""
    row = _row("PAUSED")
    session, call = _patch(row, "activate", None)
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 409
    assert "changed concurrently" in exc.value.detail
    assert session.commits == 0 and session.rollbacks == 1


async def test_status_guard_trigger_rejection_is_409():
    row = _row("PAUSED")
    err = _db_error(IntegrityError, "23514", "trg_campaign_status_guard", GUARD)
    session, call = _patch(row, "activate", err)
    with pytest.raises(HTTPException) as exc:
        await call
    assert (exc.value.status_code, exc.value.detail) == (409, GUARD)
    assert session.commits == 0 and session.rollbacks == 1


@pytest.mark.parametrize("err", [
    # другой CHECK с тем же SQLSTATE и сбой соединения — не «конфликт статуса»
    _db_error(IntegrityError, "23514", "ck_campaign_budget", "violates check"),
    _db_error(OperationalError, "08006", None, "connection lost"),
])
async def test_other_db_errors_are_not_masked_as_409(err):
    row = _row("ACTIVE")
    session, call = _patch(row, "pause", err)
    with pytest.raises(DBAPIError):
        await call
    assert session.commits == 0


def test_status_guard_rejection_reads_the_driver_error():
    assert status_guard_rejection(
        _db_error(IntegrityError, "23514", "trg_campaign_status_guard", GUARD)) == GUARD
    assert status_guard_rejection(
        _db_error(IntegrityError, "23514", "ck_campaign_dates", "x")) is None
    assert status_guard_rejection(
        _db_error(IntegrityError, "23505", "trg_campaign_status_guard", "x")) is None


# ── окно дат (B25) ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["DRAFT", "PAUSED"])
async def test_activate_after_end_date_is_409(status):
    row = _row(status, start=NOW - timedelta(days=30), end=NOW - timedelta(seconds=1))
    session, call = _patch(row, "activate")
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 409
    assert exc.value.detail == "campaign has already ended (end_date has passed)"
    assert session.statements == [] and session.commits == 0


async def test_pause_and_complete_after_end_date_are_still_allowed():
    row = _row("ACTIVE", start=NOW - timedelta(days=30), end=NOW - timedelta(hours=1))
    session, call = _patch(row, "complete", SimpleNamespace(starts_in_future=False))
    assert (await call).current == "COMPLETED"


async def test_activate_before_start_date_is_allowed_and_says_so():
    row = _row("DRAFT", start=NOW + timedelta(days=3), end=NOW + timedelta(days=30))
    session, call = _patch(row, "activate", SimpleNamespace(starts_in_future=True))
    resp = await call
    assert resp.current == "ACTIVE" and resp.starts_in_future is True
    assert "RETURNING cashback_campaigns.start_date > now()" in sql(session.statements[0])


async def test_pause_never_reports_starts_in_future():
    row = _row("ACTIVE", start=NOW + timedelta(days=3))
    session, call = _patch(row, "pause", SimpleNamespace(starts_in_future=True))
    assert (await call).starts_in_future is False


# ── отказы до UPDATE ───────────────────────────────────────────────────────────
async def test_missing_campaign_is_404():
    session = ScriptedSession()
    with pytest.raises(HTTPException) as exc:
        await patch_status(uuid.uuid4(), action="pause", session=session)
    assert exc.value.status_code == 404


async def test_fsm_violation_is_409_without_a_write():
    row = _row("COMPLETED")
    session, call = _patch(row, "activate")
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 409
    assert session.statements == []


async def test_archived_campaign_status_is_frozen():
    row = _row("PAUSED", archived_at=NOW - timedelta(days=1))
    session, call = _patch(row, "activate")
    with pytest.raises(HTTPException) as exc:
        await call
    assert (exc.value.status_code, exc.value.detail) == (409, "campaign is archived")
    assert session.statements == []
