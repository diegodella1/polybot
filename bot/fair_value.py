"""Fair value model: estimate real P(up) from realized volatility.

The idea (credit: Fernando Lopez): people misprice the distribution of
BTC moves in short timeframes.  Instead of predicting direction, we
estimate the *real* probability of an up move using recent realized
volatility and micro-drift, then buy only when the market underprices
that probability (after fees).

Model:
    σ_5m = std(5-candle returns) from recent history
    μ_5m = EMA of recent 5-candle returns (short-term drift)
    P(up) = Φ(μ / σ)  where Φ = normal CDF
    Edge  = P(side) - market_price × (1 + fee)
"""

import math
import logging
from collections import deque
from dataclasses import dataclass

from data.buffer import PriceBuffer

logger = logging.getLogger(__name__)

# Taker fee on Polymarket 5-min crypto markets: 10% (fee_rate_bps=1000)
# Confirmed: taker_base_fee=1000 on current BTC Up/Down 5-min markets.
TAKER_FEE = 0.10

# Minimum number of 5-candle windows needed for reliable vol estimate
MIN_WINDOWS = 10


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using the error function (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class FairValueEstimate:
    prob_up: float          # Estimated real P(BTC up in 5 min)
    prob_down: float        # 1 - prob_up
    vol_5m: float           # Realized 5-min volatility (σ)
    drift_5m: float         # Recent 5-min drift (μ)
    n_windows: int          # How many windows used for estimation


@dataclass
class EdgeResult:
    has_edge: bool
    side: str | None        # "up" or "down" or None
    edge: float             # P(side) - breakeven
    prob: float             # Our estimated probability for chosen side
    market_price: float     # What the market charges
    breakeven: float        # Price × (1 + fee) — need prob > this
    fair_value: FairValueEstimate | None


class ConformalTracker:
    """Track prediction residuals and compute conformal calibration metric (eq).

    eq = 90th percentile of absolute residuals over a rolling window.
    Higher eq means the model is less calibrated → tighten risk.
    """

    def __init__(self, max_size: int = 200):
        self._residuals: deque[float] = deque(maxlen=max_size)

    def update(self, y: float, phat: float):
        """Record actual outcome (1=up, 0=down) vs predicted probability.

        Loss residuals are weighted 2x so eq rises faster when the model
        fails on losing trades, enabling quicker reaction to degradation.
        """
        residual = abs(y - phat)
        self._residuals.append(residual)
        # Asymmetric weighting: losses (y=0 when we predicted high, or y=1
        # when we predicted low) get double-counted so eq tightens faster
        if (y == 0.0 and phat > 0.5) or (y == 1.0 and phat < 0.5):
            self._residuals.append(residual)

    def conformal_low(self, phat: float) -> float:
        """Conservative lower bound: phat - eq."""
        return max(0.0, phat - self.eq)

    @property
    def eq(self) -> float:
        """90th percentile of absolute residuals (calibration error)."""
        if len(self._residuals) < 10:
            return 0.0
        sorted_res = sorted(self._residuals)
        idx = int(len(sorted_res) * 0.9)
        return sorted_res[min(idx, len(sorted_res) - 1)]

    @property
    def residuals_list(self) -> list[float]:
        """Snapshot for persistence."""
        return list(self._residuals)

    def restore(self, residuals: list[float]):
        """Restore from persisted state."""
        self._residuals.clear()
        for r in residuals:
            self._residuals.append(r)


# Module-level singleton
conformal = ConformalTracker()


def estimate_fair_value(buf: PriceBuffer, duration_min: float = 5) -> FairValueEstimate | None:
    """Estimate P(up) from realized volatility, scaled to market duration.

    Uses overlapping 5-candle returns from the 1-min candle buffer.
    For durations > 5 min, vol scales by √(T/5), drift by T/5.
    """
    candles = list(buf._candles)
    n = len(candles)

    # Need enough candles for at least MIN_WINDOWS 5-candle windows
    if n < MIN_WINDOWS + 5:
        return None

    # Calculate 5-candle (≈5 min) returns
    returns_5m = []
    for i in range(5, n):
        old_close = candles[i - 5].close
        new_close = candles[i].close
        if old_close > 0:
            returns_5m.append((new_close - old_close) / old_close)

    if len(returns_5m) < MIN_WINDOWS:
        return None

    # Use recent windows (last 60 = ~1 hour of data)
    recent = returns_5m[-60:]

    # Realized volatility: std of 5-min returns
    mean_ret = sum(recent) / len(recent)
    variance = sum((r - mean_ret) ** 2 for r in recent) / len(recent)
    vol_5m = math.sqrt(variance) if variance > 0 else 1e-8

    # Short-term drift: EMA of recent returns (more weight on latest)
    # Use exponential weighting with α = 0.1
    alpha = 0.1
    ema_drift = recent[0]
    for r in recent[1:]:
        ema_drift = alpha * r + (1 - alpha) * ema_drift

    # Also incorporate live micro-momentum from last 1-min return
    ret_1m = buf.ret(1, use_live=True)
    if ret_1m is not None:
        # Scale 1-min return to 5-min equivalent and blend
        # Give live momentum 15% weight (30% was too noisy)
        live_drift = ret_1m * math.sqrt(5)  # Scale by √5
        ema_drift = 0.85 * ema_drift + 0.15 * live_drift

    # Scale vol and drift to market duration (σ_T = σ_5m × √(T/5), μ_T = μ_5m × T/5)
    scale = duration_min / 5.0
    vol = vol_5m * math.sqrt(scale)
    drift = ema_drift * scale

    # P(up) = Φ(μ / σ) — probability that a normal(μ, σ) draw is > 0
    if vol > 0:
        z_score = drift / vol
        prob_up = _norm_cdf(z_score)
    else:
        prob_up = 0.5

    # Clamp: cap at 0.75 to avoid overconfidence (>75% loses money in backtests)
    prob_up = max(0.05, min(0.75, prob_up))

    return FairValueEstimate(
        prob_up=prob_up,
        prob_down=1.0 - prob_up,
        vol_5m=vol_5m,       # Store base 5m vol for compatibility
        drift_5m=ema_drift,  # Store base 5m drift for compatibility
        n_windows=len(recent),
    )


def find_edge(
    fv: FairValueEstimate,
    price_up: float,
    price_down: float,
    fee: float = TAKER_FEE,
) -> EdgeResult:
    """Determine if there's a positive EV trade given fair value vs market prices.

    Edge exists when: P(side) > market_price × (1 + fee)
    We pick the side with the MOST edge (if any).
    """
    no_edge = EdgeResult(
        has_edge=False, side=None, edge=0.0,
        prob=0.0, market_price=0.0, breakeven=0.0,
        fair_value=fv,
    )

    if price_up <= 0 or price_down <= 0:
        return no_edge

    # Breakeven probability: need P(side) > price × (1 + fee) to have +EV
    breakeven_up = price_up * (1 + fee)
    breakeven_down = price_down * (1 + fee)

    edge_up = fv.prob_up - breakeven_up
    edge_down = fv.prob_down - breakeven_down

    # Asymmetric penalty: DOWN signals are noisier (observed bias),
    # require 3% more edge to compensate
    DOWN_EDGE_PENALTY = 0.08
    edge_down -= DOWN_EDGE_PENALTY

    # Pick the better side (if any has positive edge)
    if edge_up > 0 and edge_up >= edge_down:
        return EdgeResult(
            has_edge=True, side="up", edge=edge_up,
            prob=fv.prob_up, market_price=price_up,
            breakeven=breakeven_up, fair_value=fv,
        )
    elif edge_down > 0:
        return EdgeResult(
            has_edge=True, side="down", edge=edge_down,
            prob=fv.prob_down, market_price=price_down,
            breakeven=breakeven_down, fair_value=fv,
        )

    return no_edge
