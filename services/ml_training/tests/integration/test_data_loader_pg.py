"""B44: the training loader's real SQL against PostgreSQL at alembic head.

Opt-in (excluded from the unit gate by ``-m "not integration"``). Point
``ML_IT_POSTGRES_DSN`` at a throw-away database (URL form, any SQLAlchemy
driver suffix is fine); the fixture migrates it to head with the repo's
``db_migrations`` and every test seeds and removes its own rows::

    ML_IT_POSTGRES_DSN=postgresql://cashback:cashback@127.0.0.1:5432/cashback_it \\
    python -m pytest tests/integration -m integration
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest
from app.data_loader import TrainingDataLoader

REPO = Path(__file__).resolve().parents[4]
MIGRATIONS = REPO / "db_migrations"

DSN = os.getenv("ML_IT_POSTGRES_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="ML_IT_POSTGRES_DSN is not set"),
]


def _plain(dsn: str) -> str:
    """``postgresql+<driver>://…`` → ``postgresql://…`` (for psycopg2/alembic)."""
    return re.sub(r"^postgres(?:ql)?\+\w+://", "postgresql://", dsn)


class FakeCH:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def query_df(self, sql: str) -> pd.DataFrame:
        return self._df


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """Plain libpq URL of a database migrated to alembic head."""
    dsn = _plain(DSN or "")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=MIGRATIONS,
        env={**os.environ, "POSTGRES_DSN": dsn},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"alembic upgrade head failed:\n{proc.stderr}"
    return dsn


@pytest.fixture
def seeded(pg_dsn):
    """Two users, one campaign and recommendations in every status.

    Yields ``{"users": [...], "in_window": {rec_id: status}}`` — the rows the
    loader must return; a PENDING one and a 200-day-old ACCEPTED one must not
    come back. Everything is deleted afterwards (FKs cascade from users and
    the campaign).
    """
    import psycopg2

    users = [str(uuid.uuid4()), str(uuid.uuid4())]
    campaign = str(uuid.uuid4())
    in_window = {
        str(uuid.uuid4()): ("ACCEPTED", users[0], "5411", 0),
        str(uuid.uuid4()): ("DECLINED", users[0], "5812", 1),
        str(uuid.uuid4()): ("EXPIRED", users[1], "5411", 2),
        str(uuid.uuid4()): ("SNOOZE", users[1], "5541", 3),
    }
    excluded = {
        str(uuid.uuid4()): ("PENDING", users[0], "5411", 0),
        str(uuid.uuid4()): ("ACCEPTED", users[1], "5812", 200),
    }

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            for uid in users:
                cur.execute(
                    "INSERT INTO users (user_id, external_id) VALUES (%s, %s)",
                    (uid, f"ml-it-{uid}"),
                )
            cur.execute(
                """
                INSERT INTO cashback_campaigns
                    (campaign_id, name, cashback_rate, budget_total, status,
                     start_date, end_date)
                VALUES (%s, 'ml-it', 5.00, 100000, 'ACTIVE',
                        now() - INTERVAL '365 days', now() + INTERVAL '30 days')
                """,
                (campaign,),
            )
            for rec_id, (status, uid, mcc, days_ago) in {**in_window, **excluded}.items():
                cur.execute(
                    """
                    INSERT INTO recommendations
                        (recommendation_id, user_id, campaign_id, mcc_code,
                         model_score, generated_at, expires_at, response_status)
                    VALUES (%s, %s, %s, %s, 0.5,
                            now() - make_interval(days => %s),
                            now() - make_interval(days => %s) + INTERVAL '1 day',
                            %s)
                    """,
                    (rec_id, uid, campaign, mcc, days_ago, days_ago, status),
                )
        yield {"users": users, "in_window": {k: v[0] for k, v in in_window.items()}}
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM cashback_campaigns WHERE campaign_id = %s", (campaign,))
            cur.execute("DELETE FROM users WHERE user_id = ANY(%s::uuid[])", (users,))
        conn.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_loader_query_returns_terminal_recommendations(pg_dsn, seeded):
    """The enum column is filtered by a text[] parameter — must not raise
    ``operator does not exist: recommendation_response_status = text``."""
    loader = TrainingDataLoader(ch_client=None, pg_dsn=pg_dsn)

    df = loader._load_recommendations()

    ours = df[df["user_id"].isin(seeded["users"])]
    assert dict(zip(ours["recommendation_id"], ours["response_status"])) == seeded["in_window"]
    assert ours["mcc_code"].str.len().eq(4).all()
    assert (ours["age_seconds"] >= 0).all()


def test_build_pairs_with_sqlalchemy_asyncpg_dsn(pg_dsn, seeded):
    """Helm/compose pass ``postgresql+asyncpg://…`` (shared with the async
    services); the loader must still connect via psycopg2."""
    asyncpg_dsn = "postgresql+asyncpg://" + pg_dsn.split("://", 1)[1]
    rfm = pd.DataFrame(
        {
            "user_id": seeded["users"],
            "computed_at": pd.Timestamp.now(),
            "rfm_score": [5, 2],
        }
    )
    loader = TrainingDataLoader(ch_client=FakeCH(rfm), pg_dsn=asyncpg_dsn)

    X, y = loader.build_pairs()

    assert len(X) == len(y) == len(seeded["in_window"])
    assert int(y.sum()) == 1  # the only in-window ACCEPTED
    assert {"model_score", "age_seconds", "rfm_score", "mcc_code_int"} <= set(X.columns)
