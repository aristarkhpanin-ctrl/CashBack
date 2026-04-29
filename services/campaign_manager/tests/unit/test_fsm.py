"""Unit tests for the campaign FSM (chapter 3.2)."""
from __future__ import annotations

import pytest

from app.fsm import (
    ACTIONS, STATUSES, TRANSITIONS, InvalidTransition, allowed_actions,
    can_transition, transition,
)


# ---------------------------------------------------------------------------
# Allowed transitions — exact mapping spelled out in the task spec.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current, action, expected",
    [
        ("DRAFT",  "activate", "ACTIVE"),
        ("ACTIVE", "pause",    "PAUSED"),
        ("ACTIVE", "complete", "COMPLETED"),
        ("PAUSED", "activate", "ACTIVE"),
        ("PAUSED", "complete", "COMPLETED"),
    ],
)
def test_allowed_transitions(current, action, expected):
    assert transition(current, action) == expected
    assert can_transition(current, action) is True


# ---------------------------------------------------------------------------
# Forbidden transitions — every other state/action combination raises.
# ---------------------------------------------------------------------------
def _all_forbidden_pairs():
    forbidden: list[tuple[str, str]] = []
    for state in STATUSES:
        for action in ACTIONS:
            if (state, action) not in TRANSITIONS:
                forbidden.append((state, action))
    return forbidden


@pytest.mark.parametrize("current, action", _all_forbidden_pairs())
def test_forbidden_transitions_raise(current, action):
    with pytest.raises(InvalidTransition):
        transition(current, action)
    assert can_transition(current, action) is False


def test_forbidden_self_loops():
    """COMPLETED is a sink; nothing transitions out of it."""
    for action in ACTIONS:
        with pytest.raises(InvalidTransition):
            transition("COMPLETED", action)


def test_unknown_state_raises():
    with pytest.raises(InvalidTransition):
        transition("NUKED", "activate")


def test_unknown_action_raises():
    with pytest.raises(InvalidTransition):
        transition("DRAFT", "explode")


# ---------------------------------------------------------------------------
# allowed_actions helper used by the API to advertise valid moves.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current, expected_set",
    [
        ("DRAFT",     {"activate"}),
        ("ACTIVE",    {"pause", "complete"}),
        ("PAUSED",    {"activate", "complete"}),
        ("COMPLETED", set()),
    ],
)
def test_allowed_actions(current, expected_set):
    assert set(allowed_actions(current)) == expected_set


# ---------------------------------------------------------------------------
# Specifics for the 409-Conflict cases the API surfaces.
# ---------------------------------------------------------------------------
def test_cannot_pause_a_draft_campaign():
    with pytest.raises(InvalidTransition) as exc_info:
        transition("DRAFT", "pause")
    assert "DRAFT" in str(exc_info.value)


def test_cannot_complete_a_draft_campaign():
    with pytest.raises(InvalidTransition):
        transition("DRAFT", "complete")


def test_cannot_activate_completed_campaign():
    with pytest.raises(InvalidTransition):
        transition("COMPLETED", "activate")


def test_invalid_transition_carries_metadata():
    try:
        transition("DRAFT", "complete")
    except InvalidTransition as exc:
        assert exc.current == "DRAFT"
        assert exc.action == "complete"
    else:
        pytest.fail("InvalidTransition was not raised")
