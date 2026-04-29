"""HTTP client to the Recommendation API with retry + circuit breaker."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import httpx
import structlog
from tenacity import (
    AsyncRetrying, retry_if_exception_type, stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger("mobile.recommendation_client")


class CircuitBreakerOpen(Exception):
    """Raised when the breaker is open and we refuse to call upstream."""


class CircuitBreaker:
    """Tiny circuit breaker (closed / open / half-open).

    * counts consecutive failures
    * opens after ``failure_threshold`` failures
    * stays open for ``reset_seconds``, then admits a single probe call
    """

    def __init__(self, failure_threshold: int = 5, reset_seconds: int = 30) -> None:
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self._reset:
            return "half_open"
        return "open"

    async def before_call(self) -> None:
        if self.state == "open":
            raise CircuitBreakerOpen(
                f"breaker open ({self._failures} failures); retry in "
                f"{int(self._reset - (time.monotonic() - (self._opened_at or 0)))}s"
            )

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                log.warning("circuit_breaker_opened", failures=self._failures)


# ---------------------------------------------------------------------------
class RecommendationClient:
    """Calls the Recommendation API; retries on 5xx / network errors."""

    def __init__(
        self,
        base_url: str,
        http_client: httpx.AsyncClient,
        *,
        breaker: Optional[CircuitBreaker] = None,
        max_attempts: int = 3,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http_client
        self._breaker = breaker or CircuitBreaker()
        self._max_attempts = max_attempts

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    # ------------------------------------------------------------------
    async def _call(self, fn: Callable[[], Awaitable[httpx.Response]]) -> dict:
        await self._breaker.before_call()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential_jitter(initial=0.1, max=2.0),
                retry=retry_if_exception_type(
                    (httpx.HTTPError, _RetryableUpstream)
                ),
                reraise=True,
            ):
                with attempt:
                    response = await fn()
                    if response.status_code >= 500:
                        raise _RetryableUpstream(
                            f"{response.request.method} {response.request.url} "
                            f"-> {response.status_code}"
                        )
                    response.raise_for_status()
                    await self._breaker.record_success()
                    return response.json()
        except Exception:
            await self._breaker.record_failure()
            raise

    # ------------------------------------------------------------------
    async def get_recommendations(
        self,
        user_id: str,
        *,
        top_k: int = 5,
        transaction_amount: float | None = None,
        channel: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"top_k": top_k}
        if transaction_amount is not None:
            params["transaction_amount"] = transaction_amount
        if channel is not None:
            params["channel"] = channel

        async def _do() -> httpx.Response:
            return await self._http.get(
                f"{self._base}/recommendations/{user_id}",
                params=params,
                timeout=self._http.timeout,
            )

        return await self._call(_do)


class _RetryableUpstream(Exception):
    """Internal marker for 5xx responses so tenacity treats them as retryable."""
