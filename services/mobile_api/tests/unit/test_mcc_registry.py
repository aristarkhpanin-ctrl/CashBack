"""Unit tests for the MCC registry."""
from __future__ import annotations

import pytest
from app.mcc_registry import DEFAULT_INFO, MCC_CATALOG, icon_url, lookup


def test_known_mcc_returns_catalog_entry():
    info = lookup("5411")
    assert info.name == "Продукты"
    assert info.icon == "groceries"


def test_unknown_mcc_returns_default():
    assert lookup("9999") is DEFAULT_INFO
    assert lookup(None) is DEFAULT_INFO  # type: ignore[arg-type]


def test_terms_summary_under_140_chars():
    for code, info in MCC_CATALOG.items():
        assert len(info.terms_summary) <= 140, (
            f"MCC {code} terms_summary too long: {len(info.terms_summary)}"
        )


def test_icon_url_uses_cdn_base():
    url = icon_url("https://cdn.example.com/icons", "5411")
    assert url == "https://cdn.example.com/icons/groceries.png"
    # Trailing slash on CDN base is normalised.
    assert icon_url("https://cdn.example.com/icons/", "5812") == \
        "https://cdn.example.com/icons/restaurant.png"


def test_icon_url_unknown_mcc_uses_default_slug():
    assert icon_url("https://cdn", "9999").endswith("/default.png")
