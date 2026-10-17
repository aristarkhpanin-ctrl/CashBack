"""scripts/mint_customer_token.py — демо-IdP (stage B, §7).

Токен скрипта принимает настоящая проверка mobile_api (app/security.py),
секрет и издатель берутся из окружения или из .env.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from jose import jwt

from tests.conftest import customer_secret
from tests.unit.fakes import build_app

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "mint_customer_token.py"


@pytest.fixture(scope="module")
def idp():
    spec = importlib.util.spec_from_file_location("mint_customer_token", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def empty_env_file(tmp_path) -> str:
    """Пустой .env: секреты только из окружения процесса."""
    path = tmp_path / "empty.env"
    path.write_text("", encoding="utf-8")
    return str(path)


def _run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    base = {k: v for k, v in os.environ.items()
            if k not in ("MOBILE_JWT_SECRET", "MOBILE_JWT_ISSUER")}
    return subprocess.run([sys.executable, str(SCRIPT), *args], env={**base, **(env or {})},
                          capture_output=True, text=True, cwd=cwd, timeout=30)


async def test_minted_token_is_accepted_by_mobile_api(idp):
    me = uuid.uuid4()
    token = idp.mint_customer_token(me, secret=customer_secret())

    built = build_app()
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        own = await client.get(f"/v1/mobile/cashback/balance/{me}",
                               headers={"Authorization": f"Bearer {token}"})
        other = await client.get(f"/v1/mobile/cashback/balance/{uuid.uuid4()}",
                                 headers={"Authorization": f"Bearer {token}"})

    assert own.status_code == 200, own.text
    assert other.status_code == 403, other.text


def test_claims_follow_the_contract(idp):
    me = uuid.uuid4()
    token = idp.mint_customer_token(str(me).upper(), secret="k" * 32,
                                    issuer="bank-idp", ttl_seconds=60, now=1_700_000_000)

    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    claims = jwt.decode(token, "k" * 32, algorithms=["HS256"], audience="cashback-mobile",
                        issuer="bank-idp", options={"verify_exp": False})
    assert claims == {"iss": "bank-idp", "sub": str(me), "aud": "cashback-mobile",
                      "type": "customer", "iat": 1_700_000_000, "exp": 1_700_000_060}


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"secret": ""}, "MOBILE_JWT_SECRET is empty"),
    ({"secret": "k" * 32, "ttl_seconds": 0}, "ttl_seconds must be positive"),
])
def test_rejects_empty_secret_and_non_positive_ttl(idp, kwargs, message):
    with pytest.raises(ValueError, match=message):
        idp.mint_customer_token(uuid.uuid4(), **kwargs)


def test_cli_reads_secret_and_issuer_from_env(empty_env_file):
    me = uuid.uuid4()
    proc = _run("--user-id", str(me), "--ttl", "120",
                "--env-file", empty_env_file,
                env={"MOBILE_JWT_SECRET": "e" * 32, "MOBILE_JWT_ISSUER": "env-idp"})

    assert proc.returncode == 0, proc.stderr
    token = proc.stdout.strip()
    assert "\n" not in token
    claims = jwt.decode(token, "e" * 32, algorithms=["HS256"],
                        audience="cashback-mobile", issuer="env-idp")
    assert claims["sub"] == str(me)
    assert claims["exp"] - claims["iat"] == 120


def test_cli_falls_back_to_env_file_and_env_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# сгенерировано make env\n"
        "JWT_SECRET=admin-secret-not-for-customers-000000\n"
        "export MOBILE_JWT_SECRET=\"" + "f" * 32 + "\"\n"
        "MOBILE_JWT_ISSUER=file-idp  # издатель демо-IdP\n",
        encoding="utf-8",
    )
    me = uuid.uuid4()

    from_file = _run("--user-id", str(me), "--env-file", str(env_file))
    assert from_file.returncode == 0, from_file.stderr
    claims = jwt.decode(from_file.stdout.strip(), "f" * 32, algorithms=["HS256"],
                        audience="cashback-mobile", issuer="file-idp")
    assert claims["exp"] - claims["iat"] == 3600  # TTL по умолчанию

    from_env = _run("--user-id", str(me), "--env-file", str(env_file),
                    env={"MOBILE_JWT_SECRET": "g" * 32})
    assert from_env.returncode == 0, from_env.stderr
    jwt.decode(from_env.stdout.strip(), "g" * 32, algorithms=["HS256"],
               audience="cashback-mobile", issuer="file-idp")


def test_cli_default_env_file_is_repo_root(idp):
    assert idp.DEFAULT_ENV_FILE == SCRIPT.parents[1] / ".env"


@pytest.mark.parametrize(("args", "code", "stderr"), [
    (("--user-id", "not-a-uuid"), 2, "invalid UUID value"),
    (("--user-id", str(uuid.UUID(int=1)), "--ttl", "0"), 2, "--ttl must be"),
    (("--user-id", str(uuid.UUID(int=1)), "--env-file", "/nonexistent/.env"), 2,
     "env file not found"),
    (("--user-id", str(uuid.UUID(int=1)), "--env-file", "{empty}"), 2,
     "MOBILE_JWT_SECRET is not set"),
])
def test_cli_errors(empty_env_file, args, code, stderr):
    proc = _run(*(a.replace("{empty}", empty_env_file) for a in args))

    assert proc.returncode == code
    assert stderr in proc.stderr
    assert proc.stdout == ""
