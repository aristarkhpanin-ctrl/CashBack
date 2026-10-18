"""Подделки внешних клиентов recommendation_api для HTTP-тестов.

Маршруты, конвейер и BRE в тестах — настоящие; подменены только модель,
генератор кандидатов, Postgres и Kafka. Redis — fakeredis с журналом
команд: по нему тесты видят любую запись.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import numpy as np
from fakeredis import aioredis as fake_aioredis

USER_ID = "11111111-1111-1111-1111-111111111111"
CANDIDATES = ["5812", "5411", "5541"]
FEATURE_COLUMNS = ["recency_days", "frequency_total", "mcc_code_int"]
FEATURES = {"recency_days": 3, "frequency_total": 42, "segment_id": 5}


def campaign_row(mcc: str, n: int, **overrides: Any) -> dict:
    """Строка кампании в форме ответа _fetch_campaigns_for_mccs
    (столбцы eligible_campaigns, миграция 009)."""
    row = {
        "mcc_code": mcc,
        "campaign_id": f"00000000-0000-0000-0000-00000000000{n}",
        "name": f"Кампания {mcc}",
        "cashback_rate": 5.0,
        "min_transaction_amount": 100.0,
        "allowed_channels": ["ONLINE", "POS", "MOBILE"],
        "budget_total": 100_000.0,
        "budget_spent": 0.0,
        "daily_limit": None,
        "spent_today": 0.0,
    }
    row.update(overrides)
    return row


class FakeModel:
    """Детерминированный score: чем меньше MCC, тем выше вероятность."""

    def predict_proba(self, X):
        p = 1.0 / (1.0 + X["mcc_code_int"].to_numpy() / 5000.0)
        return np.column_stack([1.0 - p, p])


class FakeExplainer:
    expected_value = -1.5

    def shap_values(self, X):
        row = np.array([0.12, -0.04, 0.02])[: X.shape[1]]
        return np.tile(row, (len(X), 1))


class RecordingRedis(fake_aioredis.FakeRedis):
    """fakeredis, записывающий имя каждой выполненной команды."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[str] = []

    async def execute_command(self, *args: Any, **options: Any):
        self.commands.append(str(args[0]).upper())
        return await super().execute_command(*args, **options)

    async def snapshot(self) -> dict[str, tuple[Any, int]]:
        """{ключ: (значение, pttl)} — для сравнения «до/после»."""
        out: dict[str, tuple[Any, int]] = {}
        for key in sorted(await self.keys("*")):
            out[key] = (await self.get(key), await self.pttl(key))
        return out


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self) -> list[dict]:
        return list(self._rows)

    def first(self):
        return SimpleNamespace(**self._rows[0]) if self._rows else None


@dataclass
class FakeEngine:
    """AsyncEngine-заглушка: журнал SQL, вставленные строки и положенные
    клиенту кампании по MCC — как их отдаёт запрос к eligible_campaigns
    (одна лучшая на MCC). ``lookups`` — параметры каждого такого запроса,
    ``lookup_error`` — исключение вместо ответа (сбой Postgres)."""

    campaigns: dict[str, dict]
    statements: list[str] = field(default_factory=list)
    inserted: list[dict] = field(default_factory=list)
    lookups: list[dict] = field(default_factory=list)
    lookup_error: Exception | None = None

    def connect(self):
        return _FakeConn(self)

    def begin(self):
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, sql: Any, params: dict | None = None) -> _Result:
        query = " ".join(str(sql).split())
        self.engine.statements.append(query)
        rows = list(self.engine.campaigns.values())
        if "eligible_campaigns(" in query:
            self.engine.lookups.append(dict(params or {}))
            if self.engine.lookup_error is not None:
                raise self.engine.lookup_error
            return _Result([r for r in rows if r["mcc_code"] in params["mccs"]])
        if query.startswith("INSERT INTO recommendations"):
            self.engine.inserted.append(dict(params or {}))
        return _Result([])


@dataclass
class FakeClickHouse:
    """clickhouse_connect-заглушка: строка user_rfm_features по user_id."""

    features: dict[str, dict]
    queries: list[str] = field(default_factory=list)

    def query(self, sql: str, parameters: dict | None = None):
        self.queries.append(sql)
        row = self.features.get((parameters or {}).get("user_id"))
        return SimpleNamespace(
            column_names=list(row) if row else [],
            result_rows=[list(row.values())] if row else [],
        )


@dataclass
class FakeProducer:
    sent: list[tuple[str, bytes, dict]] = field(default_factory=list)

    async def send(self, topic: str, key: bytes, value: bytes) -> None:
        self.sent.append((topic, key, json.loads(value)))


@dataclass
class RecApp:
    client: Any
    app: Any
    redis: RecordingRedis
    db: FakeEngine
    producer: FakeProducer
