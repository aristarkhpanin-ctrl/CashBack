"""Лёгкие фейки инфраструктуры для HTTP-тестов (см. conftest.py)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import sqlalchemy as sa
from redis.exceptions import ResponseError
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

    ``SELECT … FOR UPDATE`` пишется в ``locked``; ``on_lock`` (если задан)
    вызывается до чтения строк — так тест моделирует чужую транзакцию,
    закоммиченную, пока запрос ждал блокировку.
    """

    def __init__(self) -> None:
        self.tables: dict[type, dict[Any, Any]] = {}
        self.commits = 0
        self.gets: list[tuple[type, Any]] = []      # журнал запросов по PK
        self.locked: list[tuple[type, Any]] = []    # журнал SELECT … FOR UPDATE
        self.on_lock: Any = None

    # -- наполнение / проверки в тестах ------------------------------------
    def put(self, *objs: Any) -> None:
        for obj in objs:
            mapper = sa.inspect(type(obj))
            # Как INSERT в настоящей БД: незаданные колонки получают скалярные
            # ``default=`` модели (token_version=0, is_active=True, …).
            for attr in mapper.column_attrs:
                default = attr.columns[0].default
                if (default is not None and default.is_scalar
                        and getattr(obj, attr.key) is None):
                    setattr(obj, attr.key, default.arg)
            pk = mapper.primary_key[0].key
            self.tables.setdefault(type(obj), {})[getattr(obj, pk)] = obj

    def rows(self, model: type) -> list[Any]:
        return list(self.tables.get(model, {}).values())

    # -- API AsyncSession ----------------------------------------------------
    async def execute(self, stmt, *_a, **_k) -> FakeResult:
        assert isinstance(stmt, Select), f"unsupported statement: {stmt!r}"
        (desc,) = stmt.column_descriptions
        model = desc["entity"]
        crit = stmt.whereclause
        if stmt._for_update_arg is not None:
            self.locked.append((model, crit.right.value if crit is not None else None))
            if self.on_lock is not None:
                self.on_lock()
        rows = self.rows(model)
        if crit is not None:
            assert crit.operator is operators.eq, f"unsupported WHERE: {crit}"
            col, value = crit.left.key, crit.right.value
            rows = [r for r in rows if getattr(r, col) == value]
        if desc["expr"] is not model:                 # select(Model.col)
            rows = [getattr(r, desc["name"]) for r in rows]
        return FakeResult(rows)

    async def get(self, model: type, pk: Any):
        self.gets.append((model, pk))
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
    """Подмножество redis.asyncio (decode_responses=True): ``data`` —
    ключ → (TTL в секундах или None, строковое значение). Время не течёт:
    TTL лишь запоминается, истечение окна тест моделирует сам."""

    def __init__(self, *, fail: bool = False, version: tuple[int, int] = (7, 2)) -> None:
        self.fail = fail
        self.version = version          # EXPIRE … NX появился в Redis 7.0
        self.data: dict[str, tuple[int | None, str]] = {}

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis is down")

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._check()
        self.data[key] = (ttl, value)

    async def set(self, key: str, value: str, ex: int | None = None,
                  nx: bool = False) -> bool | None:
        self._check()
        if nx and key in self.data:
            return None                     # как redis-py: NX не сработал
        self.data[key] = (ex, str(value))
        return True

    async def getdel(self, key: str) -> str | None:
        self._check()
        item = self.data.pop(key, None)
        return item[1] if item else None

    async def delete(self, *keys: str) -> int:
        self._check()
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def exists(self, *keys: str) -> int:
        self._check()
        return sum(k in self.data for k in keys)

    async def incr(self, key: str) -> int:
        self._check()
        ttl, value = self.data.get(key, (None, "0"))
        n = int(value) + 1
        self.data[key] = (ttl, str(n))
        return n

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        self._check()
        if nx and self.version < (7, 0):
            raise ResponseError("wrong number of arguments for 'expire' command")
        if key not in self.data:
            return False
        ttl, value = self.data[key]
        if nx and ttl is not None:
            return False
        self.data[key] = (seconds, value)
        return True

    async def ttl(self, key: str) -> int:
        self._check()
        if key not in self.data:
            return -2
        ttl = self.data[key][0]
        return -1 if ttl is None else ttl
