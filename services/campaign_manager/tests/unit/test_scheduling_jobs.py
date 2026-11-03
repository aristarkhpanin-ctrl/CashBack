"""Планировщик: независимые задачи и счётчик их сбоев (этап C — B39, B22).

Раньше всё шло одним тиком в общем try: сбой ``complete_expired`` или
ClickHouse-паузы молча отменял переиздание снапшота ML-лимитов — то есть
kill switch, не опубликованный при PUT, не публиковался и потом. Теперь
каждая задача — отдельная задача APScheduler в своём try, сбой виден в
``scheduler_job_failures_total{job}``; переиздание — раз в 60 с. Паузы по
daily_limit нет совсем (B22): лимит соблюдают eligible_campaigns и
транзакция начисления, статус кампании не меняется.

Этап D (B33): своя задача ``settle-accruals`` — PENDING старше
``SETTLEMENT_DELAY_HOURS`` становятся PAID с ``paid_at`` (раньше PAID не
ставил никто: последняя стадия воронки и «выплачено» в mobile_api — 0).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from app import scheduling
from app.api.ml_limits import SNAPSHOT_KEY
from app.models import MlLimit
from app.scheduling import (
    ML_LIMITS_REPUBLISH_SECONDS,
    SCHEDULER_JOB_FAILURES,
    CampaignScheduler,
    run_job,
)
from apscheduler.triggers.interval import IntervalTrigger
from prometheus_client import REGISTRY

from tests.unit.fakes import FakeRedis, FakeSession

JOBS = {"complete-expired-campaigns", "settle-accruals", "export-budget-metrics",
        "export-online-ctr", "republish-ml-limits"}


def _failures(job: str) -> float:
    return REGISTRY.get_sample_value("scheduler_job_failures_total", {"job": job}) or 0.0


async def test_every_job_is_scheduled_on_its_own():
    sched = CampaignScheduler(object(), redis_client=FakeRedis(), interval_minutes=15)
    sched.start()
    try:
        jobs = {job.id: job for job in sched._scheduler.get_jobs()}
    finally:
        sched.shutdown()
    assert set(jobs) == JOBS                      # ClickHouse-паузы больше нет
    for name, job in jobs.items():
        assert isinstance(job.trigger, IntervalTrigger)
        expected = (ML_LIMITS_REPUBLISH_SECONDS if name == "republish-ml-limits"
                    else 15 * 60)
        assert job.trigger.interval.total_seconds() == expected, name
        assert job.func is run_job and job.args[0] == name
        assert job.next_run_time is not None      # первый прогон — сразу
    assert ML_LIMITS_REPUBLISH_SECONDS == 60
    # серии счётчика заведены с нуля — алерт на increase() видит первый сбой
    for name in JOBS:
        assert REGISTRY.get_sample_value("scheduler_job_failures_total",
                                         {"job": name}) is not None


async def test_failing_job_is_counted_and_does_not_stop_the_others(monkeypatch):
    ran: list[str] = []

    async def _boom(_db):
        raise ConnectionError("postgres is down")

    def _record(name):
        async def _job(*_a):
            ran.append(name)
        return _job

    monkeypatch.setattr(scheduling, "complete_expired_campaigns", _boom)
    monkeypatch.setattr(scheduling, "settle_accruals", _record("settle"))
    monkeypatch.setattr(scheduling, "export_budget_metrics", _record("budget"))
    monkeypatch.setattr(scheduling, "export_online_ctr", _record("ctr"))
    monkeypatch.setattr(scheduling, "republish_ml_limits_snapshot", _record("republish"))
    before = {name: _failures(name) for name in JOBS}

    sched = CampaignScheduler(object(), redis_client=FakeRedis(), interval_minutes=15)
    sched.start()                                 # все задачи стартуют сразу
    try:
        for _ in range(50):
            if len(ran) == 4:
                break
            await asyncio.sleep(0.02)
    finally:
        sched.shutdown()

    assert sorted(ran) == ["budget", "ctr", "republish", "settle"]
    assert _failures("complete-expired-campaigns") == before["complete-expired-campaigns"] + 1
    for name in JOBS - {"complete-expired-campaigns"}:
        assert _failures(name) == before[name]


async def test_run_job_swallows_and_counts():
    async def _boom():
        raise RuntimeError("redis is down")

    before = _failures("republish-ml-limits")
    await run_job("republish-ml-limits", _boom)   # не бросает
    assert _failures("republish-ml-limits") == before + 1
    assert SCHEDULER_JOB_FAILURES._name == "scheduler_job_failures"


async def test_republish_job_publishes_the_db_state(monkeypatch):
    """Задача republish-ml-limits берёт лимиты из БД — то, что PUT сохранил,
    но не смог опубликовать (503), доходит до Redis без нового PUT."""
    from decimal import Decimal

    db = FakeSession()
    db.put(MlLimit(segment_bucket="mass", min_rate=Decimal("1"), max_rate=Decimal("4"),
                   daily_budget=Decimal("1"), enabled=False))

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return db

        async def __aexit__(self, *_exc):
            return False

    import sqlalchemy.ext.asyncio
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "AsyncSession", _Session)
    redis = FakeRedis(fail=True)
    before = _failures("republish-ml-limits")
    job = dict((n, j) for n, j, _ in
               CampaignScheduler(object(), redis_client=redis).jobs())["republish-ml-limits"]

    await run_job("republish-ml-limits", job)                  # Redis лежит
    assert _failures("republish-ml-limits") == before + 1 and redis.data == {}

    redis.fail = False                                          # Redis вернулся
    await run_job("republish-ml-limits", job)
    snap = json.loads(redis.data[SNAPSHOT_KEY][1])
    assert snap["buckets"]["mass"] == {"min_rate": 1.0, "max_rate": 4.0, "enabled": False}
    assert _failures("republish-ml-limits") == before + 1


@pytest.mark.parametrize("name", ["pause_overspent_campaigns", "DAILY_SPENT_SQL"])
def test_no_daily_limit_pause_remains(name):
    """B22: пауза по запросу в ClickHouse (таблицы начислений там нет) была
    мертва; дневной лимит больше не меняет статус кампании."""
    assert not hasattr(scheduling, name)


def test_complete_expired_is_compare_and_set():
    sql = " ".join(str(scheduling.COMPLETE_EXPIRED_SQL).split())
    # архивная кампания статус не меняет — ни через API, ни планировщиком
    assert ("WHERE status IN ('ACTIVE', 'PAUSED') AND archived_at IS NULL "
            "AND end_date <= now()") in sql


class _Result:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, [], []

    async def execute(self, stmt, *args, **_kw):
        self.sql.append(str(stmt))
        self.params.append(args[0] if args else None)
        return _Result(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Engine:
    def __init__(self, rows):
        self.conn = _Conn(rows)

    def begin(self):
        return self.conn

    def connect(self):
        return self.conn


async def test_complete_expired_runs_the_cas_update_in_a_transaction():
    engine = _Engine([("c1",), ("c2",)])
    assert await scheduling.complete_expired_campaigns(engine) == 2
    assert engine.conn.sql == [str(scheduling.COMPLETE_EXPIRED_SQL)]
    assert await scheduling.complete_expired_campaigns(_Engine([])) == 0


def test_settlement_sql_pays_pending_after_the_hold():
    sql = " ".join(str(scheduling.SETTLE_ACCRUALS_SQL).split())
    assert sql == ("UPDATE cashback_accruals SET status = 'PAID', paid_at = now() "
                   "WHERE status = 'PENDING' "
                   "AND accrued_at < now() - make_interval(hours => :delay_hours)")


async def test_settle_accruals_runs_in_a_transaction_with_the_delay():
    engine = _Engine([("a1",), ("a2",), ("a3",)])
    assert await scheduling.settle_accruals(engine, 24) == 3
    assert engine.conn.sql == [str(scheduling.SETTLE_ACCRUALS_SQL)]
    assert engine.conn.params == [{"delay_hours": 24}]
    assert await scheduling.settle_accruals(_Engine([]), 0) == 0


async def test_settlement_job_uses_the_configured_delay(monkeypatch):
    calls = []

    async def _settle(db, delay_hours):
        calls.append((db, delay_hours))
        return 0

    monkeypatch.setattr(scheduling, "settle_accruals", _settle)
    db = object()
    jobs = {n: j for n, j, _ in CampaignScheduler(
        db, redis_client=FakeRedis(), settlement_delay_hours=6).jobs()}
    await run_job("settle-accruals", jobs["settle-accruals"])
    default = {n: j for n, j, _ in CampaignScheduler(db).jobs()}
    await run_job("settle-accruals", default["settle-accruals"])
    assert calls == [(db, 6), (db, 24)]


async def test_settlement_failure_has_its_own_counter():
    before = {name: _failures(name) for name in JOBS}

    class _DeadEngine:
        def begin(self):
            raise ConnectionError("postgres is down")

    job = {n: j for n, j, _ in CampaignScheduler(_DeadEngine()).jobs()}["settle-accruals"]
    await run_job("settle-accruals", job)
    assert _failures("settle-accruals") == before["settle-accruals"] + 1
    for name in JOBS - {"settle-accruals"}:
        assert _failures(name) == before[name]


def test_online_ctr_counts_only_real_responses():
    """«Ответил» — ACCEPTED/DECLINED/SNOOZE, как в аналитике: EXPIRED оффер
    клиент не открывал и в знаменатель CTR не входит."""
    sql = " ".join(str(scheduling._ONLINE_CTR_SQL).split())
    assert ("FILTER (WHERE response_status IN ('ACCEPTED', 'DECLINED', 'SNOOZE'))"
            in sql)
    assert "<> 'PENDING'" not in sql


async def test_export_online_ctr_skips_versions_without_responses():
    from types import SimpleNamespace

    engine = _Engine([SimpleNamespace(model_version="7", ctr=0.25),
                      SimpleNamespace(model_version="8", ctr=None)])
    assert await scheduling.export_online_ctr(engine) == 1
    assert REGISTRY.get_sample_value("ml_online_ctr", {"model_version": "7"}) == 0.25
    assert REGISTRY.get_sample_value("ml_online_ctr", {"model_version": "8"}) is None


async def test_republish_without_redis_is_a_no_op():
    await scheduling.republish_ml_limits_snapshot(object(), None)
