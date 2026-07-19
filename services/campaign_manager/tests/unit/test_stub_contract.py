"""Контракт E2E-стаба против реальных Pydantic-схем сервиса (фаза 20).

frontend/e2e/stub_backend.py имитирует wire-формат campaign_manager для
браузерных тестов. Классическая гниль моков: бэкенд меняет схему, стаб
продолжает отдавать старую — E2E зеленеет на несуществующем API. Этот
тест валидирует каждый payload стаба соответствующей response-моделью:
переименование поля в schemas.py уронит CI, пока стаб не обновят.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from app.schemas import (
    ABExperimentResponse,
    ABResults,
    AdminUserResponse,
    CampaignResponse,
    CampaignStats,
    ChannelStats,
    DailyTrendResponse,
    FunnelResponse,
    KpiResponse,
    MccCategoryRef,
    MlCustomer,
    MlLimitsResponse,
    SegmentMatrixCell,
    SegmentRef,
)

_STUB_PATH = (
    Path(__file__).resolve().parents[3].parent
    / "frontend" / "e2e" / "stub_backend.py"
)


@pytest.fixture(scope="module")
def stub():
    if not _STUB_PATH.exists():
        pytest.skip(f"stub not found at {_STUB_PATH}")
    spec = importlib.util.spec_from_file_location("e2e_stub_backend", _STUB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # серверы стартуют только под __main__
    return module


def test_campaigns_match_campaign_response(stub):
    for payload in stub.CAMPAIGNS:
        CampaignResponse.model_validate(payload)


def test_stats_match_campaign_stats(stub):
    for payload in stub.STATS.values():
        CampaignStats.model_validate(payload)


def test_funnel_matches_funnel_response(stub):
    FunnelResponse.model_validate({
        "campaign_id": None,
        "period_days": 30,
        "steps": stub.FUNNEL_STEPS,
    })


def test_matrix_matches_segment_matrix_cell(stub):
    for payload in stub.MATRIX:
        SegmentMatrixCell.model_validate(payload)


def test_trend_matches_daily_trend_response(stub):
    DailyTrendResponse.model_validate({
        "campaign_id": None,
        "period_days": 30,
        "points": stub.TREND_POINTS,
    })


def test_channels_match_channel_stats(stub):
    assert len(stub.CHANNELS) == 4
    for payload in stub.CHANNELS:
        ChannelStats.model_validate(payload)


def test_experiments_match_ab_schemas(stub):
    for exp in stub.EXPERIMENTS:
        ABExperimentResponse.model_validate(exp)
        ABResults.model_validate(stub.experiment_results(exp))


def test_admin_users_match_admin_user_response(stub):
    for user in stub.ADMIN_USERS:
        AdminUserResponse.model_validate(stub.public_user(user))


def test_ml_limits_match_ml_limits_response(stub):
    MlLimitsResponse.model_validate(stub.ML_LIMITS)


def test_reference_segments_match_segment_ref(stub):
    assert len(stub.SEGMENTS_REF) == 5
    for payload in stub.SEGMENTS_REF:
        SegmentRef.model_validate(payload)


def test_reference_mcc_match_mcc_category_ref(stub):
    assert len(stub.MCC_REF) == 12
    for payload in stub.MCC_REF:
        MccCategoryRef.model_validate(payload)


def test_kpis_match_kpi_response(stub):
    KpiResponse.model_validate(stub.KPIS)


def test_ml_customers_match_ml_customer(stub):
    assert len(stub.ML_CUSTOMERS) >= 1
    for payload in stub.ML_CUSTOMERS:
        MlCustomer.model_validate(payload)
