"""Shared data classes for the Business Rules Engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RuleOutcome(str, Enum):
    """Possible verdicts for a single rule."""

    PASS = "PASS"
    SKIP = "SKIP"        # rule does not apply / cannot decide
    REJECT = "REJECT"    # candidate must be filtered out


@dataclass
class RuleContext:
    """Bag of dependencies + per-candidate state passed to each rule.

    A *single* RuleContext is constructed per ``(user_id, campaign_id)``
    candidate. Mutating ``state`` is allowed; rules can stash precomputed
    values for downstream rules.
    """

    user_id: str
    candidate_mcc: str
    campaign_id: str | None = None
    user_segment_id: int | None = None
    transaction_amount: float | None = None
    channel: str | None = None
    excluded_mccs: frozenset[str] = field(default_factory=frozenset)
    state: dict[str, Any] = field(default_factory=dict)

    # Injected dependencies (None in unit tests where the rule is short-circuited)
    redis: Any | None = None
    db: Any | None = None
    settings: Any | None = None
    campaign: dict | None = None  # cached campaign row


@dataclass
class RuleResult:
    rule: str
    outcome: RuleOutcome
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome is RuleOutcome.PASS

    @classmethod
    def passed_(cls, rule: str, **meta: Any) -> "RuleResult":
        return cls(rule=rule, outcome=RuleOutcome.PASS, metadata=meta)

    @classmethod
    def reject(cls, rule: str, reason: str, **meta: Any) -> "RuleResult":
        return cls(rule=rule, outcome=RuleOutcome.REJECT, reason=reason, metadata=meta)

    @classmethod
    def skip(cls, rule: str, reason: str = "n/a") -> "RuleResult":
        return cls(rule=rule, outcome=RuleOutcome.SKIP, reason=reason)
