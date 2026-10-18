"""Integration: деньги кампаний на настоящем Postgres со схемой alembic head
(миграция 009) — этап C, контракт §3.

Каждый тест — своя свежая БД (``db_dsn`` из conftest.py), настоящее
приложение (``create_app``) через ASGI, данные — прямыми INSERT'ами.

* B24 — DELETE кампании с историей → 409, журнал начислений цел; архив.
* B25 — activate после end_date → 409; до start_date → ``starts_in_future``.
* B38 — activate, прочитавший PAUSED, против планировщика, успевшего
  завершить кампанию: COMPLETED остаётся COMPLETED; отказ триггера — 409;
  правка черновика против activate: работающая кампания не меняется.
* B23 — ``GET /campaigns/{id}/budget`` (только чтение, расход за UTC-сутки).
* B26 — ``/campaigns/applicable`` = ``eligible_campaigns``.
* B22 — задачи планировщика на схеме head: истёкшие завершаются (кроме
  архивных), дневной лимит статус не меняет; KPI не считают архивные.
* B39 — PUT /ml-limits при лежащем Redis → 503, изменение в БД; задача
  ``republish-ml-limits`` доставляет его в настоящий Redis.

Redis — ``CM_IT_REDIS_URL`` или одноразовый testcontainers-Redis.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.integration.test_auth_migrations import _alembic

pytestmark = pytest.mark.integration

BOOTSTRAP_PASSWORD = "Bootstrap-chosen-42"
HISTORY = "campaign has history — archive it instead"


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
async def stack(db_dsn):
    import httpx
    from app.db import make_engine, make_sessionmaker
    from app.events import EventBroker
    from app.main import create_app

    from tests.unit.fakes import FakeRedis

    _alembic(db_dsn, "upgrade", "head", password=BOOTSTRAP_PASSWORD)
    engine = make_engine(db_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    app = create_app()
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.redis = FakeRedis()
    app.state.event_broker = EventBroker()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cm") as client:
            r = await client.post("/auth/login", json={
                "email": "admin@bank.ru", "password": BOOTSTRAP_PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            yield SimpleNamespace(client=client, engine=engine, app=app)
    finally:
        await engine.dispose()


# ── данные ────────────────────────────────────────────────────────────────────
async def _sql(engine, sql: str, **params):
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.fetchall() if result.returns_rows else None


async def _campaign(engine, *, status="ACTIVE", start=None, end=None, segs=(5,),
                    rate="5.00", budget="10000", spent="0", daily_limit=None,
                    rfm=(None, None), mccs=("5411",), archived=False) -> uuid.UUID:
    cid = uuid.uuid4()
    await _sql(engine, """
        INSERT INTO cashback_campaigns
               (campaign_id, name, target_segment_ids, cashback_rate, budget_total,
                budget_spent, status, start_date, end_date, daily_limit,
                rfm_min, rfm_max, archived_at)
        VALUES (:cid, :name, :segs, :rate, :budget, :spent,
                CAST(:status AS campaign_status), :start, :end, :daily,
                :rfm_min, :rfm_max, CASE WHEN :archived THEN now() END)""",
               cid=cid, name=f"c-{cid.hex[:8]}", segs=list(segs), rate=Decimal(rate),
               budget=Decimal(budget), spent=Decimal(spent), status=status,
               start=start or _now() - timedelta(days=1),
               end=end or _now() + timedelta(days=30),
               daily=Decimal(daily_limit) if daily_limit else None,
               rfm_min=rfm[0], rfm_max=rfm[1], archived=archived)
    for mcc in mccs:
        await _sql(engine, "INSERT INTO campaign_categories (campaign_id, mcc_code) "
                           "VALUES (:cid, :mcc)", cid=cid, mcc=mcc)
    return cid


async def _user(engine, *, segment=5, rfm=3, consents=("GRANTED",)) -> uuid.UUID:
    """Клиент; ``consents`` — история согласий по порядку (последнее — в силе)."""
    uid = uuid.uuid4()
    await _sql(engine, "INSERT INTO users (user_id, external_id, segment_id, rfm_score) "
                       "VALUES (:uid, :ext, :seg, :rfm)",
               uid=uid, ext=f"u-{uid.hex[:12]}", seg=segment, rfm=rfm)
    for i, status in enumerate(consents):
        await _sql(engine, """
            INSERT INTO user_consents (user_id, consent_type, status, document_version,
                                       created_at)
            VALUES (:uid, 'personalised_cashback', CAST(:st AS consent_status), 'v1',
                    now() - make_interval(days => :ago))""",
                   uid=uid, st=status, ago=len(consents) - i)
    return uid


async def _accrual(engine, uid, cid, amount: str, *, at=None, with_offer=True) -> None:
    rid = None
    if with_offer:
        rid = uuid.uuid4()
        await _sql(engine, """
            INSERT INTO recommendations (recommendation_id, user_id, campaign_id, mcc_code,
                                         model_score, response_status, expires_at)
            VALUES (:rid, :uid, :cid, '5411', 0.9, 'ACCEPTED', now() + interval '1 day')""",
                   rid=rid, uid=uid, cid=cid)
    await _sql(engine, """
        INSERT INTO cashback_accruals (user_id, campaign_id, recommendation_id,
                                       transaction_id, mcc_code, transaction_amount,
                                       cashback_amount, accrued_at)
        VALUES (:uid, :cid, :rid, :tx, '5411', 1000, :amount, COALESCE(:at, now()))""",
               uid=uid, cid=cid, rid=rid, tx=f"tx-{uuid.uuid4().hex}",
               amount=Decimal(amount), at=at)


async def _status(engine, cid) -> str:
    ((status,),) = await _sql(engine, "SELECT status::text FROM cashback_campaigns "
                                      "WHERE campaign_id = :cid", cid=cid)
    return status


async def _ledger(engine, cid):
    return await _sql(engine, """
        SELECT count(*), COALESCE(sum(cashback_amount), 0),
               (SELECT count(*) FROM recommendations r WHERE r.campaign_id = :cid),
               (SELECT count(*) FROM campaign_categories cc WHERE cc.campaign_id = :cid),
               (SELECT count(*) FROM cashback_campaigns c WHERE c.campaign_id = :cid)
          FROM cashback_accruals WHERE campaign_id = :cid""", cid=cid)


# ── B24: удаление и архив ────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["DRAFT", "PAUSED", "COMPLETED"])
async def test_delete_campaign_with_accruals_is_409_and_ledger_intact(stack, status):
    """Раньше DELETE PAUSED/COMPLETED каскадом стирал журнал начислений —
    долг перед клиентами (B24)."""
    cid = await _campaign(stack.engine, status=status)
    uid = await _user(stack.engine)
    await _accrual(stack.engine, uid, cid, "75.00")
    await _accrual(stack.engine, uid, cid, "25.00", with_offer=False)   # legacy
    before = await _ledger(stack.engine, cid)
    assert before == [(2, Decimal("100.00"), 1, 1, 1)]

    r = await stack.client.delete(f"/campaigns/{cid}")
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": HISTORY}
    assert await _ledger(stack.engine, cid) == before        # ничего не удалено


async def test_delete_draft_with_only_recommendations_is_409(stack):
    cid = await _campaign(stack.engine, status="DRAFT")
    uid = await _user(stack.engine)
    await _sql(stack.engine, """
        INSERT INTO recommendations (user_id, campaign_id, mcc_code, model_score,
                                     expires_at)
        VALUES (:uid, :cid, '5411', 0.5, now() + interval '1 day')""", uid=uid, cid=cid)
    r = await stack.client.delete(f"/campaigns/{cid}")
    assert (r.status_code, r.json()) == (409, {"detail": HISTORY})
    assert (await _ledger(stack.engine, cid))[0][2:] == (1, 1, 1)


@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED", "COMPLETED"])
async def test_delete_non_draft_without_history_is_409(stack, status):
    cid = await _campaign(stack.engine, status=status)
    r = await stack.client.delete(f"/campaigns/{cid}")
    assert (r.status_code, r.json()) == (409, {"detail": HISTORY})
    assert await _status(stack.engine, cid) == status


async def test_delete_draft_without_history_removes_it_and_its_categories(stack):
    cid = await _campaign(stack.engine, status="DRAFT", mccs=("5411", "5812"))
    r = await stack.client.delete(f"/campaigns/{cid}")
    assert r.status_code == 204, r.text
    assert await _ledger(stack.engine, cid) == [(0, Decimal("0"), 0, 0, 0)]
    assert (await stack.client.delete(f"/campaigns/{cid}")).status_code == 404


async def test_archive_hides_campaign_and_freezes_its_status(stack):
    paused = await _campaign(stack.engine, status="PAUSED")
    completed = await _campaign(stack.engine, status="COMPLETED")
    active = await _campaign(stack.engine, status="ACTIVE")
    draft = await _campaign(stack.engine, status="DRAFT")
    uid = await _user(stack.engine)
    await _accrual(stack.engine, uid, paused, "10.00")

    r = await stack.client.post(f"/campaigns/{paused}/archive")
    assert r.status_code == 200, r.text
    archived_at = r.json()["archived_at"]
    assert archived_at is not None and r.json()["status"] == "PAUSED"
    assert (await stack.client.post(f"/campaigns/{completed}/archive")).status_code == 200

    # повторный архив — та же дата; история на месте
    again = await stack.client.post(f"/campaigns/{paused}/archive")
    assert again.status_code == 200 and again.json()["archived_at"] == archived_at
    assert (await _ledger(stack.engine, paused))[0][:2] == (1, Decimal("10.00"))

    r = await stack.client.post(f"/campaigns/{active}/archive")
    assert (r.status_code, r.json()["detail"]) == (409, "pause or complete the campaign first")
    r = await stack.client.post(f"/campaigns/{draft}/archive")
    assert r.status_code == 409 and "delete it instead" in r.json()["detail"]
    assert (await stack.client.post(f"/campaigns/{uuid.uuid4()}/archive")).status_code == 404

    listed = {c["campaign_id"] for c in (await stack.client.get("/campaigns")).json()}
    assert listed == {str(active), str(draft)}
    with_archive = (await stack.client.get("/campaigns",
                                           params={"include_archived": "true"})).json()
    assert {c["campaign_id"] for c in with_archive} == {
        str(paused), str(completed), str(active), str(draft)}
    assert {c["campaign_id"] for c in with_archive if c["archived_at"]} == {
        str(paused), str(completed)}
    # архивная кампания не возвращается в работу
    r = await stack.client.patch(f"/campaigns/{paused}/status", params={"action": "activate"})
    assert (r.status_code, r.json()["detail"]) == (409, "campaign is archived")
    assert await _status(stack.engine, paused) == "PAUSED"
    # карточку архивной кампании по-прежнему можно открыть
    assert (await stack.client.get(f"/campaigns/{paused}")).json()["archived_at"]


async def test_active_list_hides_archived_unless_asked(stack):
    # ACTIVE+archived через API не получить; строка — как от старых данных
    shown = await _campaign(stack.engine)
    hidden = await _campaign(stack.engine, archived=True)
    ids = {c["campaign_id"] for c in (await stack.client.get("/campaigns/active")).json()}
    assert ids == {str(shown)}
    r = await stack.client.get("/campaigns/active", params={"include_archived": "true"})
    assert {c["campaign_id"] for c in r.json()} == {str(shown), str(hidden)}


# ── B25: окно дат при активации ───────────────────────────────────────────────
@pytest.mark.parametrize("status", ["DRAFT", "PAUSED"])
async def test_activate_after_end_date_is_409(stack, status):
    cid = await _campaign(stack.engine, status=status, start=_now() - timedelta(days=30),
                          end=_now() - timedelta(minutes=1))
    r = await stack.client.patch(f"/campaigns/{cid}/status", params={"action": "activate"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "campaign has already ended (end_date has passed)"
    assert await _status(stack.engine, cid) == status


async def test_activate_before_start_date_is_allowed_but_pays_from_start(stack):
    future = await _campaign(stack.engine, status="DRAFT",
                             start=_now() + timedelta(days=2), segs=())
    running = await _campaign(stack.engine, status="DRAFT", segs=())
    uid = await _user(stack.engine)

    r = await stack.client.patch(f"/campaigns/{future}/status", params={"action": "activate"})
    assert r.status_code == 200, r.text
    assert r.json()["current"] == "ACTIVE" and r.json()["starts_in_future"] is True
    r = await stack.client.patch(f"/campaigns/{running}/status", params={"action": "activate"})
    assert r.json()["starts_in_future"] is False
    assert await _status(stack.engine, future) == "ACTIVE"
    # до start_date кампания никому не положена
    applicable = (await stack.client.get(f"/campaigns/applicable/{uid}")).json()
    assert [c["campaign_id"] for c in applicable] == [str(running)]


# ── B38: compare-and-set против планировщика ──────────────────────────────────
@pytest.mark.parametrize("stall", ["after_read", "before_write"])
async def test_activate_racing_scheduler_complete_keeps_completed(stack, stall):
    """activate прочитал PAUSED (end_date ещё впереди); пока он «думал»,
    end_date прошёл и планировщик завершил кампанию. Без compare-and-set
    обработчик записывал ACTIVE поверх COMPLETED — кампания «оживала» (B38).

    ``after_read`` — пауза сразу после чтения кампании (так устроены и
    старый, и новый обработчик): дальше его остановит уже проверка окна
    дат. ``before_write`` — пауза перед самим UPDATE, когда все проверки
    пройдены: остановить запись может только условие ``status = PAUSED``."""
    from app.models import CashbackCampaign
    from app.scheduling import complete_expired_campaigns
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.dml import Update

    paused, release = asyncio.Event(), asyncio.Event()

    class _StallingSession(AsyncSession):
        async def get(self, entity, ident, *args, **kwargs):
            row = await super().get(entity, ident, *args, **kwargs)
            if stall == "after_read" and entity is CashbackCampaign:
                paused.set()
                await release.wait()
            return row

        async def execute(self, statement, *args, **kwargs):
            if (stall == "before_write" and isinstance(statement, Update)
                    and statement.table.name == "cashback_campaigns"):
                paused.set()
                await release.wait()
            return await super().execute(statement, *args, **kwargs)

    stack.app.state.sessionmaker = async_sessionmaker(
        stack.engine, class_=_StallingSession, expire_on_commit=False)
    end = _now() + timedelta(seconds=1.5)
    cid = await _campaign(stack.engine, status="PAUSED", end=end)

    activate = asyncio.create_task(stack.client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"}))
    await asyncio.wait_for(paused.wait(), timeout=10)   # прочитал PAUSED
    await asyncio.sleep((end - _now()).total_seconds() + 0.2)
    assert await complete_expired_campaigns(stack.engine) == 1    # планировщик успел
    release.set()
    r = await asyncio.wait_for(activate, timeout=10)

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "after_read": "campaign has already ended (end_date has passed)",
        "before_write": "campaign changed concurrently (expected status PAUSED) "
                        "— reload and retry",
    }[stall]
    assert await _status(stack.engine, cid) == "COMPLETED"


async def _waiting_on_a_lock(engine) -> bool:
    ((waiting,),) = await _sql(engine, """
        SELECT count(*) FROM pg_stat_activity
         WHERE datname = current_database() AND wait_event_type = 'Lock'""")
    return waiting > 0


async def test_draft_edit_racing_activate_does_not_touch_the_live_campaign(stack):
    """PATCH читает черновик, пока activate уже записал ACTIVE, но ещё не
    закоммитил. Без блокировки строки правка проходила проверку «только
    DRAFT» и ложилась на работающую кампанию: ставка, минимум, категории."""
    from sqlalchemy import text

    cid = await _campaign(stack.engine, status="DRAFT", mccs=("5411",))
    async with stack.engine.connect() as activate:        # activate: CAS, не закоммичен
        await activate.begin()
        moved = await activate.execute(text(
            "UPDATE cashback_campaigns SET status = 'ACTIVE' "
            "WHERE campaign_id = :cid AND status = 'DRAFT' RETURNING campaign_id"),
            {"cid": cid})
        assert moved.first() is not None
        edit = asyncio.create_task(stack.client.patch(f"/campaigns/{cid}", json={
            "cashback_rate": "50.00", "min_transaction_amount": "0",
            "mcc_codes": ["5411", "5812"]}))
        for _ in range(100):                               # правка ждёт строку
            if await _waiting_on_a_lock(stack.engine) or edit.done():
                break
            await asyncio.sleep(0.05)
        await activate.commit()
    r = await asyncio.wait_for(edit, timeout=10)

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "only DRAFT campaigns are editable (status=ACTIVE)"
    assert await _sql(stack.engine, """
        SELECT c.status::text, c.cashback_rate, c.min_transaction_amount,
               array_agg(cc.mcc_code::text ORDER BY cc.mcc_code)
          FROM cashback_campaigns c JOIN campaign_categories cc USING (campaign_id)
         WHERE c.campaign_id = :cid
         GROUP BY 1, 2, 3""", cid=cid) == [("ACTIVE", Decimal("5.00"), None, ["5411"])]
    # без гонки черновик по-прежнему правится
    draft = await _campaign(stack.engine, status="DRAFT")
    r = await stack.client.patch(f"/campaigns/{draft}", json={"cashback_rate": "7.00"})
    assert (r.status_code, r.json()["cashback_rate"]) == (200, "7.00"), r.text


async def test_status_guard_trigger_is_the_last_line_and_maps_to_409(stack, monkeypatch):
    """Даже если FSM приложения пропустит запрещённый переход, триггер БД
    (миграция 009) его отвергнет — и это 409, а не 500."""
    from app import fsm

    monkeypatch.setitem(fsm.TRANSITIONS, ("COMPLETED", "activate"), "ACTIVE")
    cid = await _campaign(stack.engine, status="COMPLETED")
    r = await stack.client.patch(f"/campaigns/{cid}/status", params={"action": "activate"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "campaign status transition COMPLETED -> ACTIVE is not allowed"
    assert await _status(stack.engine, cid) == "COMPLETED"


async def test_regular_transitions_still_work(stack):
    cid = await _campaign(stack.engine, status="DRAFT")
    for action, status in [("activate", "ACTIVE"), ("pause", "PAUSED"),
                           ("activate", "ACTIVE"), ("complete", "COMPLETED")]:
        r = await stack.client.patch(f"/campaigns/{cid}/status", params={"action": action})
        assert (r.status_code, r.json()["current"]) == (200, status), r.text
        assert await _status(stack.engine, cid) == status
    r = await stack.client.patch(f"/campaigns/{cid}/status", params={"action": "activate"})
    assert r.status_code == 409


# ── B23: бюджет только для чтения ─────────────────────────────────────────────
async def test_budget_view_reports_today_spend_in_utc(stack):
    cid = await _campaign(stack.engine, budget="1000", spent="300", daily_limit="200")
    plain = await _campaign(stack.engine, budget="500", spent="0")
    uid = await _user(stack.engine)
    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    await _accrual(stack.engine, uid, cid, "70.00")
    await _accrual(stack.engine, uid, cid, "50.00", at=midnight)
    await _accrual(stack.engine, uid, cid, "180.00", at=midnight - timedelta(minutes=1))

    r = await stack.client.get(f"/campaigns/{cid}/budget")
    assert r.status_code == 200, r.text
    body = {k: (Decimal(v) if k != "campaign_id" and v is not None else v)
            for k, v in r.json().items()}
    assert body == {"campaign_id": str(cid), "budget_total": Decimal("1000"),
                    "budget_spent": Decimal("300"), "remaining": Decimal("700"),
                    "daily_limit": Decimal("200"), "spent_today": Decimal("120"),
                    "remaining_today": Decimal("80")}
    r = await stack.client.get(f"/campaigns/{plain}/budget")
    assert r.json()["daily_limit"] is None and r.json()["remaining_today"] is None
    assert Decimal(r.json()["spent_today"]) == 0
    assert (await stack.client.get(f"/campaigns/{uuid.uuid4()}/budget")).status_code == 404
    # «резервирования» больше нет: бюджет меняет только начисление
    gone = await stack.client.post(f"/campaigns/{cid}/budget/check", json={"amount": "10"})
    assert gone.status_code in (404, 405)
    ((spent,),) = await _sql(stack.engine, "SELECT budget_spent FROM cashback_campaigns "
                                           "WHERE campaign_id = :cid", cid=cid)
    assert spent == Decimal("300.00")


# ── B26: applicable = eligible_campaigns ──────────────────────────────────────
async def test_applicable_is_exactly_eligible_campaigns(stack):
    e = stack.engine
    best = await _campaign(e, segs=(), rate="7.00", mccs=("5812",))
    both = await _campaign(e, segs=(5,), rate="5.00", mccs=("5411", "5812"),
                           budget="1000", spent="250")
    # ни одна из этих не положена клиенту — по разным правилам eligibility
    await _campaign(e, segs=(7,))                                   # чужой сегмент
    await _campaign(e, rfm=(4, 5))                                  # RFM 3 вне [4, 5]
    await _campaign(e, start=_now() + timedelta(days=2))            # ещё не началась
    await _campaign(e, archived=True)                               # в архиве
    await _campaign(e, status="PAUSED")                             # не ACTIVE
    await _campaign(e, budget="100", spent="100")                   # бюджет исчерпан
    capped = await _campaign(e, daily_limit="100")                  # дневной лимит
    ok = await _user(e, segment=5, rfm=3, consents=("GRANTED",))
    revoked = await _user(e, segment=5, rfm=3, consents=("GRANTED", "REVOKED"))
    await _accrual(e, revoked, capped, "100.00")

    r = await stack.client.get(f"/campaigns/applicable/{ok}")
    assert r.status_code == 200, r.text
    assert [(c["campaign_id"], c["mcc_codes"]) for c in r.json()] == [
        (str(best), ["5812"]), (str(both), ["5411", "5812"])]
    assert Decimal(r.json()[1]["remaining_budget"]) == Decimal("750")
    # отозванное согласие (последняя запись) — ничего, хотя GRANTED было
    assert (await stack.client.get(f"/campaigns/applicable/{revoked}")).json() == []
    assert (await stack.client.get(f"/campaigns/applicable/{uuid.uuid4()}")).json() == []
    r = await stack.client.get(f"/campaigns/applicable/{ok}", params={"limit": 1})
    assert [c["campaign_id"] for c in r.json()] == [str(best)]


# ── B22: задачи планировщика на схеме head ────────────────────────────────────
async def test_scheduler_jobs_on_head_schema(stack):
    from app.scheduling import CampaignScheduler, run_job
    from prometheus_client import REGISTRY

    from tests.unit.fakes import FakeRedis

    e = stack.engine
    ended = await _campaign(e, start=_now() - timedelta(days=9), end=_now() - timedelta(hours=1))
    ended_paused = await _campaign(e, status="PAUSED", start=_now() - timedelta(days=9),
                                   end=_now() - timedelta(minutes=5))
    ended_archived = await _campaign(e, status="PAUSED", start=_now() - timedelta(days=9),
                                     end=_now() - timedelta(minutes=5), archived=True)
    capped = await _campaign(e, daily_limit="100")
    uid = await _user(e)
    await _accrual(e, uid, capped, "150.00")                        # лимит дня превышен

    def _failures(job):
        return REGISTRY.get_sample_value("scheduler_job_failures_total", {"job": job}) or 0

    sched = CampaignScheduler(e, redis_client=FakeRedis())
    before = {name: _failures(name) for name, _, _ in sched.jobs()}
    for name, job, _ in sched.jobs():
        await run_job(name, job)
    assert {name: _failures(name) for name in before} == before     # SQL верен на head

    assert await _status(e, ended) == "COMPLETED"
    assert await _status(e, ended_paused) == "COMPLETED"
    assert await _status(e, ended_archived) == "PAUSED"   # архив статус не меняет
    assert await _status(e, capped) == "ACTIVE"     # лимит дня — не пауза (B22)
    budget = (await stack.client.get(f"/campaigns/{capped}/budget")).json()
    assert Decimal(budget["remaining_today"]) == 0


async def test_kpis_do_not_count_archived_campaigns(stack):
    """Архивная кампания скрыта из списков и в работу не вернётся — KPI
    «кампании / бюджет» по умолчанию её тоже не считают."""
    e = stack.engine
    await _campaign(e, status="PAUSED", budget="100000")
    await _campaign(e, status="ACTIVE", budget="30000")
    archived = await _campaign(e, status="PAUSED", budget="100000")
    assert (await stack.client.post(f"/campaigns/{archived}/archive")).status_code == 200

    r = await stack.client.get("/analytics/kpis")
    assert r.status_code == 200, r.text
    assert (r.json()["campaigns_count"], Decimal(r.json()["budget"])) == (
        2, Decimal("130000"))
    # конкретную архивную кампанию по-прежнему можно запросить явно
    r = await stack.client.get("/analytics/kpis", params={"campaign_id": str(archived)})
    assert (r.json()["campaigns_count"], Decimal(r.json()["budget"])) == (
        1, Decimal("100000"))


# ── B39: ML-лимиты при лежащем Redis ──────────────────────────────────────────
@pytest.fixture(scope="module")
def redis_url():
    url = os.environ.get("CM_IT_REDIS_URL")
    if url:
        yield url
        return
    pytest.importorskip("testcontainers")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7.2-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


def _dead_redis_url() -> str:
    with socket.socket() as s:                  # свободный порт — никто не слушает
        s.bind(("127.0.0.1", 0))
        return f"redis://127.0.0.1:{s.getsockname()[1]}/0"


async def test_ml_limits_put_with_redis_down_is_503_then_republished(stack, redis_url):
    from app.api.ml_limits import SNAPSHOT_KEY
    from app.scheduling import CampaignScheduler
    from prometheus_client import REGISTRY
    from redis.asyncio import Redis

    live = Redis.from_url(redis_url, decode_responses=True)
    dead = Redis.from_url(_dead_redis_url(), decode_responses=True)
    await live.delete(SNAPSHOT_KEY)
    stack.app.state.redis = dead                  # Redis «упал» после логина
    try:
        r = await stack.client.put("/ml-limits", json={
            "global_enabled": False,
            "limits": [{"segment_bucket": "premium", "min_rate": "4", "max_rate": "11",
                        "daily_budget": "180000"}]})
        assert r.status_code == 503, r.text
        assert r.json() == {"detail": "saved but not published to the serving path; "
                                      "it will be retried"}
        stored = (await stack.client.get("/ml-limits")).json()       # БД сохранила
        assert stored["global_enabled"] is False
        premium = next(x for x in stored["limits"] if x["segment_bucket"] == "premium")
        assert Decimal(premium["max_rate"]) == Decimal("11")

        def _failures():
            return REGISTRY.get_sample_value("scheduler_job_failures_total",
                                             {"job": "republish-ml-limits"}) or 0

        # планировщик с тем же лежащим Redis: задача падает, считается, живёт
        before = _failures()
        sched = CampaignScheduler(stack.engine, redis_client=dead, republish_seconds=1)
        sched.start()
        try:
            for _ in range(100):
                if _failures() >= before + 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            sched.shutdown()
        assert _failures() >= before + 2              # повторяется, а не умирает
        assert await live.get(SNAPSHOT_KEY) is None

        # Redis вернулся — задача republish-ml-limits доставляет сохранённое
        sched = CampaignScheduler(stack.engine, redis_client=live, republish_seconds=1)
        sched.start()
        try:
            raw = None
            for _ in range(100):
                raw = await live.get(SNAPSHOT_KEY)
                if raw:
                    break
                await asyncio.sleep(0.05)
        finally:
            sched.shutdown()
        snap = json.loads(raw)
        assert snap["global_enabled"] is False
        assert snap["buckets"]["premium"] == {"min_rate": 4.0, "max_rate": 11.0,
                                              "enabled": True}
        assert 0 < await live.ttl(SNAPSHOT_KEY) <= 24 * 3600
    finally:
        await live.delete(SNAPSHOT_KEY)
        await live.aclose()
        await dead.aclose()
