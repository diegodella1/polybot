"""Tests for signal-proportional position sizing."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from bot.sizing import kelly_size, _drawdown_multiplier


# Fix config values so tests don't depend on config.yaml
@pytest.fixture(autouse=True)
def mock_sizing_config():
    def fake_get(key, default=None):
        overrides = {
            "daily_loss_limit_pct": 0.15,
            "min_drawdown_multiplier": 0.25,
            "base_trade_pct": 0.05,
            "max_trade_pct": 0.08,
            "min_trade_usd": 1,
            "max_exposure_usd": 5,
            "kelly_fraction": 0.5,
            "min_estimated_winrate": 0.55,
            "max_estimated_winrate": 0.6,
        }
        return overrides.get(key, default)
    with patch("bot.sizing.get", side_effect=fake_get):
        yield


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
        # Should trade within configured price range [0.35, 0.55]
        for price in [0.38, 0.45, 0.50, 0.55]:
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
        assert "floor" in result["reason"].lower()

    def test_extreme_price_rejected(self):
        result = kelly_size(0.5, 0.005, 100.0)
        assert result["size_usd"] == 0
        result2 = kelly_size(0.5, 0.995, 100.0)
        assert result2["size_usd"] == 0

    def test_min_trade_floor(self):
        # FOK min is $1 USD — sizing should floor to it
        result = kelly_size(0.10, 0.50, 15.0)
        assert result["size_usd"] >= 1.0


class TestDrawdownMultiplier:
    def test_no_loss_returns_one(self):
        assert _drawdown_multiplier(0.0, 100.0) == 1.0

    def test_profit_returns_one(self):
        assert _drawdown_multiplier(5.0, 100.0) == 1.0

    def test_at_limit_returns_min(self):
        # daily_loss_limit_pct=0.15, min_drawdown_multiplier=0.25
        # $15 loss on $100 bankroll = 15% = exactly at limit
        mult = _drawdown_multiplier(-15.0, 100.0)
        assert abs(mult - 0.25) < 0.01

    def test_halfway_loss(self):
        # 7.5% loss = halfway to 15% limit → multiplier ~0.625
        mult = _drawdown_multiplier(-7.5, 100.0)
        assert 0.60 < mult < 0.66

    def test_beyond_limit_floors_at_min(self):
        # Loss exceeds limit — clamp at min multiplier
        mult = _drawdown_multiplier(-30.0, 100.0)
        assert abs(mult - 0.25) < 0.01

    def test_sizing_uses_multiplier(self):
        # No loss → normal size
        normal = kelly_size(0.3, 0.50, 100.0, daily_pnl=0.0)
        # At loss limit → reduced size
        reduced = kelly_size(0.3, 0.50, 100.0, daily_pnl=-15.0)
        assert reduced["size_usd"] < normal["size_usd"]
        assert reduced["drawdown_multiplier"] < normal["drawdown_multiplier"]

    def test_sizing_returns_multiplier_field(self):
        result = kelly_size(0.3, 0.50, 100.0, daily_pnl=-7.5)
        assert "drawdown_multiplier" in result
        assert 0.0 < result["drawdown_multiplier"] < 1.0
