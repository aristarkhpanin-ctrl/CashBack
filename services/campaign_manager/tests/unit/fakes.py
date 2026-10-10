"""Лёгкие фейки инфраструктуры для HTTP-тестов (см. conftest.py)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.sql import operators
from sqlalchemy.sql.selectable import Select


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalar_one_or_none(self):
        assert len(self._rows) <= 1, f"expected ≤1 row, got {len(self._rows)}"
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """In-memory замена ``AsyncSession``: таблица = dict pk → ORM-объект.

    Понимает ровно те запросы, что используют auth / ml_limits / roles / rbac:
    ``select(Model)``, ``select(Model.col)``, необязательный ``.where(Model.col
    == value)`` и ``.order_by(...)`` (порядок = порядок вставки), а также
    ``get`` / ``add`` / ``commit``. Любой другой запрос — ``AssertionError``,
    чтобы тест не «зеленел» на непонятом фейком SQL.
    """

    def __init__(self) -> None:
        self.tables: dict[type, dict[Any, Any]] = {}
        self.commits = 0

    # -- наполнение / проверки в тестах ------------------------------------
    def put(self, *objs: Any) -> None:
        for obj in objs:
            pk = sa.inspect(type(obj)).primary_key[0].key
            self.tables.setdefault(type(obj), {})[getattr(obj, pk)] = obj

    def rows(self, model: type) -> list[Any]:
        return list(self.tables.get(model, {}).values())

    # -- API AsyncSession ----------------------------------------------------
    async def execute(self, stmt, *_a, **_k) -> FakeResult:
        assert isinstance(stmt, Select), f"unsupported statement: {stmt!r}"
        (desc,) = stmt.column_descriptions
        model = desc["entity"]
        rows = self.rows(model)
        crit = stmt.whereclause
        if crit is not None:
            assert crit.operator is operators.eq, f"unsupported WHERE: {crit}"
            col, value = crit.left.key, crit.right.value
            rows = [r for r in rows if getattr(r, col) == value]
        if desc["expr"] is not model:                 # select(Model.col)
            rows = [getattr(r, desc["name"]) for r in rows]
        return FakeResult(rows)

    async def get(self, model: type, pk: Any):
        return self.tables.get(model, {}).get(pk)

    def add(self, obj: Any) -> None:
        self.put(obj)

    async def commit(self) -> None:
        self.commits += 1


def fake_sessionmaker(session: FakeSession):
    """Аналог ``async_sessionmaker``: ``async with sm() as s`` → ``session``."""
    @asynccontextmanager
    async def _sm():
        yield session
    return _sm


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.data: dict[str, tuple[int, str]] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.data[key] = (ttl, value)
