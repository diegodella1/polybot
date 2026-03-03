"""Tests for signal-proportional position sizing."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from bot.sizing import kelly_size


class TestKellySize:
    def test_basic_sizing(self):
        result = kelly_size(0.3, 0.50, 100.0)
        assert result["size_usd"] > 0
        assert result["size_usd"] <= 100 * 0.10  # safety cap

    def test_strong_signal_bigger_size(self):
        weak = kelly_size(0.10, 0.50, 100.0)
        strong = kelly_size(0.40, 0.50, 100.0)
        assert strong["size_usd"] > weak["size_usd"]

    def test_always_trades_above_threshold(self):
        # With signal above threshold, should always produce a trade
        result = kelly_size(0.15, 0.50, 50.0)
        assert result["size_usd"] > 0

    def test_trades_at_any_entry_price(self):
        # Unlike Kelly, should trade at 55c, 60c, etc.
        for price in [0.40, 0.50, 0.55, 0.60, 0.70]:
            result = kelly_size(0.25, price, 50.0)
            assert result["size_usd"] > 0, f"Should trade at entry_price={price}"

    def test_invalid_price_zero(self):
        result = kelly_size(0.3, 0.0, 100.0)
        assert result["size_usd"] == 0

    def test_invalid_price_one(self):
        result = kelly_size(0.3, 1.0, 100.0)
        assert result["size_usd"] == 0

    def test_shares_calculation(self):
        result = kelly_size(0.3, 0.50, 100.0)
        assert result["shares"] > 0
        expected_shares = result["size_usd"] / 0.50
        assert abs(result["shares"] - round(expected_shares, 4)) < 0.01

    def test_never_exceeds_10_pct_bankroll(self):
        result = kelly_size(1.0, 0.10, 20.0)
        assert result["size_usd"] <= 20.0 * 0.10 + 0.01

    def test_tiny_bankroll_rejected(self):
        result = kelly_size(0.5, 0.50, 3.0)
        assert result["size_usd"] == 0
        assert "too low" in result["reason"].lower()

    def test_extreme_price_rejected(self):
        result = kelly_size(0.5, 0.005, 100.0)
        assert result["size_usd"] == 0
        result2 = kelly_size(0.5, 0.995, 100.0)
        assert result2["size_usd"] == 0

    def test_min_trade_floor(self):
        # FOK min is $1 USD — sizing should floor to it
        result = kelly_size(0.10, 0.50, 15.0)
        assert result["size_usd"] >= 1.0
