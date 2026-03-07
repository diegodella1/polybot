"""Risk manager — gatekeeper before every trade."""

import logging
import time
from datetime import datetime, timezone

from bot.config import get
from bot.state import state
from db import get_db

logger = logging.getLogger(__name__)


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

    # 2. Signal threshold
    threshold = get("trade_threshold", 0.15)
    if abs(signal_score) < threshold:
        return RiskCheck(False, f"Signal {signal_score:.3f} below threshold {threshold}")

    # 4. Bankroll floor (emergency stop)
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
