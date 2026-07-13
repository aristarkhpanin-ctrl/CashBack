"""Минтер service-токена для вызовов recommendation_api (beyond-plan).

mobile_api подписывает короткоживущий HS256-токен (``type: service``,
``sub: mobile-api``) общим ``JWT_SECRET`` и прикладывает его как Bearer к
запросам upstream. Токен кэшируется и перевыпускается за 30 с до
истечения — минтинг дешёвый, но незачем делать его на каждый запрос.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from jose import jwt

ALGORITHM = "HS256"
_REFRESH_SKEW = 30.0  # секунд до exp, за которые перевыпускаем


class ServiceTokenProvider:
    def __init__(self, secret: str, *, ttl_seconds: int = 300,
                 subject: str = "mobile-api") -> None:
        self._secret = secret
        self._ttl = ttl_seconds
        self._subject = subject
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._expires_at - _REFRESH_SKEW:
            return self._token
        issued = datetime.now(UTC)
        payload = {
            "sub": self._subject,
            "type": "service",
            "iat": int(issued.timestamp()),
            "exp": int((issued + timedelta(seconds=self._ttl)).timestamp()),
        }
        self._token = jwt.encode(payload, self._secret, algorithm=ALGORITHM)
        self._expires_at = now + self._ttl
        return self._token

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}
