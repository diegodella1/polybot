"""Signal generation: composite trading signal from multiple sources."""

import math
import logging

from bot.config import get
from data.buffer import PriceBuffer
from data.polymarket_ws import PolymarketWS

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _normalize(value: float, scale: float = 1.0) -> float:
    return _clamp(math.tanh(value * scale))


def momentum_signal(buf: PriceBuffer) -> float | None:
    snap = buf.snapshot()
    if snap is None:
        return None
    ret_1m = snap["ret_1m"]
    ret_5m = snap["ret_5m"]
    ema_fast = snap["ema_fast"]
    ema_slow = snap["ema_slow"]
    price = snap["price"]

    if any(v is None for v in (ret_1m, ret_5m, ema_fast, ema_slow)):
        return None
    if price is None or price == 0:
        return None

    raw = 0.5 * ret_1m + 0.3 * ret_5m + 0.2 * (ema_fast - ema_slow) / price
    return _normalize(raw, scale=150)


def rsi_signal(buf: PriceBuffer) -> float | None:
    """RSI normalized to [-1, 1]. RSI > 50 → positive (bullish), < 50 → negative."""
    snap = buf.snapshot()
    if snap is None:
        return None
    rsi = snap.get("rsi_14")
    if rsi is None:
        return None
    return _clamp((rsi - 50.0) / 50.0)


def book_skew_signal(poly_ws: PolymarketWS) -> float | None:
    """Orderbook imbalance. Returns None if book is empty or one-sided."""
    ob = poly_ws.orderbook
    if not ob.bids or not ob.asks:
        return None
    if ob.bid_volume(5) < 0.10 or ob.ask_volume(5) < 0.10:
        return None
    return _clamp(ob.imbalance(levels=5))


def compute_signal(
    buf: PriceBuffer,
    poly_ws: PolymarketWS,
) -> dict:
    """Compute composite trading signal. Returns dict with components + total."""
    weights = get("weights", {})
    w_mom = weights.get("momentum", 0.70)
    w_skew = weights.get("book_skew", 0.15)
    w_rsi = weights.get("rsi", 0.15)

    # Normalize weights to sum to 1.0
    w_total = w_mom + w_skew + w_rsi
    if w_total > 0:
        w_mom /= w_total
        w_skew /= w_total
        w_rsi /= w_total

    mom = momentum_signal(buf)
    skew = book_skew_signal(poly_ws)
    rsi = rsi_signal(buf)

    result = {
        "momentum": mom,
        "book_skew": skew,
        "rsi": rsi,
        "vol_ratio": buf.vol_ratio(),
        "composite": None,
        "tradeable": False,
    }

    if mom is None:
        return result

    # Composite — only weight signals that are actually contributing
    # Dead signals (None) don't dilute the composite
    components = []
    components.append((w_mom, mom))
    if skew is not None:
        components.append((w_skew, skew))
    if rsi is not None:
        components.append((w_rsi, rsi))

    active_weight = sum(w for w, _ in components)
    if active_weight > 0:
        composite = sum(w * v for w, v in components) / active_weight
    else:
        composite = 0.0

    composite = _clamp(composite)
    result["composite"] = composite

    threshold = get("trade_threshold", 0.20)
    max_signal = get("max_signal", 0.35)
    result["tradeable"] = threshold <= abs(composite) <= max_signal

    return result
