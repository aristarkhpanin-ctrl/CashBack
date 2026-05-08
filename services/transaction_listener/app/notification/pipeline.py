"""Notification pipeline — chapter 3.2, listing 3.12.

Chain of Responsibility: ``ChannelSelector`` walks an ordered list of
:class:`NotificationAdapter` instances. The first adapter that both
``can_handle`` (matches the user's allowed channels) and successfully
``deliver``\\s wins; subsequent adapters are skipped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from prometheus_client import Counter

log = structlog.get_logger("notification")


DELIVERY = Counter(
    "notification_delivery_total",
    "Notification delivery outcomes",
    ["channel", "outcome"],
)


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------
@dataclass
class Recommendation:
    user_id: str
    mcc_code: str
    score: float
    campaign_id: Optional[str] = None
    cashback_rate: Optional[float] = None
    campaign_name: str = "Cashback offer"
    expires_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserPreferences:
    user_id: str
    allowed_channels: list[str] = field(default_factory=lambda: ["push", "in_app"])
    email: Optional[str] = None
    phone: Optional[str] = None
    push_token: Optional[str] = None
    locale: str = "ru"
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Adapter base class
# ---------------------------------------------------------------------------
class NotificationAdapter(ABC):
    name: str = "abstract"

    def can_handle(self, prefs: UserPreferences) -> bool:
        return self.name in prefs.allowed_channels

    @abstractmethod
    async def deliver(
        self, rec: Recommendation, prefs: UserPreferences,
    ) -> bool:  # pragma: no cover - abstract
        ...


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------
class PushAdapter(NotificationAdapter):
    """FCM push (stubbed: prints + optional HTTP POST when configured)."""

    name = "push"

    def __init__(self, http_client: Any = None,
                 endpoint: str = "", token: str = "") -> None:
        self._http = http_client
        self._endpoint = endpoint
        self._token = token

    def can_handle(self, prefs: UserPreferences) -> bool:
        return super().can_handle(prefs) and bool(prefs.push_token)

    async def deliver(self, rec: Recommendation, prefs: UserPreferences) -> bool:
        body = {
            "to": prefs.push_token,
            "notification": {
                "title": rec.campaign_name,
                "body": (
                    f"Кэшбэк {rec.cashback_rate or 0}% по категории "
                    f"{rec.mcc_code} ждёт вас!"
                ),
            },
            "data": {
                "campaign_id": rec.campaign_id or "",
                "mcc_code": rec.mcc_code,
                "score": str(rec.score),
            },
        }
        if self._http is not None and self._endpoint:
            try:
                resp = await self._http.post(
                    self._endpoint,
                    json=body,
                    headers={"Authorization": f"key={self._token}"} if self._token else {},
                    timeout=5.0,
                )
                ok = 200 <= resp.status_code < 300
                DELIVERY.labels(channel=self.name,
                                outcome="ok" if ok else "fcm_error").inc()
                return ok
            except Exception as exc:  # noqa: BLE001
                log.warning("push_failed", error=str(exc))
                DELIVERY.labels(channel=self.name, outcome="exception").inc()
                return False
        # Demo mode — log only.
        log.info("push_sent_stub", user_id=prefs.user_id, body=body)
        DELIVERY.labels(channel=self.name, outcome="stub").inc()
        return True


class EmailAdapter(NotificationAdapter):
    """Renders ``cashback_offer.html`` with Jinja2 and writes it to disk."""

    name = "email"

    def __init__(self, template_dir: str, sent_dir: str) -> None:
        self._template_dir = Path(template_dir)
        self._sent_dir = Path(sent_dir)
        self._sent_dir.mkdir(parents=True, exist_ok=True)
        self._env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=select_autoescape(["html"]),
        )

    def can_handle(self, prefs: UserPreferences) -> bool:
        return super().can_handle(prefs) and bool(prefs.email)

    async def deliver(self, rec: Recommendation, prefs: UserPreferences) -> bool:
        try:
            tpl = self._env.get_template("cashback_offer.html")
            html = tpl.render(rec=rec, user=prefs,
                              now=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001
            log.warning("template_failed", error=str(exc))
            DELIVERY.labels(channel=self.name, outcome="template_error").inc()
            return False

        path = self._sent_dir / (
            f"{datetime.now(UTC):%Y%m%dT%H%M%S}_"
            f"{prefs.user_id}_{uuid.uuid4().hex[:8]}.html"
        )

        def _write() -> None:
            path.write_text(html, encoding="utf-8")

        await asyncio.to_thread(_write)
        log.info("email_written", path=str(path), to=prefs.email)
        DELIVERY.labels(channel=self.name, outcome="ok").inc()
        return True


class SmsAdapter(NotificationAdapter):
    """Stub SMS adapter — only logs (the SMS gateway is out of scope)."""

    name = "sms"

    def __init__(self, http_client: Any = None, endpoint: str = "") -> None:
        self._http = http_client
        self._endpoint = endpoint

    def can_handle(self, prefs: UserPreferences) -> bool:
        return super().can_handle(prefs) and bool(prefs.phone)

    async def deliver(self, rec: Recommendation, prefs: UserPreferences) -> bool:
        text = (
            f"Кэшбэк {rec.cashback_rate or 0}% по MCC {rec.mcc_code} — "
            f"подробнее в приложении."
        )
        log.info("sms_sent_stub", phone=prefs.phone, text=text)
        DELIVERY.labels(channel=self.name, outcome="stub").inc()
        return True


class InAppAdapter(NotificationAdapter):
    """Pushes a JSON payload onto a Redis list (``in_app_queue:{user_id}``)."""

    name = "in_app"
    QUEUE_FMT = "in_app_queue:{user_id}"
    QUEUE_TTL_SECONDS = 7 * 24 * 3600

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def can_handle(self, prefs: UserPreferences) -> bool:  # always available
        return self.name in prefs.allowed_channels

    async def deliver(self, rec: Recommendation, prefs: UserPreferences) -> bool:
        payload = {
            "campaign_id": rec.campaign_id,
            "campaign_name": rec.campaign_name,
            "mcc_code": rec.mcc_code,
            "cashback_rate": rec.cashback_rate,
            "score": rec.score,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "delivered_at": datetime.now(UTC).isoformat(),
        }
        key = self.QUEUE_FMT.format(user_id=prefs.user_id)
        try:
            await self._redis.lpush(key, json.dumps(payload, ensure_ascii=False))
            await self._redis.expire(key, self.QUEUE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("in_app_failed", error=str(exc))
            DELIVERY.labels(channel=self.name, outcome="exception").inc()
            return False
        DELIVERY.labels(channel=self.name, outcome="ok").inc()
        return True


# ---------------------------------------------------------------------------
# Chain of Responsibility
# ---------------------------------------------------------------------------
class ChannelSelector:
    """Walk the ordered ``_chain`` and stop at the first successful delivery.

    Order in the spec (highest priority first): push → in-app → email → sms.
    Each adapter returns ``True`` on successful delivery; on ``False`` or
    exception the next adapter is tried.
    """

    def __init__(self, adapters: Iterable[NotificationAdapter]) -> None:
        self._chain: list[NotificationAdapter] = list(adapters)

    @property
    def chain_names(self) -> list[str]:
        return [a.name for a in self._chain]

    async def deliver(
        self,
        rec: Recommendation,
        prefs: UserPreferences,
    ) -> Optional[str]:
        for adapter in self._chain:
            if not adapter.can_handle(prefs):
                continue
            try:
                ok = await adapter.deliver(rec, prefs)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "adapter_exception",
                    adapter=adapter.name, error=str(exc), user_id=prefs.user_id,
                )
                continue
            if ok:
                return adapter.name
        return None
