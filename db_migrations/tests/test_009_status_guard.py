"""009: триггер ``trg_campaign_status_guard`` — FSM статусов в БД (B38, §1.5).

Разрешённые переходы берутся из ``services/campaign_manager/app/fsm.py``:
если FSM сервиса поменяется, а триггер — нет, тест упадёт.
"""
from __future__ import annotations

import importlib.util
import itertools

import pytest
from dbm_support import NOW, REPO

pytestmark = pytest.mark.integration

CHECK_VIOLATION = "23514"


def _load_fsm():
    path = REPO / "services" / "campaign_manager" / "app" / "fsm.py"
    spec = importlib.util.spec_from_file_location("campaign_manager_fsm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FSM = _load_fsm()
ALLOWED = {(old, new) for (old, _action), new in FSM.TRANSITIONS.items()}
CHANGES = [(old, new) for old, new in itertools.product(FSM.STATUSES, repeat=2)
           if old != new]


def test_fsm_is_the_one_the_trigger_mirrors():
    """Опорная точка: какие переходы сейчас в FSM сервиса."""
    assert ALLOWED == {("DRAFT", "ACTIVE"), ("ACTIVE", "PAUSED"),
                       ("ACTIVE", "COMPLETED"), ("PAUSED", "ACTIVE"),
                       ("PAUSED", "COMPLETED")}


@pytest.mark.parametrize(("old", "new"), sorted(ALLOWED))
def test_fsm_transition_is_allowed(pg, old, new):
    campaign_id = pg.campaign(status=old)
    pg.rows("UPDATE cashback_campaigns SET status = %s WHERE campaign_id = %s",
            new, campaign_id)
    assert pg.value("SELECT status::text FROM cashback_campaigns WHERE campaign_id = %s",
                    campaign_id) == new


@pytest.mark.parametrize(("old", "new"), sorted(set(CHANGES) - ALLOWED))
def test_transition_outside_fsm_is_rejected(pg, old, new):
    campaign_id = pg.campaign(status=old)
    exc = pg.error("UPDATE cashback_campaigns SET status = %s WHERE campaign_id = %s",
                   new, campaign_id)
    assert exc.pgcode == CHECK_VIOLATION
    assert exc.diag.message_primary == (
        f"campaign status transition {old} -> {new} is not allowed")
    assert exc.diag.constraint_name == "trg_campaign_status_guard"
    assert pg.value("SELECT status::text FROM cashback_campaigns WHERE campaign_id = %s",
                    campaign_id) == old


@pytest.mark.parametrize("new", ["DRAFT", "ACTIVE", "PAUSED"])
def test_completed_is_terminal(pg, new):
    """Гонка activate ↔ complete планировщика: COMPLETED не «оживает»."""
    campaign_id = pg.campaign(status="PAUSED")
    pg.rows("UPDATE cashback_campaigns SET status = 'COMPLETED' WHERE campaign_id = %s",
            campaign_id)
    exc = pg.error("UPDATE cashback_campaigns SET status = %s WHERE campaign_id = %s",
                   new, campaign_id)
    assert exc.pgcode == CHECK_VIOLATION


@pytest.mark.parametrize("status", FSM.STATUSES)
def test_update_without_status_change_is_not_a_transition(pg, status):
    campaign_id = pg.campaign(status=status)
    pg.rows("UPDATE cashback_campaigns SET status = status, name = 'renamed' "
            "WHERE campaign_id = %s", campaign_id)
    assert pg.value("SELECT name FROM cashback_campaigns WHERE campaign_id = %s",
                    campaign_id) == "renamed"


def test_other_columns_of_a_completed_campaign_stay_editable(pg):
    """Архивирование COMPLETED-кампании и списание бюджета — не смена статуса."""
    campaign_id = pg.campaign(status="COMPLETED")
    pg.rows("UPDATE cashback_campaigns SET archived_at = %s, budget_spent = 10 "
            "WHERE campaign_id = %s", NOW, campaign_id)
    assert pg.rows("SELECT archived_at, budget_spent FROM cashback_campaigns "
                   "WHERE campaign_id = %s", campaign_id) == [(NOW, 10)]


def test_bulk_update_is_all_or_nothing(pg):
    """Одна недопустимая строка отменяет весь UPDATE (как у планировщика)."""
    active = pg.campaign(status="ACTIVE")
    draft = pg.campaign(status="DRAFT")
    exc = pg.error("UPDATE cashback_campaigns SET status = 'COMPLETED' "
                   "WHERE campaign_id IN (%s, %s)", active, draft)
    assert exc.pgcode == CHECK_VIOLATION
    assert pg.rows("SELECT campaign_id::text, status::text FROM cashback_campaigns "
                   "WHERE campaign_id IN (%s, %s) ORDER BY 1", active, draft) == sorted(
        [(active, "ACTIVE"), (draft, "DRAFT")])


def test_insert_is_not_guarded(pg):
    """Триггер — только на UPDATE: сидер и импорт вставляют любой статус."""
    campaign_id = pg.campaign(status="COMPLETED")
    assert pg.value("SELECT status::text FROM cashback_campaigns WHERE campaign_id = %s",
                    campaign_id) == "COMPLETED"
