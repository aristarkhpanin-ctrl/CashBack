"""Integration: campaign CRUD + filtering against a real Postgres.

Eight tests using a minimal subset of the production schema.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
async def schema(postgres_dsn):
    """Provision tables once per module so tests share a clean schema."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_dsn)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"))
        for stmt in [
            """DO $$BEGIN
                 CREATE TYPE campaign_status AS ENUM
                     ('DRAFT','ACTIVE','PAUSED','COMPLETED');
               EXCEPTION WHEN duplicate_object THEN null; END$$;""",
            """CREATE TABLE IF NOT EXISTS cashback_campaigns (
                 campaign_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                 name TEXT NOT NULL,
                 target_segment_ids INT[] NOT NULL,
                 cashback_rate NUMERIC(5,2) NOT NULL,
                 budget_total NUMERIC(15,2) NOT NULL,
                 budget_spent NUMERIC(15,2) DEFAULT 0,
                 status campaign_status DEFAULT 'DRAFT',
                 start_date TIMESTAMPTZ NOT NULL,
                 end_date TIMESTAMPTZ NOT NULL,
                 allowed_channels TEXT[] DEFAULT '{}'
               )""",
            """CREATE TABLE IF NOT EXISTS campaign_categories (
                 campaign_id UUID REFERENCES cashback_campaigns ON DELETE CASCADE,
                 mcc_code CHAR(4),
                 PRIMARY KEY(campaign_id, mcc_code)
               )""",
        ]:
            await conn.execute(text(stmt))
    yield engine
    await engine.dispose()


async def _insert(engine, **overrides):
    """Insert a campaign row directly via SQL — keeps tests independent of the API."""
    from sqlalchemy import text
    cid = overrides.pop("campaign_id", uuid.uuid4())
    row = {
        "name":   overrides.pop("name", f"camp-{uuid.uuid4().hex[:6]}"),
        "segs":   overrides.pop("target_segment_ids", [1, 2, 3]),
        "rate":   overrides.pop("cashback_rate", "5.00"),
        "budget": overrides.pop("budget_total", "10000"),
        "spent":  overrides.pop("budget_spent",  "0"),
        "status": overrides.pop("status", "ACTIVE"),
        "s":      overrides.pop("start_date",
                                datetime.now(timezone.utc) - timedelta(days=1)),
        "e":      overrides.pop("end_date",
                                datetime.now(timezone.utc) + timedelta(days=30)),
        "chans":  overrides.pop("allowed_channels", ["POS", "ONLINE"]),
        "mccs":   overrides.pop("mcc_codes", ["5411"]),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text("""INSERT INTO cashback_campaigns
                       (campaign_id, name, target_segment_ids, cashback_rate,
                        budget_total, budget_spent, status, start_date,
                        end_date, allowed_channels)
                    VALUES (:cid, :name, :segs, :rate, :budget, :spent,
                            :status::campaign_status, :s, :e, :chans)"""),
            {"cid": str(cid), **row},
        )
        for mcc in row["mccs"]:
            await conn.execute(
                text("INSERT INTO campaign_categories VALUES (:c, :m) "
                     "ON CONFLICT DO NOTHING"),
                {"c": str(cid), "m": mcc},
            )
    return str(cid)


# ---------------------------------------------------------------------------
async def test_insert_and_read_back_campaign(schema):
    from sqlalchemy import text
    cid = await _insert(schema, name="alpha")
    async with schema.connect() as conn:
        row = (await conn.execute(
            text("SELECT name, status::text AS status FROM cashback_campaigns "
                 "WHERE campaign_id = :c"),
            {"c": cid},
        )).one()
    assert row.name == "alpha"
    assert row.status == "ACTIVE"


async def test_filter_active_only(schema):
    """Insert ACTIVE + PAUSED — query returns only ACTIVE."""
    from sqlalchemy import text
    await _insert(schema, status="ACTIVE", name="active-1")
    await _insert(schema, status="PAUSED", name="paused-1")
    async with schema.connect() as conn:
        rows = (await conn.execute(
            text("SELECT name FROM cashback_campaigns WHERE status = 'ACTIVE'"),
        )).scalars().all()
    assert "active-1" in rows
    assert "paused-1" not in rows


async def test_filter_by_segment_membership(schema):
    """target_segment_ids = ANY filter — listing 3.10 mechanics."""
    from sqlalchemy import text
    await _insert(schema, target_segment_ids=[1, 5, 9], name="seg-A")
    await _insert(schema, target_segment_ids=[7, 8, 9], name="seg-B")
    async with schema.connect() as conn:
        rows = (await conn.execute(
            text("SELECT name FROM cashback_campaigns "
                 "WHERE 5 = ANY(target_segment_ids)"),
        )).scalars().all()
    assert "seg-A" in rows
    assert "seg-B" not in rows


async def test_remaining_budget_filter(schema):
    """budget_total - budget_spent > min_award."""
    from sqlalchemy import text
    await _insert(schema, name="rich",   budget_total="100000", budget_spent="100")
    await _insert(schema, name="empty",  budget_total="100",    budget_spent="100")
    async with schema.connect() as conn:
        rows = (await conn.execute(
            text("SELECT name FROM cashback_campaigns "
                 "WHERE (budget_total - budget_spent) > :min"),
            {"min": 1.0},
        )).scalars().all()
    assert "rich" in rows
    assert "empty" not in rows


async def test_status_transition_draft_to_active(schema):
    from sqlalchemy import text
    cid = await _insert(schema, status="DRAFT")
    async with schema.begin() as conn:
        await conn.execute(
            text("UPDATE cashback_campaigns SET status='ACTIVE' "
                 "WHERE campaign_id = :c"),
            {"c": cid},
        )
    async with schema.connect() as conn:
        st = (await conn.execute(
            text("SELECT status::text FROM cashback_campaigns WHERE campaign_id=:c"),
            {"c": cid},
        )).scalar_one()
    assert st == "ACTIVE"


async def test_invalid_status_value_is_rejected(schema):
    from sqlalchemy import text
    cid = await _insert(schema)
    with pytest.raises(Exception):
        async with schema.begin() as conn:
            await conn.execute(
                text("UPDATE cashback_campaigns SET status = 'NUKED' "
                     "WHERE campaign_id = :c"),
                {"c": cid},
            )


async def test_campaign_categories_join(schema):
    from sqlalchemy import text
    cid = await _insert(schema, mcc_codes=["5411", "5812"])
    async with schema.connect() as conn:
        rows = (await conn.execute(
            text("SELECT mcc_code FROM campaign_categories WHERE campaign_id=:c"),
            {"c": cid},
        )).scalars().all()
    assert {r.strip() for r in rows} == {"5411", "5812"}


async def test_select_for_update_on_budget(schema):
    """The budget reservation rule relies on FOR UPDATE — verify it works."""
    from sqlalchemy import text
    cid = await _insert(schema, budget_total="10000", budget_spent="0")
    async with schema.begin() as conn:
        row = (await conn.execute(
            text("""SELECT budget_total, budget_spent
                      FROM cashback_campaigns
                     WHERE campaign_id = :c
                     FOR UPDATE"""),
            {"c": cid},
        )).one()
        assert Decimal(row.budget_total) - Decimal(row.budget_spent) == Decimal("10000")
        await conn.execute(
            text("UPDATE cashback_campaigns SET budget_spent = budget_spent + 100 "
                 "WHERE campaign_id = :c"),
            {"c": cid},
        )
    async with schema.connect() as conn:
        spent = (await conn.execute(
            text("SELECT budget_spent FROM cashback_campaigns WHERE campaign_id=:c"),
            {"c": cid},
        )).scalar_one()
    assert Decimal(spent) == Decimal("100")
