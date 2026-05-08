"""Unit tests for the notification pipeline (listing 3.12)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from app.notification.ost import DeliveryScheduler, OSTUpdater
from app.notification.pipeline import (
    ChannelSelector,
    EmailAdapter,
    InAppAdapter,
    NotificationAdapter,
    PushAdapter,
    Recommendation,
    SmsAdapter,
    UserPreferences,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, list[str]] = {}
        self.kv: dict[str, tuple[str, int | None]] = {}

    async def lpush(self, key: str, value: str) -> int:
        self.hashes.setdefault(key, []).insert(0, value)
        return len(self.hashes[key])

    async def expire(self, key: str, ttl: int) -> bool:
        return key in self.hashes

    async def get(self, key: str):
        v = self.kv.get(key)
        return v[0] if v else None

    async def set(self, key: str, value: str, ex: int | None = None):
        self.kv[key] = (value, ex)


def _rec(**overrides) -> Recommendation:
    base = {
        "user_id": "u-1", "mcc_code": "5411",
        "score": 0.83, "campaign_id": "c-1",
        "cashback_rate": 5.0, "campaign_name": "Grocery",
        "expires_at": datetime(2026, 5, 15, tzinfo=UTC),
    }
    base.update(overrides)
    return Recommendation(**base)


def _prefs(**overrides) -> UserPreferences:
    base = {
        "user_id": "u-1",
        "allowed_channels": ["push", "in_app", "email", "sms"],
        "email": "user@example.com", "phone": "+79991112233",
        "push_token": "fcm-tok",
    }
    base.update(overrides)
    return UserPreferences(**base)


# ---------------------------------------------------------------------------
# Email adapter renders the template and writes a file
# ---------------------------------------------------------------------------
async def test_email_adapter_renders_template(tmp_path: Path):
    template_dir = (Path(__file__).resolve().parents[2]
                    / "app" / "notification" / "templates")
    out_dir = tmp_path / "sent"
    adapter = EmailAdapter(str(template_dir), str(out_dir))

    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True
    files = list(out_dir.glob("*.html"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "5411" in body
    assert "Grocery" in body
    # Cashback rate is rendered with 1-decimal precision.
    assert "5.0%" in body


# ---------------------------------------------------------------------------
# In-app adapter pushes onto Redis list
# ---------------------------------------------------------------------------
async def test_in_app_adapter_pushes_to_redis():
    redis = FakeRedis()
    adapter = InAppAdapter(redis_client=redis)
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok
    assert redis.hashes["in_app_queue:u-1"]
    payload = json.loads(redis.hashes["in_app_queue:u-1"][0])
    assert payload["mcc_code"] == "5411"
    assert payload["cashback_rate"] == 5.0


# ---------------------------------------------------------------------------
# Push adapter — stub mode + HTTP error mode
# ---------------------------------------------------------------------------
async def test_push_adapter_stub_mode_when_endpoint_missing():
    adapter = PushAdapter(http_client=None, endpoint="", token="")
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True


async def test_push_adapter_returns_false_on_5xx():
    class _Resp:
        status_code = 503
    class _Http:
        async def post(self, *a, **kw):
            return _Resp()
    adapter = PushAdapter(http_client=_Http(), endpoint="https://fcm/x", token="t")
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is False


async def test_push_adapter_returns_true_on_2xx():
    class _Resp:
        status_code = 200
    class _Http:
        async def post(self, *a, **kw):
            return _Resp()
    adapter = PushAdapter(http_client=_Http(), endpoint="https://fcm/x", token="t")
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True


async def test_push_adapter_returns_false_on_exception():
    class _Http:
        async def post(self, *a, **kw):
            raise RuntimeError("boom")
    adapter = PushAdapter(http_client=_Http(), endpoint="https://fcm/x", token="t")
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is False


# ---------------------------------------------------------------------------
# Sms adapter — stub branch
# ---------------------------------------------------------------------------
async def test_sms_adapter_logs_and_returns_true():
    adapter = SmsAdapter()
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True


# ---------------------------------------------------------------------------
# Email adapter — template error path
# ---------------------------------------------------------------------------
async def test_email_adapter_returns_false_on_template_error(tmp_path):
    # Point at an empty template dir so get_template raises.
    out_dir = tmp_path / "out"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    adapter = EmailAdapter(str(empty_dir), str(out_dir))
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is False


async def test_in_app_adapter_handles_redis_exception():
    class BoomRedis:
        async def lpush(self, key, value):
            raise RuntimeError("boom")
        async def expire(self, key, ttl):
            return True

    adapter = InAppAdapter(redis_client=BoomRedis())
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is False


# ---------------------------------------------------------------------------
# Push / SMS guard — adapters reject when prefs are missing the channel
# ---------------------------------------------------------------------------
def test_push_adapter_can_handle_requires_token():
    adapter = PushAdapter()
    assert adapter.can_handle(_prefs(push_token="x")) is True
    assert adapter.can_handle(_prefs(push_token=None)) is False
    assert adapter.can_handle(_prefs(allowed_channels=["sms"])) is False


def test_sms_adapter_can_handle_requires_phone():
    adapter = SmsAdapter()
    assert adapter.can_handle(_prefs(phone="+7")) is True
    assert adapter.can_handle(_prefs(phone=None)) is False


# ---------------------------------------------------------------------------
# Chain of Responsibility — first ok adapter wins
# ---------------------------------------------------------------------------
class _RecordingAdapter(NotificationAdapter):
    def __init__(self, name: str, succeed: bool) -> None:
        self.name = name
        self._succeed = succeed
        self.calls = 0

    async def deliver(self, rec, prefs):
        self.calls += 1
        return self._succeed


async def test_chain_picks_first_successful_adapter():
    a = _RecordingAdapter("push",   succeed=False)
    b = _RecordingAdapter("in_app", succeed=True)
    c = _RecordingAdapter("email",  succeed=True)
    chain = ChannelSelector([a, b, c])
    chosen = await chain.deliver(_rec(),
                                 _prefs(allowed_channels=["push", "in_app", "email"]))
    assert chosen == "in_app"
    assert a.calls == 1
    assert b.calls == 1
    assert c.calls == 0   # short-circuit after b


async def test_chain_returns_none_when_no_adapter_can_handle():
    a = _RecordingAdapter("push", succeed=True)
    chain = ChannelSelector([a])
    chosen = await chain.deliver(_rec(),
                                 _prefs(allowed_channels=["sms"]))
    assert chosen is None
    assert a.calls == 0   # adapter.name 'push' not in allowed_channels


async def test_chain_skips_adapters_that_raise():
    class _Boom(NotificationAdapter):
        name = "in_app"
        async def deliver(self, rec, prefs):
            raise RuntimeError("kaboom")

    boom = _Boom()
    fallback = _RecordingAdapter("email", succeed=True)
    chain = ChannelSelector([boom, fallback])
    chosen = await chain.deliver(_rec(),
                                 _prefs(allowed_channels=["in_app", "email"]))
    assert chosen == "email"
    assert fallback.calls == 1


def test_chain_advertises_channel_order():
    chain = ChannelSelector([
        _RecordingAdapter("push",   succeed=True),
        _RecordingAdapter("in_app", succeed=True),
        _RecordingAdapter("email",  succeed=True),
        _RecordingAdapter("sms",    succeed=True),
    ])
    assert chain.chain_names == ["push", "in_app", "email", "sms"]


# ---------------------------------------------------------------------------
# OST scheduler
# ---------------------------------------------------------------------------
async def test_ost_updater_validates_length():
    redis = FakeRedis()
    upd = OSTUpdater(redis_client=redis)
    with pytest.raises(ValueError):
        await upd.update("u-1", [1] * 23)


async def test_ost_updater_persists_payload():
    redis = FakeRedis()
    upd = OSTUpdater(redis_client=redis)
    await upd.update("u-1", [0] * 12 + [10] * 12)
    raw = await redis.get("ost:u-1")
    payload = json.loads(raw)
    assert len(payload["hours"]) == 24


async def test_delivery_scheduler_immediate_when_in_active_hours():
    redis = FakeRedis()
    sched = DeliveryScheduler(
        redis_client=redis,
        default_active_hours=range(8, 22),
    )
    # 12:00 UTC — should deliver immediately
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    assert await sched.should_deliver_now("u-1", now=now) is True
    assert await sched.next_send_time("u-1", now=now) == now


async def test_delivery_scheduler_defers_outside_active_hours():
    redis = FakeRedis()
    sched = DeliveryScheduler(
        redis_client=redis,
        default_active_hours=range(8, 22),
        max_defer_hours=18,
    )
    # 03:00 UTC — outside the default 08:00-22:00 window
    now = datetime(2026, 4, 29, 3, 0, tzinfo=UTC)
    assert await sched.should_deliver_now("u-1", now=now) is False
    next_dt = await sched.next_send_time("u-1", now=now)
    assert next_dt > now
    assert next_dt.hour == 8


async def test_delivery_scheduler_uses_user_specific_histogram():
    redis = FakeRedis()
    # User is most active 22-23h.
    payload = {"hours": [0] * 22 + [50, 60]}
    await redis.set("ost:u-night", json.dumps(payload))
    sched = DeliveryScheduler(redis_client=redis)
    # Noon → outside the user's active window → defer.
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    next_dt = await sched.next_send_time("u-night", now=now)
    assert next_dt.hour in (22, 23)
