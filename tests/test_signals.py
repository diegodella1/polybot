"""Tests for signal generation."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from data.buffer import PriceBuffer, Candle
from bot.signals import _clamp, _normalize, momentum_signal


class FakeKline:
    def __init__(self, o, h, l, c, v, closed=True):
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.is_closed = closed
        self.open_time = 0


def make_buffer(prices: list[float], vol=100.0) -> PriceBuffer:
    """Create a buffer with given close prices."""
    buf = PriceBuffer(max_size=100)
    for i, p in enumerate(prices):
        kline = FakeKline(p, p + 0.5, p - 0.5, p, vol, closed=True)
        kline.open_time = i * 60000
        buf.update(kline)
    return buf


class TestClamp:
    def test_within_range(self):
        assert _clamp(0.5) == 0.5

    def test_above_max(self):
        assert _clamp(1.5) == 1.0

    def test_below_min(self):
        assert _clamp(-1.5) == -1.0


class TestNormalize:
    def test_zero(self):
        assert _normalize(0) == 0.0

    def test_positive(self):
        result = _normalize(1.0, scale=1.0)
        assert 0 < result < 1.0

    def test_negative(self):
        result = _normalize(-1.0, scale=1.0)
        assert -1.0 < result < 0


class TestPriceBuffer:
    def test_basic_operations(self):
        buf = make_buffer([100, 101, 102, 103, 104])
        assert buf.size == 5
        assert buf.current_price == 104

    def test_ret(self):
        buf = make_buffer([100, 101, 102, 103, 104])
        ret = buf.ret(1)
        assert ret is not None
        assert abs(ret - (104 - 103) / 103) < 1e-6

    def test_ema(self):
        prices = list(range(100, 130))
        buf = make_buffer(prices)
        ema5 = buf.ema(5)
        assert ema5 is not None
        assert ema5 > 0

    def test_rsi_overbought(self):
        # Steadily rising prices → high RSI
        prices = [100 + i * 0.5 for i in range(30)]
        buf = make_buffer(prices)
        rsi = buf.rsi(14)
        assert rsi is not None
        assert rsi > 70

    def test_rsi_oversold(self):
        # Steadily falling prices → low RSI
        prices = [130 - i * 0.5 for i in range(30)]
        buf = make_buffer(prices)
        rsi = buf.rsi(14)
        assert rsi is not None
        assert rsi < 30

    def test_atr(self):
        buf = make_buffer([100 + i for i in range(25)])
        atr = buf.atr(5)
        assert atr is not None
        assert atr > 0

    def test_snapshot_insufficient_data(self):
        buf = make_buffer([100, 101, 102])
        assert buf.snapshot() is None

    def test_snapshot_sufficient_data(self):
        prices = [100 + i * 0.1 for i in range(25)]
        buf = make_buffer(prices)
        snap = buf.snapshot()
        assert snap is not None
        assert "ret_1m" in snap
        assert "rsi_14" in snap


class TestMomentumSignal:
    def test_uptrend(self):
        prices = [100 + i * 0.3 for i in range(25)]
        buf = make_buffer(prices)
        signal = momentum_signal(buf)
        assert signal is not None
        assert signal > 0

    def test_downtrend(self):
        prices = [107 - i * 0.3 for i in range(25)]
        buf = make_buffer(prices)
        signal = momentum_signal(buf)
        assert signal is not None
        assert signal < 0

    def test_insufficient_data(self):
        buf = make_buffer([100, 101])
        signal = momentum_signal(buf)
        assert signal is None
