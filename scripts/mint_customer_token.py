#!/usr/bin/env python3
"""Демо-IdP: выпускает customer-токен для mobile_api (stage B, B12).

mobile_api только ПРОВЕРЯЕТ токены клиентов (services/mobile_api/app/security.py):
HS256, подпись ``MOBILE_JWT_SECRET``, ``aud = cashback-mobile``,
``iss = MOBILE_JWT_ISSUER``, ``type = customer``, ``sub`` — UUID клиента.
В проде такие токены выпускает IdP банка; в демо, тестах, e2e и locust —
этот скрипт (своего эндпоинта выдачи токенов у BFF нет).

    python3 scripts/mint_customer_token.py --user-id <uuid> [--ttl 3600]

``MOBILE_JWT_SECRET`` / ``MOBILE_JWT_ISSUER`` берутся из окружения, а если
там их нет — из ``.env`` в корне репозитория (его создаёт ``make env``) или
из файла ``--env-file``. Печатается только токен:

    TOKEN=$(python3 scripts/mint_customer_token.py --user-id "$USER_ID")
    curl -H "Authorization: Bearer $TOKEN" \\
         "http://localhost:8003/v1/mobile/cashback/balance/$USER_ID"

Только стандартная библиотека: скрипт запускается системным python3 без
venv, а тесты/e2e/locust импортируют ``mint_customer_token()`` отсюда же.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

AUDIENCE = "cashback-mobile"
TOKEN_TYPE = "customer"
DEFAULT_ISSUER = "cashback-demo-idp"
DEFAULT_TTL_SECONDS = 3600
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_customer_token(
    user_id: uuid.UUID | str,
    *,
    secret: str,
    issuer: str = DEFAULT_ISSUER,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """HS256 customer-токен с клеймами контракта stage B (§2)."""
    if not secret:
        raise ValueError("MOBILE_JWT_SECRET is empty")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued = int(time.time()) if now is None else now
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": issuer,
        "sub": str(uuid.UUID(str(user_id))),
        "aud": AUDIENCE,
        "type": TOKEN_TYPE,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, claims)
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"),
                         hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` из .env (как его читает docker compose): комментарии,
    ``export``, кавычки и ``  # комментарий`` после значения без кавычек."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def load_settings(env_file: Path | None = None) -> tuple[str, str]:
    """(secret, issuer): окружение важнее .env — как у pydantic-settings."""
    path = DEFAULT_ENV_FILE if env_file is None else env_file
    if env_file is not None and not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    file_values = read_env_file(path) if path.is_file() else {}

    def pick(name: str) -> str:
        return os.environ.get(name) or file_values.get(name, "")

    return pick("MOBILE_JWT_SECRET"), pick("MOBILE_JWT_ISSUER") or DEFAULT_ISSUER


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint a demo customer token for mobile_api (stage B, B12).",
    )
    parser.add_argument("--user-id", required=True, type=uuid.UUID,
                        help="customer user_id (UUID) — becomes the token sub")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS,
                        help="token lifetime in seconds (default: 3600)")
    parser.add_argument("--env-file", type=Path, default=None,
                        help=f"read secrets from this file (default: {DEFAULT_ENV_FILE})")
    args = parser.parse_args(argv)
    if args.ttl <= 0:
        parser.error("--ttl must be a positive number of seconds")

    try:
        secret, issuer = load_settings(args.env_file)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    if not secret:
        print("MOBILE_JWT_SECRET is not set: export it or run `make env` "
              "to create .env", file=sys.stderr)
        return 2
    print(mint_customer_token(args.user_id, secret=secret, issuer=issuer,
                              ttl_seconds=args.ttl))
    return 0


if __name__ == "__main__":
    sys.exit(main())
