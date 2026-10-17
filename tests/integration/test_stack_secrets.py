"""Integration: секреты локального стенда (stage B, ADR-0013; контракт §1, §8).

Настоящие инструменты, без контейнеров: ``make env``, ``docker compose
config`` и ``scripts/init_db.sh`` (docker / curl / alembic — заглушки):

* ``make env`` создаёт .env из .env.example: ключи — 32 случайных байта hex,
  пароли — 18 байт base64, все разные, файл 600; существующий .env не
  перезаписывается никогда, о пустых в нём ключах говорится;
* docker compose без любого из ключей не собирает конфигурацию («run make
  env»), а с .env от ``make env`` каждый сервис получает ровно свои ключи
  (таблица §1): ни mobile-api, ни recommendation-api не видят JWT_SECRET;
* ``scripts/init_db.sh`` передаёт ADMIN_BOOTSTRAP_PASSWORD из .env миграциям —
  и одноразовому alembic-контейнеру, и локальному alembic.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

KEYS = ("JWT_SECRET", "SERVICE_JWT_SECRET", "MOBILE_JWT_SECRET")
PASSWORDS = ("ADMIN_BOOTSTRAP_PASSWORD", "DEMO_USERS_PASSWORD")
SECRETS = KEYS + PASSWORDS
# §1: кто какой ключ получает. Остальные сервисы — ни одного из SECRETS.
EXPECTED_SECRETS = {
    "campaign-manager": {"JWT_SECRET", "SERVICE_JWT_SECRET"},
    "recommendation-api": {"SERVICE_JWT_SECRET"},
    "mobile-api": {"MOBILE_JWT_SECRET", "SERVICE_JWT_SECRET"},
}
CORS_ORIGINS = "http://localhost:3000,http://localhost:5173"


def _scrubbed_env(**extra: str) -> dict[str, str]:
    """Окружение без секретов (CI экспортирует свои на прогон) и без
    переменных, которые меняют поведение make / compose."""
    drop = {*SECRETS, "MOBILE_JWT_ISSUER", "CORS_ORIGINS", "FORWARDED_ALLOW_IPS",
            "MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not k.startswith("COMPOSE_")}
    env.update(extra)
    return env


def _parse_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if line and not line.startswith("#"))


@pytest.fixture(scope="module")
def make_env(require_tool):
    make, _ = require_tool("make"), require_tool("openssl")

    def _run(env_file: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [make, "-s", "--no-print-directory", "env", f"ENV_FILE={env_file}"],
            cwd=REPO_ROOT, env=_scrubbed_env(), capture_output=True, text=True,
            timeout=60,
        )

    return _run


@pytest.fixture(scope="module")
def generated_env(make_env, tmp_path_factory) -> Path:
    env_file = tmp_path_factory.mktemp("make-env") / ".env"
    proc = make_env(env_file)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return env_file


@pytest.fixture(scope="module")
def compose(require_tool, generated_env):
    docker = require_tool("docker")
    if subprocess.run([docker, "compose", "version"], capture_output=True).returncode:
        pytest.fail("`docker compose` (plugin v2) is not available", pytrace=False)

    def _run(env_file: Path | str, *args: str,
             profiles: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        cmd = [docker, "compose", "-p", "cbsecrets", "--env-file", str(env_file),
               "-f", "docker-compose.yml"]
        for profile in profiles:
            cmd += ["--profile", profile]
        return subprocess.run([*cmd, *args], cwd=REPO_ROOT, env=_scrubbed_env(),
                              capture_output=True, text=True, timeout=60)

    # Все профили (ml, celery …): секреты проверяются у каждого сервиса.
    # `config --profiles` тоже интерполирует файл — ему нужен полный .env.
    listed = _run(generated_env, "config", "--profiles")
    assert listed.returncode == 0, listed.stderr
    every_profile = tuple(listed.stdout.split())
    assert every_profile, "docker-compose.yml has no profiles?"
    return lambda env_file, *args: _run(env_file, *args, profiles=every_profile)


# ---------------------------------------------------------------------------
# make env
# ---------------------------------------------------------------------------
def test_make_env_fills_every_secret_with_random_distinct_values(make_env, generated_env,
                                                                 tmp_path):
    assert generated_env.stat().st_mode & 0o777 == 0o600
    values = _parse_env(generated_env)

    for key in KEYS:
        assert re.fullmatch(r"[0-9a-f]{64}", values[key]), key
    for key in PASSWORDS:
        assert len(base64.b64decode(values[key], validate=True)) == 18, key
    assert len({values[key] for key in SECRETS}) == len(SECRETS)
    assert values["MOBILE_JWT_ISSUER"] == "cashback-demo-idp"
    assert values["CORS_ORIGINS"] == CORS_ORIGINS

    # Всё, кроме секретов, — строка в строку .env.example (комментарии тоже).
    def _without_secrets(path: Path) -> list[str]:
        return [line for line in path.read_text().splitlines()
                if line.split("=", 1)[0] not in SECRETS]

    assert _without_secrets(generated_env) == _without_secrets(ENV_EXAMPLE)
    assert {line for line in ENV_EXAMPLE.read_text().splitlines()
            if line.split("=", 1)[0] in SECRETS} == {f"{key}=" for key in SECRETS}

    # Случайные: второй .env не повторяет ни одного значения.
    other = tmp_path / ".env"
    assert make_env(other).returncode == 0
    again = _parse_env(other)
    assert all(again[key] != values[key] for key in SECRETS)


def test_make_env_never_overwrites_an_existing_env(make_env, tmp_path):
    env_file = tmp_path / ".env"
    own = "# мой .env\nJWT_SECRET=\nSERVICE_JWT_SECRET=kept-as-is\n"
    env_file.write_text(own)

    proc = make_env(env_file)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert env_file.read_text() == own
    assert "already exists — left unchanged" in proc.stdout
    assert re.search(r"empty or missing in .*: JWT_SECRET MOBILE_JWT_SECRET$",
                     proc.stdout, re.MULTILINE), proc.stdout
    assert [p.name for p in tmp_path.iterdir()] == [".env"]   # и без временных файлов


def test_make_env_warns_about_equal_keys_in_an_existing_env(make_env, tmp_path):
    """Ключи разных назначений не должны совпадать: с JWT_SECRET ==
    MOBILE_JWT_SECRET держатель ключа клиентских токенов (IdP, в демо — любой
    с .env) подписал бы admin-токен. Сервисы видят только свои ключи и
    совпадения не заметят — предупреждает make env."""
    env_file = tmp_path / ".env"
    same, other = "a" * 64, "b" * 64
    own = f"JWT_SECRET={same}\nSERVICE_JWT_SECRET={other}\nMOBILE_JWT_SECRET={same}\n"
    env_file.write_text(own)

    proc = make_env(env_file)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert env_file.read_text() == own
    assert re.search(r"^!! equal keys in .*: JWT_SECRET=MOBILE_JWT_SECRET$",
                     proc.stdout, re.MULTILINE), proc.stdout
    assert same not in proc.stdout                      # значение не печатается


# ---------------------------------------------------------------------------
# docker compose
# ---------------------------------------------------------------------------
def test_compose_refuses_to_start_without_env(compose, tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    for env_file in (empty, ENV_EXAMPLE):     # нет .env / скопирован .env.example
        proc = compose(env_file, "config", "-q")
        assert proc.returncode != 0
        assert re.search(r"required variable (JWT_SECRET|SERVICE_JWT_SECRET|"
                         r"MOBILE_JWT_SECRET) is missing a value: run make env",
                         proc.stderr), proc.stderr


@pytest.mark.parametrize("key", KEYS)
def test_compose_requires_each_key(compose, generated_env, tmp_path, key):
    env_file = tmp_path / ".env"
    env_file.write_text(re.sub(rf"(?m)^{key}=.*$", f"{key}=", generated_env.read_text()))

    proc = compose(env_file, "config", "-q")

    assert proc.returncode != 0
    assert f"required variable {key} is missing a value: run make env" in proc.stderr


def test_each_service_gets_only_its_own_secrets(compose, generated_env):
    proc = compose(generated_env, "config", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    services = json.loads(proc.stdout)["services"]
    values = _parse_env(generated_env)
    # Профили включены: проверяются и ml-training, и celery-воркер Airflow.
    assert {"ml-training", "airflow-worker", *EXPECTED_SECRETS} <= set(services)

    for name, service in services.items():
        env = service.get("environment") or {}
        expected = EXPECTED_SECRETS.get(name, set())
        assert {key for key in env if key in SECRETS} == expected, name
        for key in expected:
            assert env[key] == values[key], (name, key)
        # Ни значение чужого ключа, ни пароли — ни в env, ни в command и т.п.
        rendered = json.dumps(service)
        leaked = [key for key in SECRETS if key not in expected and values[key] in rendered]
        assert not leaked, (name, leaked)

    for name in EXPECTED_SECRETS:
        assert services[name]["environment"]["CORS_ORIGINS"] == CORS_ORIGINS, name
    assert services["mobile-api"]["environment"]["MOBILE_JWT_ISSUER"] == "cashback-demo-idp"
    cm = services["campaign-manager"]["environment"]
    assert cm["RECOMMENDATION_API_URL"] == "http://recommendation-api:8001"
    assert cm["JWT_SECRET"] != cm["SERVICE_JWT_SECRET"]


def test_login_throttle_trusts_exactly_the_frontend_proxy(compose, generated_env):
    """UI-запросы приходят в campaign-manager от nginx фронта, и uvicorn
    (--proxy-headers) берёт клиента из X-Forwarded-For только от адресов
    FORWARDED_ALLOW_IPS. Прежний 127.0.0.1 с nginx не совпадал: троттлинг
    логина считал всех пользователей UI одним IP (5 неверных паролей
    блокировали email для всех). Доверять нужно ровно адресу nginx, не
    подсети: с адреса шлюза приходят подключения к опубликованному :8002."""
    proc = compose(generated_env, "config", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    config = json.loads(proc.stdout)
    (ipam,) = config["networks"]["cashback-net"]["ipam"]["config"]
    services = config["services"]
    pinned = services["frontend"]["networks"]["cashback-net"]["ipv4_address"]

    assert services["campaign-manager"]["environment"]["FORWARDED_ALLOW_IPS"] == pinned
    subnet, dynamic = (ipaddress.ip_network(ipam[k]) for k in ("subnet", "ip_range"))
    address = ipaddress.ip_address(pinned)
    assert address in subnet and dynamic.subnet_of(subnet)
    assert address not in dynamic         # динамический адрес его не займёт
    assert address != ipaddress.ip_address(ipam["gateway"])
    others = [name for name, svc in services.items() if name != "frontend"
              and ((svc.get("networks") or {}).get("cashback-net") or {})
              .get("ipv4_address") == pinned]
    assert not others


# ---------------------------------------------------------------------------
# scripts/init_db.sh → migrations
# ---------------------------------------------------------------------------
# Команды, которые init_db.sh берёт из PATH (кроме docker / curl / alembic —
# у теста свои заглушки). Ни одного alembic: иначе скрипт выберет локальный.
INIT_DB_TOOLS = ("dirname", "grep", "head", "seq", "sleep", "sort", "basename")


@pytest.fixture()
def init_db(require_tool, tmp_path):
    """Запуск копии scripts/init_db.sh в песочнице: свой корень (.env,
    пустые каталоги миграций) и PATH только из заглушек и INIT_DB_TOOLS.
    Заглушки пишут argv и видимый им ADMIN_BOOTSTRAP_PASSWORD в лог."""
    bash = require_tool("bash")
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "db_migrations").mkdir()
    (root / "infrastructure" / "clickhouse" / "migrations").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "init_db.sh", root / "scripts" / "init_db.sh")
    tools, stubs, log = tmp_path / "tools", tmp_path / "stubs", tmp_path / "calls.log"
    tools.mkdir()
    stubs.mkdir()
    for tool in INIT_DB_TOOLS:
        (tools / tool).symlink_to(require_tool(tool))

    def _stub(name: str, stdout: str = "") -> None:
        path = stubs / name
        path.write_text(
            f"#!{bash}\n"
            f'printf "%s\\n" "{name} $*" >> "{log}"\n'
            f'printf "%s\\n" "  ADMIN_BOOTSTRAP_PASSWORD=${{ADMIN_BOOTSTRAP_PASSWORD-<unset>}}" '
            f'>> "{log}"\n'
            f'printf "%s\\n" "  DEMO_USERS_PASSWORD=${{DEMO_USERS_PASSWORD-<unset>}}" '
            f'>> "{log}"\n'
            + ('[ "$1" = network ] && echo cashback_cashback-net\n' if name == "docker" else "")
            + (f"echo {stdout}\n" if stdout else "")
            + "exit 0\n")
        path.chmod(0o755)

    _stub("docker")
    _stub("curl", stdout="Ok.")

    def _run(dotenv: str, *, local_alembic: bool) -> tuple[subprocess.CompletedProcess, str]:
        (root / ".env").write_text(dotenv)
        if local_alembic:
            _stub("alembic")
        proc = subprocess.run(
            [bash, str(root / "scripts" / "init_db.sh")],
            env=_scrubbed_env(PATH=f"{stubs}:{tools}", HOME=str(tmp_path)),
            capture_output=True, text=True, timeout=60,
        )
        return proc, log.read_text() if log.exists() else ""

    return _run


DOTENV_PASSWORD = "Bootstrap-from-dotenv-42"
DOTENV_DEMO_PASSWORD = "Demo-users-from-dotenv-42"


def test_init_db_passes_bootstrap_password_to_the_alembic_container(init_db):
    proc, calls = init_db(f"POSTGRES_DB=cashback\nADMIN_BOOTSTRAP_PASSWORD={DOTENV_PASSWORD}\n"
                          f"DEMO_USERS_PASSWORD={DOTENV_DEMO_PASSWORD}\n",
                          local_alembic=False)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    run = re.search(r"^docker run .*\n  ADMIN_BOOTSTRAP_PASSWORD=(.*)$", calls, re.MULTILINE)
    assert run, calls
    # `-e ИМЯ` без значения: docker берёт его из своего окружения, а пароль
    # не попадает в командную строку (ps, логи).
    assert " -e ADMIN_BOOTSTRAP_PASSWORD " in run.group(0), run.group(0)
    assert DOTENV_PASSWORD not in run.group(0).splitlines()[0]
    assert run.group(1) == DOTENV_PASSWORD
    assert "alembic upgrade head" in run.group(0)
    # 008 заменяет им публичные пароли демо-логинов старого сидера.
    assert " -e DEMO_USERS_PASSWORD " in run.group(0)
    assert DOTENV_DEMO_PASSWORD not in run.group(0)
    assert f"  DEMO_USERS_PASSWORD={DOTENV_DEMO_PASSWORD}\n" in calls


def test_init_db_passes_bootstrap_password_to_local_alembic(init_db):
    proc, calls = init_db(f"ADMIN_BOOTSTRAP_PASSWORD={DOTENV_PASSWORD}\n", local_alembic=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(rf"^alembic upgrade head\n  ADMIN_BOOTSTRAP_PASSWORD={DOTENV_PASSWORD}$",
                     calls, re.MULTILINE), calls
    assert "docker run" not in calls


def test_init_db_without_bootstrap_password_points_at_the_printed_one(init_db):
    proc, calls = init_db("ADMIN_BOOTSTRAP_PASSWORD=\n", local_alembic=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"^alembic upgrade head\n  ADMIN_BOOTSTRAP_PASSWORD=$", calls,
                     re.MULTILINE), calls
    assert "'bootstrap admin password:' line above" in proc.stdout
