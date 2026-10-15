"""Unit tests for DataValidator — covers all six rules from table 16.

We use pytest-asyncio because :meth:`DataValidator.validate_batch`
awaits :meth:`_fetch_recent_ids`. The ClickHouse client is replaced
with a tiny fake to keep the tests hermetic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable

import pytest
from app.mcc_registry import MCC_REGISTRY
from app.validator import DataValidator, ValidationReport, cleanse, parse_transaction_date


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
            datetime.now(UTC) - timedelta(hours=1)
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
_FUTURE_TS = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
_OLD_TS = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")


@pytest.mark.parametrize(
    "overrides, expected_rule",
    [
        # ---- R1: NOT NULL ------------------------------------------
        ({"transaction_id": None}, "R1_not_null"),
        ({"user_id": ""}, "R1_not_null"),
        ({"channel": None}, "R1_not_null"),
        # user_id must load into transactions_raw.user_id UUID
        ({"user_id": "user-42"}, "R1_not_null"),
        ({"user_id": "{00000000-0000-0000-0000-000000000001}"}, "R1_not_null"),
        ({"user_id": "urn:uuid:00000000-0000-0000-0000-000000000001"}, "R1_not_null"),
        ({"user_id": 42}, "R1_not_null"),

        # ---- R2: amount range --------------------------------------
        ({"amount": "0"}, "R2_amount_range"),
        ({"amount": "-50"}, "R2_amount_range"),
        ({"amount": "20000000"}, "R2_amount_range"),    # > AMOUNT_MAX
        ({"amount": "not-a-number"}, "R2_amount_range"),
        # What Python's Decimal accepts but Decimal(15, 2) cannot hold or
        # ClickHouse cannot parse — and NaN, which used to crash the rule.
        ({"amount": "NaN"}, "R2_amount_range"),
        ({"amount": "sNaN"}, "R2_amount_range"),
        ({"amount": "Infinity"}, "R2_amount_range"),
        ({"amount": "-Infinity"}, "R2_amount_range"),
        ({"amount": "1_000"}, "R2_amount_range"),
        ({"amount": "1e3"}, "R2_amount_range"),
        ({"amount": " 10.00"}, "R2_amount_range"),
        ({"amount": "+10.00"}, "R2_amount_range"),
        ({"amount": "10.005"}, "R2_amount_range"),       # ClickHouse truncates silently
        ({"amount": "\u0661\u0660"}, "R2_amount_range"),   # Arabic-Indic digits
        ({"amount": float("nan")}, "R2_amount_range"),

        # ---- R3: MCC ISO 18245 -------------------------------------
        ({"mcc_code": "999"}, "R3_mcc_iso18245"),       # 3-digit
        ({"mcc_code": "ABCD"}, "R3_mcc_iso18245"),      # non-digit
        ({"mcc_code": "0000"}, "R3_mcc_iso18245"),      # not in registry

        # ---- R4: timestamp -----------------------------------------
        ({"transaction_date": "yesterday"}, "R4_timestamp"),  # not ISO
        ({"transaction_date": _FUTURE_TS}, "R4_timestamp"),
        ({"transaction_date": _OLD_TS}, "R4_timestamp"),
        ({"transaction_date": "2026-13-01T00:00:00"}, "R4_timestamp"),
        ({"transaction_date": 1760000000}, "R4_timestamp"),

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


@pytest.mark.parametrize("amount", ["1", "10.5", "1234.56", "0.01", "10000000", "0010.50"])
async def test_plain_decimal_amounts_pass(amount):
    report = await DataValidator(ch_client=None).validate_batch([_good_record(amount=amount)])
    assert report.errors == []


async def test_uuid_case_is_irrelevant():
    rec = _good_record(user_id="AF1B185A-2EFD-4927-AE20-B5DC9AFE394E")
    assert (await DataValidator(ch_client=None).validate_batch([rec])).errors == []


async def test_iso8601_shapes_pass_and_cleanse_to_utc():
    hour_ago = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    shapes = [
        hour_ago.isoformat(),                                         # +00:00
        hour_ago.isoformat().replace("+00:00", "Z"),
        hour_ago.replace(microsecond=5).isoformat(),                  # fraction
        hour_ago.astimezone(timezone(timedelta(hours=3))).isoformat(),
        hour_ago.replace(tzinfo=None).isoformat(),                    # naive = UTC
        hour_ago.strftime("%Y%m%dT%H%M%S"),                           # basic format
        hour_ago,                                                     # already parsed
    ]
    records = [_good_record(transaction_id=f"tx-{i}", transaction_date=d)
               for i, d in enumerate(shapes)]
    report = await DataValidator(ch_client=None).validate_batch(records)
    assert report.errors == []
    cleansed = [cleanse(r)["transaction_date"] for r in report.valid]
    assert [c.replace(microsecond=0) for c in cleansed] == [hour_ago] * len(shapes)
    assert all(c.tzinfo == UTC for c in cleansed)
    assert records[0]["transaction_date"] == shapes[0]  # cleanse() copies


def test_parse_transaction_date_rejects_what_it_cannot_represent():
    assert parse_transaction_date(None) is None
    assert parse_transaction_date(12.5) is None
    assert parse_transaction_date("2026-02-30") is None
    assert parse_transaction_date("0001-01-01T00:00:00+05:00") is None  # before year 1 in UTC


async def test_a_rule_that_raises_rejects_the_record_instead_of_the_batch(caplog):
    class Exploding(DataValidator):
        @staticmethod
        def _check_mcc(rec):
            if rec["transaction_id"] == "tx-boom":
                raise ArithmeticError("boom")
            return None

    records = [_good_record(transaction_id="tx-ok"), _good_record(transaction_id="tx-boom")]
    report = await Exploding(ch_client=None).validate_batch(records)

    assert [r["transaction_id"] for r in report.valid] == ["tx-ok"]
    [error] = report.errors
    assert error.rule == "R3_mcc_iso18245"
    assert error.reason == "rule raised ArithmeticError: boom"
    assert "R3_mcc_iso18245 raised" in caplog.text


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
