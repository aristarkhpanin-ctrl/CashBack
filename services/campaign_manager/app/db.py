"""SQLAlchemy async engine + Base for the Campaign Manager."""
from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from starlette.requests import Request


class Base(DeclarativeBase):
    pass


def make_engine(dsn: str):
    return create_async_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session_dep(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a session bound to the running engine.

    Аннотация ``Request`` обязательна: без неё FastAPI считает ``request``
    обязательным query-параметром и каждый эндпоинт с БД отвечает 422 (B7).
    """
    sm = request.app.state.sessionmaker
    async with sm() as session:
        yield session
