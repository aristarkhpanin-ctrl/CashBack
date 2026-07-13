"""Unit tests for the synchronous holdout split (beyond-plan)."""
from __future__ import annotations

import pytest
from app.model_registry import LoadedModel, ModelWatcher, in_holdout


def _model(version: str) -> LoadedModel:
    return LoadedModel(model=object(), explainer=object(),
                       version=version, feature_columns=[])


def _watcher(*, enabled=True, ratio=0.05) -> ModelWatcher:
    w = ModelWatcher("uri", "cashback_lgbm_ranker",
                     holdout_enabled=enabled, holdout_ratio=ratio,
                     holdout_salt="test-salt")
    w._loaded = _model("5")        # Production
    w._holdout_model = _model("4")  # предшественник
    return w


# ── in_holdout ───────────────────────────────────────────────────────────────
def test_in_holdout_deterministic():
    assert in_holdout("u-1", ratio=0.5, salt="s") == in_holdout("u-1", ratio=0.5, salt="s")


def test_in_holdout_ratio_bounds():
    assert not in_holdout("anyone", ratio=0.0, salt="s")
    assert in_holdout("anyone", ratio=1.0, salt="s")


def test_in_holdout_salt_changes_assignment():
    users = [f"u-{i}" for i in range(2000)]
    a = {u for u in users if in_holdout(u, ratio=0.2, salt="salt-a")}
    b = {u for u in users if in_holdout(u, ratio=0.2, salt="salt-b")}
    assert a != b  # разная соль → разбиение сдвигается


def test_in_holdout_fraction_close_to_ratio():
    users = [f"u-{i}" for i in range(20000)]
    frac = sum(in_holdout(u, ratio=0.05, salt="s") for u in users) / len(users)
    assert 0.04 <= frac <= 0.06


# ── get_for_user ─────────────────────────────────────────────────────────────
async def _first_holdout_user(w: ModelWatcher) -> str:
    for i in range(100000):
        uid = f"u-{i}"
        if in_holdout(uid, ratio=0.05, salt="test-salt"):
            return uid
    raise AssertionError("no holdout user found")


async def _first_prod_user(w: ModelWatcher) -> str:
    for i in range(100000):
        uid = f"u-{i}"
        if not in_holdout(uid, ratio=0.05, salt="test-salt"):
            return uid
    raise AssertionError("no prod user found")


@pytest.mark.asyncio
async def test_holdout_user_gets_predecessor_model():
    w = _watcher()
    uid = await _first_holdout_user(w)
    model, group = await w.get_for_user(uid)
    assert group == "holdout"
    assert model.version == "4"


@pytest.mark.asyncio
async def test_non_holdout_user_gets_production():
    w = _watcher()
    uid = await _first_prod_user(w)
    model, group = await w.get_for_user(uid)
    assert group == "prod"
    assert model.version == "5"


@pytest.mark.asyncio
async def test_no_predecessor_serves_production_to_everyone():
    w = _watcher()
    w._holdout_model = None  # предшественник ещё не появился
    uid = await _first_holdout_user(w)
    model, group = await w.get_for_user(uid)
    assert group == "prod"
    assert model.version == "5"


@pytest.mark.asyncio
async def test_holdout_disabled_serves_production():
    w = _watcher(enabled=False)
    uid = await _first_holdout_user(w)
    model, group = await w.get_for_user(uid)
    assert group == "prod"
    assert model.version == "5"
