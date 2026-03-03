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
    return _normalize(raw, scale=500)


def book_skew_signal(poly_ws: PolymarketWS) -> float | None:
    """Orderbook imbalance. Returns None if book is empty or one-sided."""
    ob = poly_ws.orderbook
    # Need BOTH sides to compute meaningful imbalance
    if not ob.bids or not ob.asks:
        return None
    # Need minimum liquidity on each side
    if ob.bid_volume(5) < 0.10 or ob.ask_volume(5) < 0.10:
        return None
    return _clamp(ob.imbalance(levels=5))


def volatility_multiplier(buf: PriceBuffer) -> tuple[float, float]:
    """Returns (momentum_mult, fair_value_mult) based on vol regime."""
    vr = buf.vol_ratio()
    if vr is None:
        return (1.0, 1.0)

    if vr > 1.5:
        return (1.3, 0.7)
    elif vr < 0.7:
        return (0.7, 1.3)
    else:
        t = (vr - 0.7) / 0.8
        mom_mult = 0.7 + t * 0.6
        fv_mult = 1.3 - t * 0.6
        return (mom_mult, fv_mult)


def fair_value_signal(
    buf: PriceBuffer, poly_ws: PolymarketWS, mom: float
) -> float | None:
    """Gap between estimated fair value and market price.

    Uses midpoint (not just ask) for symmetric comparison.
    Fair value estimation accounts for current market price, not anchored at 0.5.
    """
    ob = poly_ws.orderbook
    mid = ob.midpoint
    if mid is None:
        return None

    # Estimate fair value: current midpoint + momentum adjustment
    # Small correction: if BTC momentum is positive, Up token should be slightly higher
    fair_value = mid + mom * 0.05
    gap = fair_value - mid

    # Only meaningful if gap is significant relative to spread
    spread = ob.spread
    if spread is not None and spread > 0:
        # Gap must be at least half the spread to matter
        if abs(gap) < spread * 0.5:
            return 0.0

    return _clamp(gap * 10)


def compute_signal(
    buf: PriceBuffer,
    poly_ws: PolymarketWS,
    rag_signal: float = 0.0,
    sentiment_signal: float = 0.0,
) -> dict:
    """Compute composite trading signal. Returns dict with components + total."""
    # Validate external inputs
    rag_signal = _clamp(rag_signal) if isinstance(rag_signal, (int, float)) and math.isfinite(rag_signal) else 0.0
    sentiment_signal = _clamp(sentiment_signal) if isinstance(sentiment_signal, (int, float)) and math.isfinite(sentiment_signal) else 0.0

    weights = get("weights", {})
    w_mom = weights.get("momentum", 0.30)
    w_skew = weights.get("book_skew", 0.20)
    w_fv = weights.get("fair_value", 0.20)
    w_rag = weights.get("rag_pattern", 0.10)
    w_sent = weights.get("sentiment", 0.05)

    # Normalize weights to sum to 1.0
    w_total = w_mom + w_skew + w_fv + w_rag + w_sent
    if w_total > 0:
        w_mom /= w_total
        w_skew /= w_total
        w_fv /= w_total
        w_rag /= w_total
        w_sent /= w_total

    mom = momentum_signal(buf)
    skew = book_skew_signal(poly_ws)
    mom_mult, fv_mult = volatility_multiplier(buf)

    result = {
        "momentum": mom,
        "book_skew": skew,
        "vol_ratio": buf.vol_ratio(),
        "fair_value": None,
        "rag_pattern": rag_signal,
        "sentiment": sentiment_signal,
        "composite": None,
        "tradeable": False,
    }

    if mom is None:
        return result

    fv = fair_value_signal(buf, poly_ws, mom)
    result["fair_value"] = fv

    # Composite — only weight signals that are actually contributing
    # Dead signals (None or 0.0) don't dilute the composite
    components = []
    components.append((w_mom, mom_mult * mom))
    if skew is not None:
        components.append((w_skew, skew))
    if fv is not None and fv != 0.0:
        components.append((w_fv, fv_mult * fv))
    if rag_signal != 0.0:
        components.append((w_rag, rag_signal))
    if sentiment_signal != 0.0:
        components.append((w_sent, sentiment_signal))

    active_weight = sum(w for w, _ in components)
    if active_weight > 0:
        composite = sum(w * v for w, v in components) / active_weight
    else:
        composite = 0.0

    composite = _clamp(composite)
    result["composite"] = composite

    threshold = get("trade_threshold", 0.15)
    result["tradeable"] = abs(composite) >= threshold

    return result
