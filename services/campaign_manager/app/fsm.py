"""Finite-state machine for campaign status transitions.

Allowed transitions (chapter 3.2)::

    DRAFT   --activate--> ACTIVE
    ACTIVE  --pause-----> PAUSED
    ACTIVE  --complete--> COMPLETED
    PAUSED  --activate--> ACTIVE
    PAUSED  --complete--> COMPLETED

Any other ``(state, action)`` combination raises
:class:`InvalidTransition` and the API surfaces a ``409 Conflict``.
"""
from __future__ import annotations

from typing import Final

CampaignStatus = str  # DRAFT / ACTIVE / PAUSED / COMPLETED


STATUSES: Final[tuple[str, ...]] = ("DRAFT", "ACTIVE", "PAUSED", "COMPLETED")
ACTIONS: Final[tuple[str, ...]] = ("activate", "pause", "complete")


# (current_status, action) → new_status
TRANSITIONS: dict[tuple[str, str], str] = {
    ("DRAFT", "activate"):  "ACTIVE",
    ("ACTIVE", "pause"):    "PAUSED",
    ("ACTIVE", "complete"): "COMPLETED",
    ("PAUSED", "activate"): "ACTIVE",
    ("PAUSED", "complete"): "COMPLETED",
}


class InvalidTransition(ValueError):
    """Raised when ``(current, action)`` is not allowed by the FSM."""

    def __init__(self, current: str, action: str) -> None:
        self.current = current
        self.action = action
        super().__init__(
            f"cannot {action} a campaign in state {current!r}"
        )


def transition(current: str, action: str) -> str:
    """Return the next status for ``(current, action)`` or raise."""
    if current not in STATUSES:
        raise InvalidTransition(current, action)
    if action not in ACTIONS:
        raise InvalidTransition(current, action)
    key = (current, action)
    if key not in TRANSITIONS:
        raise InvalidTransition(current, action)
    return TRANSITIONS[key]


def can_transition(current: str, action: str) -> bool:
    return (current, action) in TRANSITIONS


def allowed_actions(current: str) -> list[str]:
    """Return the list of actions valid for the current state."""
    return [act for (st, act) in TRANSITIONS if st == current]
