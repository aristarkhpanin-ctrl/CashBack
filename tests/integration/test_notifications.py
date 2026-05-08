"""Integration: notification pipeline — 4 channels + chain orchestration."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Use the transaction_listener service's `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "transaction_listener"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration


def _rec(**overrides):
    from app.notification.pipeline import Recommendation
    base = dict(
        user_id="u-1", mcc_code="5411", score=0.83,
        campaign_id="c-1", cashback_rate=5.0, campaign_name="Grocery",
    )
    base.update(overrides)
    return Recommendation(**base)


def _prefs(**overrides):
    from app.notification.pipeline import UserPreferences
    base = dict(
        user_id="u-1",
        allowed_channels=["push", "in_app", "email", "sms"],
        email="user@example.com", phone="+79991112233", push_token="tok",
    )
    base.update(overrides)
    return UserPreferences(**base)


# ---------------------------------------------------------------------------
async def test_in_app_channel_pushes_to_redis(redis_url):
    pytest.importorskip("redis")
    from app.notification.pipeline import InAppAdapter
    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await redis.delete("in_app_queue:u-1")
        adapter = InAppAdapter(redis_client=redis)
        ok = await adapter.deliver(_rec(), _prefs())
        assert ok is True
        items = await redis.lrange("in_app_queue:u-1", 0, -1)
        assert len(items) == 1
        payload = json.loads(items[0])
        assert payload["mcc_code"] == "5411"
    finally:
        await redis.aclose()


async def test_email_channel_writes_html_file(tmp_path):
    """Render the bundled cashback_offer.html into tmp_path."""
    from app.notification.pipeline import EmailAdapter

    template_dir = (
        Path(__file__).resolve().parents[2]
        / "services" / "transaction_listener" / "app"
        / "notification" / "templates"
    )
    out_dir = tmp_path / "sent"
    adapter = EmailAdapter(str(template_dir), str(out_dir))
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True
    html_files = list(out_dir.glob("*.html"))
    assert len(html_files) == 1
    body = html_files[0].read_text(encoding="utf-8")
    assert "5411" in body
    assert "Grocery" in body


async def test_sms_channel_returns_true_in_stub_mode():
    from app.notification.pipeline import SmsAdapter
    adapter = SmsAdapter()
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True


async def test_push_channel_stub_mode_when_no_endpoint():
    from app.notification.pipeline import PushAdapter
    adapter = PushAdapter(http_client=None, endpoint="", token="")
    ok = await adapter.deliver(_rec(), _prefs())
    assert ok is True


async def test_chain_picks_first_eligible_adapter(redis_url):
    """In-app first, push second — push has no token so chain falls back to in-app."""
    pytest.importorskip("redis")
    from app.notification.pipeline import (
        ChannelSelector,
        InAppAdapter,
        PushAdapter,
        SmsAdapter,
    )
    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await redis.delete("in_app_queue:u-fallback")
        chain = ChannelSelector([
            PushAdapter(),                # no token in prefs → can_handle=False
            InAppAdapter(redis_client=redis),
            SmsAdapter(),
        ])
        prefs = _prefs(user_id="u-fallback", push_token=None,
                       allowed_channels=["push", "in_app"])
        chosen = await chain.deliver(_rec(user_id="u-fallback"), prefs)
        assert chosen == "in_app"
    finally:
        await redis.aclose()
