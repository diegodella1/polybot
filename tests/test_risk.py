"""Tests for risk manager."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import asyncio
import time
from unittest.mock import AsyncMock, patch


# Mock state and db before importing risk
@pytest.fixture(autouse=True)
def mock_state():
    with patch("bot.risk.state") as mock_st:
        mock_st.get = AsyncMock(return_value=None)
        mock_st.set = AsyncMock()
        yield mock_st


@pytest.fixture(autouse=True)
def mock_db():
    with patch("bot.risk.get_db") as mock:
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[])
        db.execute = AsyncMock(return_value=cursor)
        db.close = AsyncMock()
        mock.return_value = db
        yield mock


class TestRiskCheck:
    @pytest.mark.asyncio
    async def test_bot_disabled(self, mock_state):
        from bot.risk import check_risk
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": False,
        }.get(k, d))

        result = await check_risk(0.3, 100.0)
        assert not result
        assert "stopped" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_below_threshold(self, mock_state):
        from bot.risk import check_risk
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": False,
        }.get(k, d))

        result = await check_risk(0.05, 100.0)
        assert not result
        assert "threshold" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_bankroll_floor(self, mock_state):
        from bot.risk import check_risk
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": False,
        }.get(k, d))

        result = await check_risk(0.5, 3.0)  # below $5 floor
        assert not result
        assert "floor" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_daily_loss_no_longer_blocks(self, mock_state):
        from bot.risk import check_risk
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": False,
        }.get(k, d))

        # Large daily loss should NOT block — sizing handles drawdown now
        result = await check_risk(0.5, 100.0)
        assert result

    @pytest.mark.asyncio
    async def test_open_position_blocked(self, mock_state):
        from bot.risk import check_risk
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": True,
        }.get(k, d))

        result = await check_risk(0.5, 100.0)
        assert not result
        assert "position" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_trade_cooldown_blocks(self, mock_state):
        from bot.risk import check_risk
        # Last trade was 30 seconds ago, cooldown is 600s
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": False,
            "last_trade_timestamp": time.time() - 30,  # 30s ago
        }.get(k, d))

        result = await check_risk(0.5, 100.0)
        assert not result
        assert "trade cooldown" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_trade_cooldown_expired_allows(self, mock_state):
        from bot.risk import check_risk
        # Last trade was 700 seconds ago, cooldown is 600s → should pass
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": False,
            "last_trade_timestamp": time.time() - 700,
        }.get(k, d))

        result = await check_risk(0.5, 100.0)
        assert result

    @pytest.mark.asyncio
    async def test_allowed_trade(self, mock_state):
        from bot.risk import check_risk
        mock_state.get = AsyncMock(side_effect=lambda k, d=None: {
            "enabled": True,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "current_exposure": 0.0,
            "has_open_position": False,
        }.get(k, d))

        result = await check_risk(0.3, 100.0)
        assert result
