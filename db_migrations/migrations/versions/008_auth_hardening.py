"""auth hardening: forced password change and token revocation (stage B)

``admin_users`` получает (B20, B35):

* ``must_change_password`` — access-токен такого пользователя годится только
  для ``GET /auth/me`` и ``POST /auth/change-password``;
* ``token_version`` — номер версии токенов: смена роли, деактивация и смена
  пароля увеличивают его, и все выданные токены перестают приниматься;
* ``password_changed_at``.

Существующие БД:

* строки со старым встроенным паролем ``admin`` (хэш-константа прежней 002):
  флага мало — пароль публичный, и первым его «сменит» тот, кто войдёт
  раньше владельца. Поэтому пароль заменяется на ``ADMIN_BOOTSTRAP_PASSWORD``
  (те же требования, что в 002) или, если переменная не задана, на случайный,
  напечатанный один раз («bootstrap admin password: …»), и ставится
  ``must_change_password = true``;
* демо-логины прежнего сидера (``m.sokolova@bank.ru`` / ``marketer``,
  ``d.ivanov@bank.ru`` / ``analyst`` — пароли тоже были в README; узнаются
  по хэш-константам того сидера) — так же: пароль заменяется на
  ``DEMO_USERS_PASSWORD`` (те же требования, что у сидера) или на случайный,
  напечатанный один раз («demo users password: …»), и ставится
  ``must_change_password = true``. Без этого стенд, обновлённый одним
  ``make migrate``, сохранял входы, известные всем читавшим старый README;
* bootstrap-админ, чей пароль сгенерировала 002 (пометка в
  ``alembic_bootstrap``, хэш с тех пор не менялся) → ``must_change_password``.
  Пароль из ``ADMIN_BOOTSTRAP_PASSWORD`` выбрал оператор → флаг не ставится.

``token_version`` при замене не увеличивается: колонка создаётся здесь же,
а токены, выданные до 008, не несут ``ver`` и так не принимаются.

Downgrade удаляет колонки; заменённые публичные пароли не возвращаются, а
повторный upgrade не ставит флаг заново для пароля, заменённого здесь:
он уже не публичный и не помечен 002 (случайный или выбран оператором).

Revision ID: 008
Revises: 007
Create Date: 2026-10-15
"""
from __future__ import annotations

import os
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# bcrypt("admin"), 12 раундов — встроенный пароль прежней 002 (B20).
LEGACY_ADMIN_HASH = "$2b$12$qIYXRcx2QAiOif7zODxWR.4XxKISlyUrGxt5MV8EKwKf7WA/E9hOq"
# bcrypt("marketer") и bcrypt("analyst") — демо-логины прежнего сидера
# (scripts/seed_demo_data.py до stage B, LEGACY_DEMO_PASSWORD_HASHES).
LEGACY_DEMO_HASHES = (
    "$2b$12$yygSc5CQMnJW2odxHX/31edrZY5etN6U4afFW6/NtH.JgbcEwSmNS",
    "$2b$12$7urn2rB62CxjIkZhTLw2ueE0wjoyhMn0cwC.ZCkMsS1nmIy7ad3XG",
)

BOOTSTRAP_PASSWORD_ENV = "ADMIN_BOOTSTRAP_PASSWORD"
DEMO_PASSWORD_ENV = "DEMO_USERS_PASSWORD"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72
KNOWN_DEFAULT_PASSWORDS = frozenset({
    "admin", "dev-secret-change-me", "please-change-me", "change-me",
    "changeme", "secret", "test-secret",
})


def _password_from_env(name: str, weak: frozenset[str]) -> tuple[str, bool]:
    """``(пароль, сгенерирован_ли)``: из окружения или случайный."""
    password = os.environ.get(name, "")
    if not password:
        return secrets.token_urlsafe(18), True
    if (len(password) < MIN_PASSWORD_LENGTH
            or len(password.encode()) > MAX_PASSWORD_BYTES
            or password.strip().lower() in weak):
        raise RuntimeError(
            f"{name} must be {MIN_PASSWORD_LENGTH}+ characters "
            f"(at most {MAX_PASSWORD_BYTES} bytes) and not a known default; "
            f"unset it to generate a random password"
        )
    return password, False


def bootstrap_password() -> tuple[str, bool]:
    """Пароль bootstrap-админа — как в 002 (миграции не импортируют
    друг друга: каждая — снимок на момент написания)."""
    return _password_from_env(BOOTSTRAP_PASSWORD_ENV, KNOWN_DEFAULT_PASSWORDS)


def demo_users_password() -> tuple[str, bool]:
    """Пароль демо-логинов — те же требования, что у сидера."""
    return _password_from_env(DEMO_PASSWORD_ENV,
                              KNOWN_DEFAULT_PASSWORDS | {"marketer", "analyst"})


def _replace_public_passwords(bind, hashes: tuple[str, ...], choose, label: str) -> None:
    """Строки с публичным паролем (хэш из ``hashes``) получают новый пароль
    и ``must_change_password``: первым его «сменил» бы тот, кто войдёт
    раньше владельца. Пароль выбирается, только если такие строки есть."""
    in_hashes = sa.bindparam("hashes", expanding=True)
    legacy = bind.execute(
        sa.text("SELECT count(*) FROM admin_users WHERE password_hash IN :hashes")
        .bindparams(in_hashes),
        {"hashes": list(hashes)},
    ).scalar_one()
    if not legacy:
        return
    password, generated = choose()
    bind.execute(
        sa.text(
            """
            UPDATE admin_users
               SET password_hash = crypt(:password, gen_salt('bf', 12)),
                   must_change_password = true,
                   password_changed_at = now()
             WHERE password_hash IN :hashes
            """
        ).bindparams(in_hashes),
        {"password": password, "hashes": list(hashes)},
    )
    if generated:
        print(f"{label} password: {password}", flush=True)


def upgrade() -> None:
    op.add_column("admin_users", sa.Column(
        "must_change_password", sa.Boolean(), nullable=False,
        server_default=sa.text("false")))
    op.add_column("admin_users", sa.Column(
        "token_version", sa.Integer(), nullable=False,
        server_default=sa.text("0")))
    op.add_column("admin_users", sa.Column(
        "password_changed_at", sa.TIMESTAMP(timezone=True), nullable=True))

    bind = op.get_bind()
    _replace_public_passwords(bind, (LEGACY_ADMIN_HASH,), bootstrap_password,
                              "bootstrap admin")
    _replace_public_passwords(bind, LEGACY_DEMO_HASHES, demo_users_password,
                              "demo users")

    if sa.inspect(bind).has_table("alembic_bootstrap"):
        bind.execute(sa.text(
            """
            UPDATE admin_users AS a
               SET must_change_password = true
              FROM alembic_bootstrap AS b
             WHERE a.email = b.email AND a.password_hash = b.password_hash
            """
        ))


def downgrade() -> None:
    op.drop_column("admin_users", "password_changed_at")
    op.drop_column("admin_users", "token_version")
    op.drop_column("admin_users", "must_change_password")
