"""Ring buffer for price data with TA indicators (EMA, ATR, RSI)."""

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: int = 0


class PriceBuffer:
    """Fixed-size ring buffer that computes TA indicators on the fly."""

    # Max age in seconds before data is considered stale (3 minutes)
    STALE_THRESHOLD = 180

    def __init__(self, max_size: int = 100):
        self._candles: deque[Candle] = deque(maxlen=max_size)
        self._current: Candle | None = None  # In-progress candle (not yet closed)
        self._last_update: float = 0.0  # time.time() of last update

    @property
    def size(self) -> int:
        return len(self._candles)

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self._candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self._candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self._candles]

    @property
    def current_price(self) -> float | None:
        if self._current:
            return self._current.close
        if self._candles:
            return self._candles[-1].close
        return None

    @property
    def is_stale(self) -> bool:
        """True if no data received within STALE_THRESHOLD seconds."""
        if self._last_update == 0:
            return True
        return (time.time() - self._last_update) > self.STALE_THRESHOLD

    def update(self, kline):
        """Update buffer with a kline tick. Appends on close."""
        self._last_update = time.time()
        candle = Candle(
            open=kline.open,
            high=kline.high,
            low=kline.low,
            close=kline.close,
            volume=kline.volume,
            timestamp=kline.open_time,
        )
        if kline.is_closed:
            self._candles.append(candle)
            self._current = None
        else:
            self._current = candle

    def ret(self, periods: int) -> float | None:
        """Return over N closed candles."""
        if len(self._candles) < periods + 1:
            return None
        old = self._candles[-(periods + 1)].close
        new = self._candles[-1].close
        if old == 0:
            return None
        return (new - old) / old

    def ema(self, period: int) -> float | None:
        """Exponential moving average of closes."""
        closes = self.closes
        if len(closes) < period:
            return None
        k = 2.0 / (period + 1)
        ema_val = closes[0]
        for c in closes[1:]:
            ema_val = c * k + ema_val * (1 - k)
        return ema_val

    def atr(self, period: int) -> float | None:
        """Average True Range."""
        if len(self._candles) < period + 1:
            return None
        trs = []
        candles = list(self._candles)
        for i in range(-period, 0):
            h = candles[i].high
            l = candles[i].low
            pc = candles[i - 1].close
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return sum(trs) / len(trs)

    def rsi(self, period: int = 14) -> float | None:
        """Relative Strength Index."""
        closes = self.closes
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        recent = deltas[-period:]
        gains = [d for d in recent if d > 0]
        losses = [-d for d in recent if d < 0]
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def vol_ratio(self) -> float | None:
        """ATR(5) / ATR(20) — volatility regime indicator."""
        atr5 = self.atr(5)
        atr20 = self.atr(20)
        if atr5 is None or atr20 is None or atr20 == 0:
            return None
        return atr5 / atr20

    def snapshot(self) -> dict | None:
        """Current TA snapshot for signal generation.

        Returns None if data is stale (no updates in STALE_THRESHOLD seconds).
        """
        if self.size < 21:
            return None
        if self.is_stale:
            return None
        price = self.current_price
        if price is None:
            return None
        return {
            "price": price,
            "ret_1m": self.ret(1),
            "ret_5m": self.ret(5),
            "ret_15m": self.ret(15),
            "ema_fast": self.ema(5),
            "ema_slow": self.ema(15),
            "atr_5": self.atr(5),
            "atr_20": self.atr(20),
            "vol_ratio": self.vol_ratio(),
            "rsi_14": self.rsi(14),
        }
