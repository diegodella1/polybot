"""Risk manager — gatekeeper before every trade."""

import logging
import time
from collections import deque
from datetime import datetime, timezone

from bot.config import get
from bot.state import state
from db import get_db

logger = logging.getLogger(__name__)

# --- Ring buffers for v2 strategy gates ---
_stale_reads: deque[float] = deque(maxlen=50)       # timestamps of stale data reads
_slippage_events: deque[tuple[float, float]] = deque(maxlen=100)  # (timestamp, slippage)
_kill_switch_active = False
_kill_switch_activated_at: float = 0.0


class RiskCheck:
    __slots__ = ("allowed", "reason")

    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self):
        return self.allowed


async def check_risk(signal_score: float, bankroll: float) -> RiskCheck:
    """Run all risk checks. Returns RiskCheck with allowed/reason."""

    # 1. Bot enabled?
    enabled = await state.get("enabled", True)
    if not enabled:
        return RiskCheck(False, "Bot is stopped")

    # 2. Bankroll floor (emergency stop)
    bankroll_floor = get("bankroll_floor_usd", 5.0)
    if bankroll < bankroll_floor:
        return RiskCheck(False, f"Bankroll ${bankroll:.2f} below floor ${bankroll_floor:.2f}")

    # 5. Consecutive losses → cooldown
    consec_losses = await state.get("consecutive_losses", 0)
    max_consec = get("max_consecutive_losses", 3)
    if consec_losses >= max_consec:
        cooldown_remaining = await state.get("cooldown_remaining", 0)
        if cooldown_remaining > 0:
            return RiskCheck(
                False,
                f"Cooldown: {cooldown_remaining} rounds remaining after {consec_losses} losses",
            )

    # 6. Circuit breaker: win rate check
    min_wr = get("min_win_rate", None)
    window = get("circuit_breaker_window", 40)
    if min_wr:
        win_rate = await _recent_win_rate()
        if win_rate is not None and win_rate < min_wr:
            return RiskCheck(
                False,
                f"Circuit breaker: win rate {win_rate:.1%} < {min_wr:.1%} (last {window})",
            )

    # 7. Max exposure check
    max_exposure = get("max_exposure_usd", 15.0)
    current_exposure = await state.get("current_exposure", 0.0)
    if current_exposure >= max_exposure:
        return RiskCheck(False, f"Max exposure reached: ${current_exposure:.2f}")

    # 8. Open position check
    has_open = await state.get("has_open_position", False)
    if has_open:
        return RiskCheck(False, "Position already open, waiting for resolution")

    # 9. Trade cooldown (time-based)
    cooldown_secs = get("trade_cooldown_seconds", 0)
    if cooldown_secs > 0:
        last_trade_ts = await state.get("last_trade_timestamp", 0.0)
        elapsed = time.time() - last_trade_ts
        if elapsed < cooldown_secs:
            remaining = int(cooldown_secs - elapsed)
            mins = remaining // 60
            secs = remaining % 60
            return RiskCheck(
                False,
                f"Trade cooldown: {mins}m{secs:02d}s remaining",
            )

    # 10. Post-loss cooldown (longer pause after a loss)
    post_loss_cd = get("post_loss_cooldown_seconds", 0)
    if post_loss_cd > 0:
        last_loss_ts = await state.get("last_loss_timestamp", 0.0)
        if last_loss_ts > 0:
            elapsed = time.time() - last_loss_ts
            if elapsed < post_loss_cd:
                remaining = int(post_loss_cd - elapsed)
                mins = remaining // 60
                secs = remaining % 60
                return RiskCheck(
                    False,
                    f"Post-loss cooldown: {mins}m{secs:02d}s remaining",
                )

    return RiskCheck(True)


async def _recent_win_rate() -> float | None:
    """Win rate over the last N trades."""
    window = get("circuit_breaker_window", 40)
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT outcome FROM trades
               WHERE outcome IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (window,),
        )
        rows = await cursor.fetchall()
        if len(rows) < window:
            return None
        wins = sum(1 for r in rows if r["outcome"] in ("win", "take_profit"))
        return wins / len(rows)
    finally:
        await db.close()


async def record_outcome(won: bool, pnl: float, bankroll: float):
    """Update risk state after trade resolution."""
    if won:
        await state.set("consecutive_losses", 0)
    else:
        await state.set("last_loss_timestamp", time.time())
        consec = await state.get("consecutive_losses", 0)
        consec += 1
        await state.set("consecutive_losses", consec)
        if consec >= get("max_consecutive_losses", 3):
            cooldown = get("cooldown_rounds", 6)
            await state.set("cooldown_remaining", cooldown)
            logger.warning("Entering cooldown: %d rounds after %d losses", cooldown, consec)

    daily_pnl = await state.get("daily_pnl", 0.0)
    await state.set("daily_pnl", daily_pnl + pnl)
    await state.set("bankroll", bankroll)
    # Clear position tracking
    await state.set("has_open_position", False)
    await state.set("current_exposure", 0.0)


async def decrement_cooldown():
    """Call once per round to count down cooldown.

    Returns True if cooldown was active (for engine to know if this round counted).
    """
    remaining = await state.get("cooldown_remaining", 0)
    if remaining > 0:
        await state.set("cooldown_remaining", remaining - 1)
        if remaining - 1 == 0:
            await state.set("consecutive_losses", 0)
            logger.info("Cooldown ended, resetting consecutive losses")
        return True
    return False


async def reset_daily():
    """Reset daily counters. Call at midnight UTC."""
    bankroll = await state.get("bankroll", 0.0)
    await state.set("bankroll_open", bankroll)
    await state.set("daily_pnl", 0.0)
    await state.set("daily_fees", 0.0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await state.set("daily_reset_date", today)


# ─── Strategy v2 gates (additive, don't touch existing functions) ───


def check_regime_edge(
    tau: float,
    edge: float,
    spread: float | None,
    vol_5m: float,
    vol_median: float,
    microspike: bool,
    delta_dir: str,
    side: str,
) -> RiskCheck:
    """Apply edge thresholds by temporal regime (tau = seconds remaining).

    Each regime has a min edge requirement. Late regimes also have spread limits.
    Microspike in opposing direction → block.
    """
    regime = get("edge_regime", {})

    # Determine regime and required edge
    if tau > 60:
        req_edge = regime.get("tau_120_60", 0.18)
    elif tau > 30:
        req_edge = regime.get("tau_60_30", 0.14)
    elif tau > 10:
        req_edge = regime.get("tau_30_10", 0.12)
    else:
        req_edge = regime.get("tau_10_0", 0.15)

    if edge < req_edge:
        return RiskCheck(
            False,
            f"Edge {edge:.1%} < regime req {req_edge:.1%} (tau={tau:.0f}s)",
        )

    # Late-entry spread limit
    if tau <= 30 and spread is not None:
        late_max = regime.get("late_max_spread", 0.03)
        if spread > late_max:
            return RiskCheck(
                False,
                f"Late spread {spread:.4f} > {late_max} (tau={tau:.0f}s)",
            )

    # Microspike opposing direction → block
    if microspike:
        # If BTC spiked up and we're buying down (or vice versa), block
        if delta_dir != side:
            return RiskCheck(False, f"Microspike against {side} (delta={delta_dir})")

    return RiskCheck(True)


async def check_kill_switch() -> RiskCheck:
    """Multi-condition kill switch.

    Activates on any of:
    - 2+ stale reads in last 10s
    - 2+ abnormal slippage events (>2x median) in last 5min
    - eq > kill_calibration_eq_max
    - N consecutive losses within window

    Resumes after kill_resume_stable_min minutes of stability.
    """
    global _kill_switch_active, _kill_switch_activated_at

    now = time.time()

    # Check if kill switch should resume
    if _kill_switch_active:
        resume_min = get("kill_resume_stable_min", 10)
        elapsed_min = (now - _kill_switch_activated_at) / 60.0
        if elapsed_min < resume_min:
            return RiskCheck(False, f"Kill switch active ({elapsed_min:.1f}/{resume_min}min)")

        # Check eq for resume
        from bot.fair_value import conformal
        resume_eq = get("kill_resume_eq_max", 0.10)
        if conformal.eq > resume_eq:
            return RiskCheck(False, f"Kill switch: eq={conformal.eq:.3f} > resume threshold {resume_eq}")

        # Stable enough — deactivate
        _kill_switch_active = False
        logger.info("Kill switch deactivated after %.1f min stability", elapsed_min)

    # --- Check trigger conditions ---

    # 1. Stale reads: 2+ in last 10s
    recent_stale = sum(1 for ts in _stale_reads if now - ts < 10)
    if recent_stale >= 2:
        _activate_kill("2+ stale reads in 10s")
        return RiskCheck(False, "Kill switch: stale data")

    # 2. Abnormal slippage: 2+ events with slippage > 2x in last 5min
    if len(_slippage_events) >= 5:
        recent_slip = [(ts, s) for ts, s in _slippage_events if now - ts < 300]
        if len(recent_slip) >= 2:
            median_slip = sorted(s for _, s in _slippage_events)[len(_slippage_events) // 2]
            abnormal = sum(1 for _, s in recent_slip if s > median_slip * 2)
            if abnormal >= 2:
                _activate_kill(f"2+ abnormal slippage in 5min (median={median_slip:.4f})")
                return RiskCheck(False, "Kill switch: abnormal slippage")

    # 3. Conformal calibration
    from bot.fair_value import conformal
    eq_max = get("kill_calibration_eq_max", 0.14)
    if conformal.eq > eq_max:
        _activate_kill(f"eq={conformal.eq:.3f} > {eq_max}")
        return RiskCheck(False, f"Kill switch: eq={conformal.eq:.3f}")

    # 4. Consecutive losses within window
    kill_losses = get("kill_consecutive_losses", 3)
    kill_window = get("kill_consecutive_window_min", 10)
    consec = await state.get("consecutive_losses", 0)
    if consec >= kill_losses:
        last_loss_ts = await state.get("last_loss_timestamp", 0.0)
        if now - last_loss_ts < kill_window * 60:
            _activate_kill(f"{consec} consecutive losses in {kill_window}min")
            return RiskCheck(False, f"Kill switch: {consec} losses in {kill_window}min")

    return RiskCheck(True)


def _activate_kill(reason: str):
    """Activate kill switch with logging."""
    global _kill_switch_active, _kill_switch_activated_at
    _kill_switch_active = True
    _kill_switch_activated_at = time.time()
    logger.warning("KILL SWITCH ACTIVATED: %s", reason)


def check_endgame(
    tau: float,
    spread: float | None,
    vol_5m: float,
    vol_prev: float,
    edge: float,
) -> RiskCheck:
    """Block trades in the last N seconds unless conditions are strong.

    Endgame (last 20s): block if spread widens or vol increases,
    UNLESS edge >= endgame_min_edge AND spread <= endgame_max_spread.
    """
    endgame_sec = get("endgame_seconds", 20)
    if tau > endgame_sec:
        return RiskCheck(True)  # Not in endgame

    min_edge = get("endgame_min_edge", 0.18)
    max_spread = get("endgame_max_spread", 0.02)

    # Strong enough to trade in endgame?
    if edge >= min_edge and (spread is None or spread <= max_spread):
        return RiskCheck(True)

    # Block: conditions not met for endgame trading
    reasons = []
    if edge < min_edge:
        reasons.append(f"edge {edge:.1%} < {min_edge:.1%}")
    if spread is not None and spread > max_spread:
        reasons.append(f"spread {spread:.4f} > {max_spread}")
    if vol_prev > 0 and vol_5m > vol_prev * 1.2:
        reasons.append("vol rising")

    return RiskCheck(False, f"Endgame block (tau={tau:.0f}s): {', '.join(reasons)}")


def check_daily_dollar_limits(daily_pnl: float) -> RiskCheck:
    """Check daily stop-loss and profit target in absolute USD."""
    stop_loss = get("daily_stop_loss_usd", 2.0)
    profit_target = get("daily_profit_target_usd", 2.0)

    if daily_pnl <= -stop_loss:
        return RiskCheck(False, f"Daily stop loss: ${daily_pnl:.2f} <= -${stop_loss:.2f}")

    if profit_target > 0 and daily_pnl >= profit_target:
        return RiskCheck(False, f"Daily profit target: ${daily_pnl:.2f} >= ${profit_target:.2f}")

    return RiskCheck(True)


def check_slippage_tightening() -> float:
    """If recent p95 slippage > threshold, return edge bonus to add to required edge.

    Returns 0.0 if no tightening needed, otherwise the bonus edge required.
    """
    now = time.time()
    duration = get("slippage_tighten_duration_min", 30) * 60
    p95_threshold = get("slippage_tighten_p95", 0.02)
    bonus = get("slippage_tighten_edge_bonus", 0.02)

    recent = [s for ts, s in _slippage_events if now - ts < duration]
    if len(recent) < 5:
        return 0.0

    sorted_slip = sorted(recent)
    p95_idx = int(len(sorted_slip) * 0.95)
    p95 = sorted_slip[min(p95_idx, len(sorted_slip) - 1)]

    if p95 > p95_threshold:
        logger.info("Slippage tightening: p95=%.4f > %.4f, adding +%.1f%% edge", p95, p95_threshold, bonus * 100)
        return bonus

    return 0.0


def record_stale_read():
    """Record a stale data read for kill switch tracking."""
    _stale_reads.append(time.time())


def record_slippage(slippage: float):
    """Record a slippage event for kill switch and tightening tracking."""
    _slippage_events.append((time.time(), abs(slippage)))
