"""Shared pytest configuration for the ETL test-suite."""
from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: needs real ClickHouse / Kafka / Schema Registry "
        "(see tests/integration/test_real_pipeline.py for the ETL_IT_* variables)",
    )
