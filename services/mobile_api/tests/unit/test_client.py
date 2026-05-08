"""Unit tests for the recommendation client (retry + circuit breaker)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from app.clients.recommendation_client import (
    CircuitBreaker,
    CircuitBreakerOpen,
    RecommendationClient,
)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------
async def test_breaker_starts_closed():
    cb = CircuitBreaker()
    assert cb.state == "closed"
    await cb.before_call()  # no-op


async def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, reset_seconds=10)
    for _ in range(3):
        await cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(CircuitBreakerOpen):
        await cb.before_call()


async def test_breaker_half_open_after_reset(monkeypatch):
    cb = CircuitBreaker(failure_threshold=2, reset_seconds=1)
    for _ in range(2):
        await cb.record_failure()
    assert cb.state == "open"
    # Fast-forward monotonic clock by patching time.
    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 5.0)
    assert cb.state == "half_open"


async def test_breaker_resets_on_success():
    cb = CircuitBreaker(failure_threshold=3)
    await cb.record_failure()
    await cb.record_failure()
    await cb.record_success()
    assert cb.state == "closed"


# ---------------------------------------------------------------------------
# RecommendationClient
# ---------------------------------------------------------------------------
async def test_client_returns_payload_on_success():
    payload = {"user_id": "u-1", "recommendations": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recommendations/u-1"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as h:
        client = RecommendationClient("http://x", h, max_attempts=1)
        result = await client.get_recommendations("u-1", top_k=3)
    assert result == payload


async def test_client_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="oops")
        return httpx.Response(200, json={"recommendations": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as h:
        client = RecommendationClient("http://x", h, max_attempts=4)
        result = await client.get_recommendations("u-2")
    assert result == {"recommendations": []}
    assert calls["n"] == 3


async def test_client_opens_breaker_on_repeated_failures():
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=30)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as h:
        client = RecommendationClient(
            "http://x", h, breaker=breaker, max_attempts=1,
        )
        for _ in range(2):
            with pytest.raises(Exception):
                await client.get_recommendations("u-3")
    assert breaker.state == "open"
    with pytest.raises(CircuitBreakerOpen):
        await breaker.before_call()


async def test_client_breaker_short_circuits_when_open():
    breaker = CircuitBreaker(failure_threshold=1)
    await breaker.record_failure()
    assert breaker.state == "open"

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("upstream must not be called when breaker is open")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as h:
        client = RecommendationClient(
            "http://x", h, breaker=breaker, max_attempts=3,
        )
        with pytest.raises(CircuitBreakerOpen):
            await client.get_recommendations("u-4")
