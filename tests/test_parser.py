"""
Unit tests for the scraper_daemon price parser.

Run with:
    pytest tests/test_parser.py -v

These tests guard against regressions in the _parse_price function,
especially for the "no decimal dot" heuristic and range validation.

Note: redis and playwright are mocked at the module level so these
tests can run without those heavyweight packages installed.
"""

import re
import sys
import os
from unittest.mock import MagicMock

# Inject lightweight mocks before importing scraper_daemon
# so that `import redis.asyncio` and `import playwright` don't fail.
sys.modules.setdefault("redis", MagicMock())
sys.modules.setdefault("redis.asyncio", MagicMock())
sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.async_api", MagicMock())

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from scraper_daemon import _parse_price


# ── Fixtures ───────────────────────────────────────────────────────────

GOLD_TARGET = {
    "name": "Gold (XAUUSD)",
    "type": "metal",
    "min_value": 500.0,
    "max_value": 5_000.0,
}

SILVER_TARGET = {
    "name": "Silver (XAGUSD)",
    "type": "metal",
    "min_value": 5.0,
    "max_value": 500.0,
}

COPPER_TARGET = {
    "name": "Copper (XCUUSD)",
    "type": "metal",
    "min_value": 0.5,
    "max_value": 50.0,
}

USDIDR_TARGET = {
    "name": "USD/IDR",
    "type": "currency",
    "min_value": 10_000.0,
    "max_value": 25_000.0,
}


# ── Gold tests ─────────────────────────────────────────────────────────

class TestGoldParsing:
    def test_standard_format_with_commas(self):
        """Gold price with comma thousands separator."""
        assert _parse_price("3,247.80", GOLD_TARGET) == pytest.approx(3247.80)

    def test_no_decimal_dot(self):
        """TradingView sometimes omits decimal dot for metals."""
        # "324780" → 3247.80
        assert _parse_price("324780", GOLD_TARGET) == pytest.approx(3247.80)

    def test_with_commas_and_no_decimal(self):
        """Comma-separated, no decimal."""
        # "3,24780" → remove comma → "324780" → 3247.80
        assert _parse_price("3,24780", GOLD_TARGET) == pytest.approx(3247.80)

    def test_below_minimum_returns_none(self):
        """Price below 500 is invalid for gold."""
        assert _parse_price("499.00", GOLD_TARGET) is None

    def test_above_maximum_returns_none(self):
        """Price above 5000 is invalid for gold."""
        assert _parse_price("5001.00", GOLD_TARGET) is None

    def test_empty_string_returns_none(self):
        assert _parse_price("", GOLD_TARGET) is None

    def test_non_numeric_returns_none(self):
        assert _parse_price("N/A", GOLD_TARGET) is None
        assert _parse_price("--", GOLD_TARGET) is None

    def test_whitespace_stripped(self):
        assert _parse_price("  3,247.80  ", GOLD_TARGET) == pytest.approx(3247.80)


# ── Silver tests ───────────────────────────────────────────────────────

class TestSilverParsing:
    def test_standard_format(self):
        assert _parse_price("32.15", SILVER_TARGET) == pytest.approx(32.15)

    def test_no_decimal_dot_silver(self):
        # "3215" → 32.15
        assert _parse_price("3215", SILVER_TARGET) == pytest.approx(32.15)

    def test_below_min_returns_none(self):
        assert _parse_price("4.99", SILVER_TARGET) is None

    def test_above_max_returns_none(self):
        assert _parse_price("501.00", SILVER_TARGET) is None


# ── Copper tests ───────────────────────────────────────────────────────

class TestCopperParsing:
    def test_standard_format(self):
        assert _parse_price("4.68", COPPER_TARGET) == pytest.approx(4.68)

    def test_no_decimal_dot_copper(self):
        # "468" → 4.68
        assert _parse_price("468", COPPER_TARGET) == pytest.approx(4.68)

    def test_below_min_returns_none(self):
        assert _parse_price("0.49", COPPER_TARGET) is None

    def test_above_max_returns_none(self):
        assert _parse_price("51.00", COPPER_TARGET) is None


# ── USDIDR tests ───────────────────────────────────────────────────────

class TestUSDIDRParsing:
    def test_standard_format(self):
        """USDIDR has no missing decimal dot heuristic."""
        assert _parse_price("16,325.00", USDIDR_TARGET) == pytest.approx(16325.00)

    def test_without_decimal(self):
        # Type=currency → no decimal insertion heuristic
        assert _parse_price("16325", USDIDR_TARGET) == pytest.approx(16325.0)

    def test_below_min_returns_none(self):
        assert _parse_price("9999", USDIDR_TARGET) is None

    def test_above_max_returns_none(self):
        assert _parse_price("25001", USDIDR_TARGET) is None

    def test_comma_separators(self):
        assert _parse_price("16,325", USDIDR_TARGET) == pytest.approx(16325.0)


# ── Edge cases ─────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_none_input_returns_none(self):
        # _parse_price expects str, but test defensive behavior
        assert _parse_price(None, GOLD_TARGET) is None  # type: ignore[arg-type]

    def test_zero_returns_none(self):
        assert _parse_price("0", GOLD_TARGET) is None

    def test_negative_returns_none(self):
        assert _parse_price("-100", GOLD_TARGET) is None

    def test_scientific_notation_returns_none(self):
        # Our regex does not match scientific notation
        assert _parse_price("3.24e3", GOLD_TARGET) is None
