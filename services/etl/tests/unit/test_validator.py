"""Unit tests for DataValidator — covers all six rules from table 16.

We use pytest-asyncio because :meth:`DataValidator.validate_batch`
awaits :meth:`_fetch_recent_ids`. The ClickHouse client is replaced
with a tiny fake to keep the tests hermetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable

import pytest

from app.mcc_registry import MCC_REGISTRY
from app.validator import DataValidator, ValidationReport


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------
class FakeCHClient:
    """Minimal stub for clickhouse-connect: query() returns recent ids."""

    def __init__(self, recent_ids: Iterable[str] = ()):
        self._ids = list(recent_ids)

    def query(self, sql: str):
        rows = [(tid,) for tid in self._ids]
        return SimpleNamespace(result_rows=rows)


def _good_record(**overrides) -> dict:
    """Baseline record that passes every rule."""
    base = {
        "transaction_id": "tx-0001",
        "user_id": "00000000-0000-0000-0000-000000000001",
        "mcc_code": "5411",
        "amount": "1234.56",
        "currency": "RUB",
        "transaction_date": (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(timespec="seconds"),
        "channel": "POS",
        "merchant_id": "MID-1",
        "metadata": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
async def test_valid_record_passes_all_rules():
    v = DataValidator(ch_client=FakeCHClient())
    report = await v.validate_batch([_good_record()])
    assert report.errors == []
    assert len(report.valid) == 1
    assert report.error_rate() == 0.0


async def test_known_good_mcc_codes_all_pass():
    v = DataValidator(ch_client=None)
    sample_codes = list(MCC_REGISTRY)[:10]
    records = [_good_record(transaction_id=f"tx-{i}", mcc_code=c)
               for i, c in enumerate(sample_codes)]
    report = await v.validate_batch(records)
    assert len(report.valid) == 10
    assert report.errors == []


# ---------------------------------------------------------------------------
# Parametrised: every rule, both directions where useful
# ---------------------------------------------------------------------------
_FUTURE_TS = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
_OLD_TS = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")


@pytest.mark.parametrize(
    "overrides, expected_rule",
    [
        # ---- R1: NOT NULL ------------------------------------------
        ({"transaction_id": None}, "R1_not_null"),
        ({"user_id": ""}, "R1_not_null"),
        ({"channel": None}, "R1_not_null"),

        # ---- R2: amount range --------------------------------------
        ({"amount": "0"}, "R2_amount_range"),
        ({"amount": "-50"}, "R2_amount_range"),
        ({"amount": "20000000"}, "R2_amount_range"),    # > AMOUNT_MAX
        ({"amount": "not-a-number"}, "R2_amount_range"),

        # ---- R3: MCC ISO 18245 -------------------------------------
        ({"mcc_code": "999"}, "R3_mcc_iso18245"),       # 3-digit
        ({"mcc_code": "ABCD"}, "R3_mcc_iso18245"),      # non-digit
        ({"mcc_code": "0000"}, "R3_mcc_iso18245"),      # not in registry

        # ---- R4: timestamp -----------------------------------------
        ({"transaction_date": "yesterday"}, "R4_timestamp"),  # not ISO
        ({"transaction_date": _FUTURE_TS}, "R4_timestamp"),
        ({"transaction_date": _OLD_TS}, "R4_timestamp"),

        # ---- R6: ISO 4217 currency ---------------------------------
        ({"currency": "FOO"}, "R6_iso4217_currency"),
        ({"currency": "RUBL"}, "R6_iso4217_currency"),  # length != 3
        ({"currency": "12X"}, "R6_iso4217_currency"),
    ],
)
async def test_individual_rules_reject_bad_records(overrides, expected_rule):
    v = DataValidator(ch_client=None)
    record = _good_record(**overrides)
    report = await v.validate_batch([record])
    assert report.valid == []
    assert len(report.errors) == 1
    assert report.errors[0].rule == expected_rule


# ---------------------------------------------------------------------------
# R5: deduplication via ClickHouse + intra-batch
# ---------------------------------------------------------------------------
async def test_deduplication_against_clickhouse_history():
    ch = FakeCHClient(recent_ids=["tx-already-seen"])
    v = DataValidator(ch_client=ch)
    report = await v.validate_batch([_good_record(transaction_id="tx-already-seen")])
    assert len(report.errors) == 1
    assert report.errors[0].rule == "R5_deduplication"


async def test_deduplication_within_same_batch():
    v = DataValidator(ch_client=None)
    records = [
        _good_record(transaction_id="tx-dup"),
        _good_record(transaction_id="tx-dup"),  # duplicate within the batch
    ]
    report = await v.validate_batch(records)
    assert len(report.valid) == 1
    assert len(report.errors) == 1
    assert report.errors[0].rule == "R5_deduplication"


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------
async def test_error_rate_is_computed_correctly():
    v = DataValidator(ch_client=None)
    records = [
        _good_record(transaction_id="ok-1"),
        _good_record(transaction_id="ok-2"),
        _good_record(transaction_id="bad-1", amount="-10"),
    ]
    report = await v.validate_batch(records)
    assert len(report.valid) == 2
    assert len(report.errors) == 1
    assert report.error_rate() == pytest.approx(1 / 3)
    assert report.total == 3


async def test_empty_batch_has_zero_error_rate():
    v = DataValidator(ch_client=None)
    report = await v.validate_batch([])
    assert report.total == 0
    assert report.error_rate() == 0.0


# ---------------------------------------------------------------------------
# CH unavailability is tolerated
# ---------------------------------------------------------------------------
async def test_clickhouse_unreachable_does_not_break_validation():
    class BoomClient:
        def query(self, sql):
            raise RuntimeError("connection refused")

    v = DataValidator(ch_client=BoomClient())
    report = await v.validate_batch([_good_record()])
    assert isinstance(report, ValidationReport)
    assert len(report.valid) == 1
    assert report.errors == []


# ---------------------------------------------------------------------------
# Constant sanity checks (regression guards)
# ---------------------------------------------------------------------------
def test_amount_max_constant_matches_spec():
    assert DataValidator.AMOUNT_MAX == 10_000_000


def test_mcc_registry_size_at_least_100():
    assert len(MCC_REGISTRY) >= 100


def test_is_valid_mcc_helper():
    from app.mcc_registry import is_valid_mcc

    assert is_valid_mcc("5411") is True
    # Pick another one we know lives in the registry.
    assert is_valid_mcc(next(iter(MCC_REGISTRY))) is True
    assert is_valid_mcc(None) is False
    assert is_valid_mcc("") is False
    assert is_valid_mcc("12345") is False     # wrong length
    assert is_valid_mcc("ABCD") is False      # non-digit
    assert is_valid_mcc(5411) is False        # not a string
    assert is_valid_mcc("0001") is False      # not in registry


def test_rules_are_six():
    v = DataValidator(ch_client=None)
    rule_names = [name for name, _ in v.rules]
    assert rule_names == [
        "R1_not_null",
        "R2_amount_range",
        "R3_mcc_iso18245",
        "R4_timestamp",
        "R5_deduplication",
        "R6_iso4217_currency",
    ]
