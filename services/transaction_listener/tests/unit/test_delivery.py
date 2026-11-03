"""DeliveryChannelRecorder — the delivery channel of an offer (stage D, B29).

The NotificationPipeline adapter that delivered ``recommendations.created``
is written to ``recommendations.channel`` of the offer the event names —
once (``channel IS NULL``: the first delivering channel, or IN_APP when the
app showed the offer first, stays). Best effort: the notification is already
out, a Postgres failure is logged and counted, never raised. The SQL itself
runs on real Postgres in ``tests/integration/test_delivery_channel.py``.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.notification import delivery
from app.notification.delivery import CHANNEL_BY_ADAPTER, DeliveryChannelRecorder


class FakeDB:
    """AsyncEngine.begin(): records (sql, params); ``rowcount`` / ``error``."""

    def __init__(self, rowcount: int = 1, error: Exception | None = None) -> None:
        self.rowcount = rowcount
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def begin(self) -> FakeDB:
        return self

    async def __aenter__(self) -> FakeDB:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, clause: Any, params: dict[str, Any]) -> Any:
        self.calls.append((" ".join(str(clause).split()), params))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(rowcount=self.rowcount)


def outcomes() -> dict[str, float]:
    return {s.labels["outcome"]: s.value for metric in delivery.CHANNEL_RECORDED.collect()
            for s in metric.samples if s.name.endswith("_total")}


def delta(before: dict[str, float], outcome: str) -> float:
    return outcomes().get(outcome, 0) - before.get(outcome, 0)


def test_every_adapter_of_the_chain_has_a_delivery_channel():
    from app.notification.pipeline import (
        EmailAdapter,
        InAppAdapter,
        PushAdapter,
        SmsAdapter,
    )

    names = {a.name for a in (PushAdapter, InAppAdapter, EmailAdapter, SmsAdapter)}
    assert set(CHANNEL_BY_ADAPTER) == names
    # Values of the delivery_channel enum (migration 003).
    assert set(CHANNEL_BY_ADAPTER.values()) == {"PUSH", "SMS", "EMAIL", "IN_APP"}


@pytest.mark.parametrize(("adapter", "channel"), sorted(CHANNEL_BY_ADAPTER.items()))
async def test_first_delivery_writes_the_channel_once(adapter, channel):
    db = FakeDB(rowcount=1)
    rid = str(uuid.uuid4())
    before = outcomes()

    assert await DeliveryChannelRecorder(db).record(rid, adapter) is True

    [(sql, params)] = db.calls
    assert sql.startswith("UPDATE recommendations SET channel = CAST(:channel AS delivery_channel)")
    assert "WHERE recommendation_id = CAST(:rid AS uuid) AND channel IS NULL" in sql
    assert params == {"channel": channel, "rid": rid}
    assert delta(before, "recorded") == 1


async def test_channel_already_recorded_is_kept():
    before = outcomes()

    assert await DeliveryChannelRecorder(FakeDB(rowcount=0)).record(
        str(uuid.uuid4()), "push") is False
    assert delta(before, "already_recorded") == 1


@pytest.mark.parametrize("rid", [None, "", "not-a-uuid"])
async def test_event_without_a_valid_recommendation_id_touches_nothing(rid):
    db = FakeDB()
    before = outcomes()

    assert await DeliveryChannelRecorder(db).record(rid, "push") is False
    assert db.calls == []
    assert delta(before, "no_recommendation_id") == 1


async def test_unknown_adapter_touches_nothing():
    db = FakeDB()
    before = outcomes()

    assert await DeliveryChannelRecorder(db).record(str(uuid.uuid4()), "pigeon") is False
    assert db.calls == []
    assert delta(before, "unknown_channel") == 1


async def test_postgres_failure_is_counted_not_raised():
    db = FakeDB(error=ConnectionRefusedError("postgres is down"))
    before = outcomes()

    assert await DeliveryChannelRecorder(db).record(str(uuid.uuid4()), "email") is False
    assert len(db.calls) == 1
    assert delta(before, "error") == 1
