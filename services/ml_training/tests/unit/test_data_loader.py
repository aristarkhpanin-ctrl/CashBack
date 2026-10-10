"""Unit tests for TrainingDataLoader: DSN handling and dataset assembly.

PostgreSQL and ClickHouse are replaced by fakes; the real PG query is
exercised in ``tests/integration/test_data_loader_pg.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from app.data_loader import (
    PG_RECOMMENDATIONS_SQL,
    TERMINAL_STATUSES,
    TrainingDataLoader,
)


class FakeCH:
    """Minimal stand-in for ``clickhouse_connect`` client."""

    def __init__(self, df: pd.DataFrame | None) -> None:
        self._df = df
        self.queries: list[str] = []

    def query_df(self, sql: str) -> pd.DataFrame | None:
        self.queries.append(sql)
        return self._df


def _recs() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "recommendation_id": ["r1", "r2", "r3", "r4"],
            "user_id": ["u1", "u1", "u2", "u3"],
            "campaign_id": ["c1", "c2", "c1", "c1"],
            "mcc_code": ["5411", "581", "5541", "5411"],
            "model_score": [0.9, 0.2, 0.5, 0.7],
            "age_seconds": [10.0, 20.0, 30.0, 40.0],
            "response_status": ["ACCEPTED", "DECLINED", "EXPIRED", "ACCEPTED"],
        }
    )


def _rfm() -> pd.DataFrame:
    return pd.DataFrame(
        {
            # u1 twice: ReplacingMergeTree duplicate, the later row must win.
            "user_id": ["u1", "u1", "u2"],
            "computed_at": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-02"]),
            "rfm_score": [1, 5, 3],
            "freq_5411": ["2", "7", "not-a-number"],
        }
    )


def _loader(ch: FakeCH, recs: pd.DataFrame) -> TrainingDataLoader:
    loader = TrainingDataLoader(ch_client=ch, pg_dsn="postgresql://x@h/db")
    loader._load_recommendations = lambda: recs  # type: ignore[method-assign]
    return loader


# ---------------------------------------------------------------------------
# PostgreSQL connection: SQLAlchemy-style DSN must reach libpq in plain form
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        # Helm/compose hand every service the async SQLAlchemy URL.
        (
            "postgresql+asyncpg://cashback:p%40ss@pg:5432/cashback?sslmode=require",
            "postgresql://cashback:p%40ss@pg:5432/cashback?sslmode=require",
        ),
        (
            "postgresql+psycopg2://cashback:cashback@pg/cashback",
            "postgresql://cashback:cashback@pg/cashback",
        ),
        ("postgres+asyncpg://u@h/db", "postgres://u@h/db"),
        # Already libpq-compatible forms are passed through untouched.
        ("postgresql://u:pw@h:5432/db", "postgresql://u:pw@h:5432/db"),
        ("host=h port=5432 dbname=db user=u", "host=h port=5432 dbname=db user=u"),
    ],
)
def test_load_recommendations_connects_with_libpq_dsn(dsn: str, expected: str):
    loader = TrainingDataLoader(ch_client=FakeCH(None), pg_dsn=dsn)
    conn = MagicMock()
    with (
        patch("psycopg2.connect", return_value=conn) as connect,
        patch("app.data_loader.pd.read_sql", return_value=_recs()) as read_sql,
    ):
        df = loader._load_recommendations()

    connect.assert_called_once_with(expected)
    read_sql.assert_called_once()
    sql, used_conn = read_sql.call_args.args
    assert sql == PG_RECOMMENDATIONS_SQL
    assert used_conn is conn.__enter__.return_value
    assert read_sql.call_args.kwargs["params"] == (list(TERMINAL_STATUSES),)
    assert len(df) == 4


def test_terminal_statuses_exclude_pending():
    assert "PENDING" not in TERMINAL_STATUSES
    assert set(TERMINAL_STATUSES) == {"ACCEPTED", "DECLINED", "EXPIRED", "SNOOZE"}


# ---------------------------------------------------------------------------
# ClickHouse RFM features
# ---------------------------------------------------------------------------
def test_load_rfm_keeps_latest_row_per_user_and_uses_window():
    ch = FakeCH(_rfm())
    loader = TrainingDataLoader(ch_client=ch, pg_dsn="postgresql://x@h/db", rfm_window_days=30)

    df = loader.load_rfm_for_svd()

    assert "INTERVAL 30 DAY" in ch.queries[0]
    assert sorted(df["user_id"]) == ["u1", "u2"]
    assert df.set_index("user_id").loc["u1", "rfm_score"] == 5


@pytest.mark.parametrize("ch_result", [None, pd.DataFrame()])
def test_load_rfm_returns_empty_frame_when_table_empty(ch_result):
    loader = TrainingDataLoader(ch_client=FakeCH(ch_result), pg_dsn="postgresql://x@h/db")
    df = loader.load_rfm_for_svd()
    assert isinstance(df, pd.DataFrame) and df.empty


# ---------------------------------------------------------------------------
# build_pairs
# ---------------------------------------------------------------------------
def test_build_pairs_joins_labels_and_encodes_features():
    X, y = _loader(FakeCH(_rfm()), _recs()).build_pairs()

    # u3 has no RFM row → inner join drops r4; u1 keeps 2 recs, u2 one.
    assert len(X) == len(y) == 3
    assert y.tolist() == [1, 0, 0]
    assert set(X.columns) == {
        "model_score",
        "age_seconds",
        "rfm_score",
        "freq_5411",
        "mcc_code_int",
    }
    assert (X.dtypes == "float32").all()
    # mcc_code "581" is zero-padded to "0581" before hashing to int.
    assert X["mcc_code_int"].tolist() == [5411.0, 581.0, 5541.0]
    # RFM of the latest u1 snapshot; non-numeric strings coerce to 0.
    assert X["rfm_score"].tolist() == [5.0, 5.0, 3.0]
    assert X["freq_5411"].tolist() == [7.0, 7.0, 0.0]


@pytest.mark.parametrize(
    ("recs", "rfm", "message"),
    [
        (_recs().iloc[0:0], _rfm(), "no terminal-status recommendations"),
        (_recs(), pd.DataFrame(), "user_rfm_features is empty"),
        (
            _recs().assign(user_id="u-unknown"),
            _rfm(),
            "no users with both recommendations and RFM features",
        ),
    ],
)
def test_build_pairs_fails_loudly_on_missing_data(recs, rfm, message):
    with pytest.raises(RuntimeError, match=message):
        _loader(FakeCH(rfm), recs).build_pairs()
