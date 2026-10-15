"""DataValidator — chapter 3.1, listing 3.4.

Implements the six validation rules from table 16:

    R1  NOT-NULL on required fields; user_id is a UUID
    R2  amount is a plain decimal (≤13 integer, ≤2 fraction digits) in (0, AMOUNT_MAX]
    R3  mcc_code is 4 digits and present in MCC_REGISTRY (ISO 18245)
    R4  transaction_date is ISO-8601, not in the future, ≤365 days old
    R5  no duplicate transaction_id within the last 24 hours (ClickHouse)
    R6  currency is a 3-letter ISO 4217 code

Each rule returns either ``None`` (record passes) or an error string.
:meth:`validate_batch` is async — it pre-fetches recent transaction ids
from ClickHouse via :meth:`_fetch_recent_ids` and then evaluates the
rules in-memory.

A record that passes must also load: the rules accept only what
``transactions_raw`` can store (``user_id UUID``, ``amount Decimal(15, 2)``,
``mcc_code FixedString(4)``), because a row ClickHouse rejects would fail the
load. A rule never raises: an unexpected error rejects the record instead
of stopping the ETL. :func:`cleanse` turns valid records into the shape the
loader inserts (``transaction_date`` as an aware UTC datetime).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable, Optional

from app.mcc_registry import MCC_REGISTRY

log = logging.getLogger(__name__)


# ISO 4217 — common currencies. Validation only checks shape + membership.
ISO_4217: frozenset[str] = frozenset(
    {
        "RUB", "USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD",
        "NZD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "BYN",
        "KZT", "UZS", "AMD", "AZN", "GEL", "UAH", "MDL", "AED", "SGD",
        "HKD", "KRW", "INR", "BRL", "MXN", "ZAR", "ILS",
    }
)

# transactions_raw.user_id is UUID: canonical 8-4-4-4-12 hex form only
# (ClickHouse rejects e.g. the "{...}" / "urn:uuid:" forms uuid.UUID accepts).
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
# transactions_raw.amount is Decimal(15, 2): ASCII digits, optional 1-2 decimals.
# No sign, exponent, separators, whitespace, NaN / Infinity.
_AMOUNT_RE = re.compile(r"[0-9]{1,13}(?:\.[0-9]{1,2})?")

REQUIRED_FIELDS: tuple[str, ...] = (
    "transaction_id",
    "user_id",
    "mcc_code",
    "amount",
    "currency",
    "transaction_date",
    "channel",
)


def parse_transaction_date(raw: Any) -> Optional[datetime]:
    """ISO-8601 string (or datetime) -> aware UTC datetime; ``None`` if unparseable.

    Naive values count as UTC. Shared by R4 and :func:`cleanse`, so a record
    that passed R4 always gets a timestamp.
    """
    if isinstance(raw, datetime):
        ts = raw
    elif isinstance(raw, str):
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    try:
        return ts.astimezone(UTC)
    except OverflowError:
        return None


def cleanse(record: dict) -> dict:
    """A valid record as it is loaded: ``transaction_date`` -> aware UTC datetime."""
    out = dict(record)
    out["transaction_date"] = parse_transaction_date(record.get("transaction_date"))
    return out


@dataclass
class ValidationError:
    record: dict
    rule: str
    reason: str


@dataclass
class ValidationReport:
    valid: list[dict] = field(default_factory=list)
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.valid) + len(self.errors)

    def error_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return len(self.errors) / self.total


class DataValidator:
    """Pure-Python validator for transaction records.

    Parameters
    ----------
    ch_client:
        ``clickhouse_connect.driver.Client`` (or anything with a
        ``query`` method returning ``.result_rows``). May be ``None`` in
        unit tests — :meth:`_fetch_recent_ids` then yields an empty set.
    recent_window_hours:
        Look-back horizon for the deduplication query.
    table:
        ClickHouse table queried for recent ids.
    currency_set:
        Override the default ISO 4217 set (used in tests).
    """

    AMOUNT_MAX: int = 10_000_000
    AMOUNT_MIN: Decimal = Decimal("0.00")
    MAX_AGE_DAYS: int = 365

    def __init__(
        self,
        ch_client: Any | None = None,
        *,
        recent_window_hours: int = 24,
        table: str = "transactions_raw",
        currency_set: Iterable[str] | None = None,
    ) -> None:
        self._ch = ch_client
        self._recent_window_hours = recent_window_hours
        self._table = table
        self._recent_ids: set[str] = set()
        self._currency_set = frozenset(currency_set) if currency_set else ISO_4217

    # ------------------------------------------------------------------ R5
    async def _fetch_recent_ids(self) -> set[str]:
        """Pull transaction_ids from the last 24h into ``self._recent_ids``."""
        if self._ch is None:
            self._recent_ids = set()
            return self._recent_ids

        sql = (
            f"SELECT transaction_id FROM {self._table} "
            f"WHERE inserted_at >= now() - INTERVAL {self._recent_window_hours} HOUR"
        )

        def _run() -> set[str]:
            result = self._ch.query(sql)
            return {row[0] for row in result.result_rows}

        try:
            self._recent_ids = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not fetch recent ids: %s — assuming empty", exc)
            self._recent_ids = set()
        return self._recent_ids

    # ------------------------------------------------------------------ R1
    @staticmethod
    def _check_not_null(rec: dict) -> Optional[str]:
        for field_ in REQUIRED_FIELDS:
            if rec.get(field_) in (None, ""):
                return f"required field '{field_}' is null/empty"
        user_id = rec["user_id"]
        if not isinstance(user_id, str) or not _UUID_RE.fullmatch(user_id):
            return f"user_id '{user_id}' is not a UUID"
        return None

    # ------------------------------------------------------------------ R2
    @classmethod
    def _check_amount(cls, rec: dict) -> Optional[str]:
        raw = rec.get("amount")
        text = raw if isinstance(raw, str) else str(raw)
        if not _AMOUNT_RE.fullmatch(text):
            return f"amount '{raw}' is not a decimal with at most 2 fraction digits"
        value = Decimal(text)
        if value <= cls.AMOUNT_MIN:
            return f"amount {value} must be > 0"
        if value > Decimal(cls.AMOUNT_MAX):
            return f"amount {value} exceeds AMOUNT_MAX={cls.AMOUNT_MAX}"
        return None

    # ------------------------------------------------------------------ R3
    @staticmethod
    def _check_mcc(rec: dict) -> Optional[str]:
        mcc = rec.get("mcc_code")
        if not isinstance(mcc, str) or len(mcc) != 4 or not mcc.isdigit():
            return f"mcc_code '{mcc}' is not 4-digit ISO 18245"
        if mcc not in MCC_REGISTRY:
            return f"mcc_code '{mcc}' not in ISO 18245 registry"
        return None

    # ------------------------------------------------------------------ R4
    @classmethod
    def _check_timestamp(cls, rec: dict) -> Optional[str]:
        raw = rec.get("transaction_date")
        if not raw:
            return "transaction_date missing"
        ts = parse_transaction_date(raw)
        if ts is None:
            return f"transaction_date '{raw}' is not ISO-8601"
        now = datetime.now(UTC)
        if ts > now + timedelta(minutes=5):
            return f"transaction_date {ts.isoformat()} is in the future"
        if ts < now - timedelta(days=cls.MAX_AGE_DAYS):
            return (
                f"transaction_date {ts.isoformat()} older than "
                f"{cls.MAX_AGE_DAYS} days"
            )
        return None

    # ------------------------------------------------------------------ R5
    def _check_deduplication(self, rec: dict) -> Optional[str]:
        tid = rec.get("transaction_id")
        if not tid:
            return None  # caught by R1
        if tid in self._recent_ids:
            return f"duplicate transaction_id '{tid}' seen in last 24h"
        # Track within this batch as well.
        self._recent_ids.add(tid)
        return None

    # ------------------------------------------------------------------ R6
    def _check_currency(self, rec: dict) -> Optional[str]:
        cur = rec.get("currency")
        if not isinstance(cur, str) or len(cur) != 3 or not cur.isalpha():
            return f"currency '{cur}' is not a 3-letter ISO 4217 code"
        if cur.upper() not in self._currency_set:
            return f"currency '{cur}' not in ISO 4217 set"
        return None

    # ------------------------------------------------------------------ orchestration
    @property
    def rules(self) -> list[tuple[str, Callable[[dict], Optional[str]]]]:
        return [
            ("R1_not_null", self._check_not_null),
            ("R2_amount_range", self._check_amount),
            ("R3_mcc_iso18245", self._check_mcc),
            ("R4_timestamp", self._check_timestamp),
            ("R5_deduplication", self._check_deduplication),
            ("R6_iso4217_currency", self._check_currency),
        ]

    async def validate_batch(self, records: Iterable[dict]) -> ValidationReport:
        await self._fetch_recent_ids()
        report = ValidationReport()
        for rec in records:
            error: Optional[ValidationError] = None
            for rule_name, rule in self.rules:
                try:
                    reason = rule(rec)
                except Exception as exc:  # noqa: BLE001 — a rule must not stop the ETL
                    reason = f"rule raised {type(exc).__name__}: {exc}"
                    log.error("validation rule %s raised on %r: %s", rule_name, rec, exc)
                if reason is not None:
                    error = ValidationError(rec, rule_name, reason)
                    break
            if error is None:
                report.valid.append(rec)
            else:
                report.errors.append(error)
        return report
