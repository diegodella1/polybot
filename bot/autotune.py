"""Auto-tuning loop: analyze recent trades and adjust parameters within guardrails."""

import logging
from datetime import datetime, timezone

from bot.config import get, load_config, save_config
from bot.fair_value import conformal
from bot.state import state
from db import get_db

logger = logging.getLogger(__name__)

# --- Guardrails: (min, max, max_delta_per_cycle) ---
_PARAM_BOUNDS = {
    "min_edge":              (0.04, 0.15, 0.02),
    "max_edge":              (0.12, 0.30, 0.02),
    "edge_regime.tau_120_60":(0.08, 0.25, 0.02),
    "edge_regime.tau_60_30": (0.08, 0.25, 0.02),
    "edge_regime.tau_30_10": (0.08, 0.25, 0.02),
    "edge_regime.tau_10_0":  (0.08, 0.25, 0.02),
    "max_spread_cents":      (2, 10, 1),
    "trade_cooldown_seconds":(3, 30, 5),
    "endgame_min_edge":      (0.12, 0.25, 0.02),
}

# Parameters that must NEVER be touched
_FROZEN_PARAMS = {
    "bankroll_floor_usd", "kill_consecutive_losses", "kill_consecutive_window_min",
    "kill_resume_stable_min", "kill_calibration_eq_max", "kill_resume_eq_max",
    "daily_stop_loss_usd", "fixed_size_usd", "max_fixed_size_usd",
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamp_delta(old: float, new: float, max_delta: float) -> float:
    """Limit change to ±max_delta from old value."""
    delta = new - old
    clamped_delta = _clamp(delta, -max_delta, max_delta)
    return old + clamped_delta


def _safe_adjust(param: str, old: float, proposed: float) -> float:
    """Apply both delta clamp and absolute range clamp."""
    bounds = _PARAM_BOUNDS.get(param)
    if bounds is None:
        return old  # Unknown param, don't touch
    lo, hi, max_delta = bounds
    value = _clamp_delta(old, proposed, max_delta)
    return _clamp(value, lo, hi)


def _get_nested(cfg: dict, dotted_key: str):
    """Get value from nested dict using dot notation."""
    keys = dotted_key.split(".")
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return None
    return val


def _set_nested(cfg: dict, dotted_key: str, value):
    """Set value in nested dict using dot notation."""
    keys = dotted_key.split(".")
    d = cfg
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _tune_from_eq() -> dict:
    """Adjust min_edge based on conformal calibration metric (eq)."""
    eq = conformal.eq
    if eq == 0.0:
        return {}  # Not enough data

    current = get("min_edge", 0.06)

    if eq > 0.12:
        proposed = current + 0.01
        reason = f"eq={eq:.3f} > 0.12 (model miscalibrated, tighten)"
    elif eq < 0.05:
        proposed = current - 0.01
        reason = f"eq={eq:.3f} < 0.05 (well calibrated, relax)"
    else:
        return {}

    new_val = _safe_adjust("min_edge", current, proposed)
    if new_val == current:
        return {}

    return {"min_edge": {"old": current, "new": round(new_val, 4), "reason": reason}}


async def _query_regime_stats(lookback_h: int) -> list[dict]:
    """Query trade stats grouped by tau regime."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT
                CASE
                    WHEN tau_at_entry > 60 THEN 'tau_120_60'
                    WHEN tau_at_entry > 30 THEN 'tau_60_30'
                    WHEN tau_at_entry > 10 THEN 'tau_30_10'
                    ELSE 'tau_10_0'
                END as regime,
                COUNT(*) as n,
                SUM(CASE WHEN outcome IN ('win','take_profit') THEN 1 ELSE 0 END) as wins,
                AVG(pnl) as avg_pnl
            FROM trades
            WHERE outcome IS NOT NULL
              AND tau_at_entry IS NOT NULL
              AND timestamp >= datetime('now', ? || ' hours')
            GROUP BY regime
        """, (f"-{lookback_h}",))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _tune_regime_edges(lookback_h: int, min_trades: int,
                             target_wr: float, margin: float) -> dict:
    """Adjust per-regime edge thresholds based on win rate."""
    rows = await _query_regime_stats(lookback_h)
    changes = {}

    for row in rows:
        regime = row["regime"]
        n = row["n"]
        wins = row["wins"]

        if n < min_trades:
            continue

        wr = wins / n
        param = f"edge_regime.{regime}"
        current = get(param, 0.14)

        if wr < target_wr - margin:
            # Losing too much → raise edge requirement
            proposed = current + 0.02
            reason = f"{regime}: WR={wr:.1%} < {target_wr - margin:.0%} over {n} trades, tighten"
        elif wr > target_wr + margin:
            # Winning well → can relax (conservative: +0.01 instead of +0.02)
            proposed = current - 0.01
            reason = f"{regime}: WR={wr:.1%} > {target_wr + margin:.0%} over {n} trades, relax"
        else:
            continue

        new_val = _safe_adjust(param, current, proposed)
        if new_val != current:
            changes[param] = {"old": current, "new": round(new_val, 4), "reason": reason}

    return changes


async def _query_hourly_stats(lookback_h: int) -> list[dict]:
    """Query trade stats grouped by hour UTC."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT
                CAST(strftime('%%H', timestamp) AS INTEGER) as hour_utc,
                COUNT(*) as n,
                SUM(CASE WHEN outcome IN ('win','take_profit') THEN 1 ELSE 0 END) as wins,
                AVG(pnl) as avg_pnl
            FROM trades
            WHERE outcome IS NOT NULL
              AND timestamp >= datetime('now', ? || ' hours')
            GROUP BY hour_utc
            HAVING COUNT(*) >= 10
        """, (f"-{lookback_h}",))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _tune_active_hours(lookback_h: int, min_trades: int) -> dict:
    """Exclude hours with consistently poor win rate."""
    rows = await _query_hourly_stats(lookback_h)
    current_excluded = set(get("active_hours_utc", []))

    # If active_hours_utc is empty, all hours are active
    # We'll build exclusion list from scratch
    new_excluded = set()

    for row in rows:
        hour = row["hour_utc"]
        n = row["n"]
        wins = row["wins"]

        if n < min_trades:
            continue

        wr = wins / n
        if wr < 0.40:
            new_excluded.add(hour)

    # Hours previously excluded that now perform well → rehabilitate
    # (only if they appear in data with enough trades and WR >= 50%)
    hour_data = {r["hour_utc"]: r for r in rows}
    for hour in list(current_excluded):
        if hour in hour_data and hour_data[hour]["n"] >= min_trades:
            wr = hour_data[hour]["wins"] / hour_data[hour]["n"]
            if wr >= 0.50:
                # Rehabilitate: don't add to new_excluded
                pass
            else:
                new_excluded.add(hour)
        else:
            # Keep exclusion if no data to prove otherwise
            new_excluded.add(hour)

    # Convert to active hours (inverse of excluded)
    if new_excluded:
        active = sorted(h for h in range(24) if h not in new_excluded)
    else:
        active = []  # Empty = all hours active

    current_active = sorted(current_excluded) if current_excluded else []
    current_cfg = get("active_hours_utc", [])

    if active != current_cfg:
        excluded_str = sorted(new_excluded) if new_excluded else []
        return {
            "active_hours_utc": {
                "old": current_cfg,
                "new": active,
                "reason": f"Excluding hours {excluded_str} (WR < 40% over {min_trades}+ trades)",
            }
        }

    return {}


async def _query_spread_stats(lookback_h: int, spread_threshold: float) -> dict:
    """Query WR for trades with high spread."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT
                COUNT(*) as n,
                SUM(CASE WHEN outcome IN ('win','take_profit') THEN 1 ELSE 0 END) as wins
            FROM trades t
            JOIN decisions d ON d.market = t.condition_id
                AND d.ts = t.timestamp
                AND d.decision = 'trade'
            WHERE t.outcome IS NOT NULL
              AND d.spread > ?
              AND t.timestamp >= datetime('now', ? || ' hours')
        """, (spread_threshold, f"-{lookback_h}"))
        row = await cursor.fetchone()
        if row and row["n"]:
            return {"n": row["n"], "wins": row["wins"]}
        return {"n": 0, "wins": 0}
    finally:
        await db.close()


async def _tune_spread(lookback_h: int, min_trades: int,
                       target_wr: float) -> dict:
    """Reduce max_spread_cents if high-spread trades lose too much."""
    current = get("max_spread_cents", 5)
    threshold = current * 0.8 / 100  # 80% of current max, convert cents to fraction

    stats = await _query_spread_stats(lookback_h, threshold)
    if stats["n"] < min_trades:
        return {}

    wr = stats["wins"] / stats["n"]
    if wr < 0.40:
        proposed = current - 1
        new_val = _safe_adjust("max_spread_cents", current, proposed)
        if new_val != current:
            return {
                "max_spread_cents": {
                    "old": current,
                    "new": int(new_val),
                    "reason": f"High-spread trades WR={wr:.1%} < 40% over {stats['n']} trades",
                }
            }

    return {}


async def auto_tune() -> dict:
    """Analyze recent performance and adjust parameters.

    Returns dict with {param: {old, new, reason}} for each change.
    """
    if not get("autotune_enabled", True):
        return {}

    lookback_h = get("autotune_lookback_hours", 24)
    min_trades = get("autotune_min_trades", 20)
    target_wr = get("autotune_target_wr", 0.50)
    margin = get("autotune_wr_margin", 0.05)

    changes = {}

    # 1. Overall calibration via eq
    changes.update(_tune_from_eq())

    # 2. Per-regime analysis
    changes.update(await _tune_regime_edges(lookback_h, min_trades, target_wr, margin))

    # 3. Active hours
    changes.update(await _tune_active_hours(lookback_h, min_trades))

    # 4. Spread threshold
    changes.update(await _tune_spread(lookback_h, min_trades, target_wr))

    # 5. Apply changes
    if changes:
        cfg = load_config()
        for param, delta in changes.items():
            _set_nested(cfg, param, delta["new"])
        save_config(cfg)

        # Log each change
        for param, delta in changes.items():
            logger.info(
                "AUTOTUNE %s: %s → %s (%s)",
                param, delta["old"], delta["new"], delta["reason"],
            )

        # Persist history
        history = await state.get("autotune_history", [])
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in changes.items()},
        })
        # Keep last 50 entries
        if len(history) > 50:
            history = history[-50:]
        await state.set("autotune_history", history)
        await state.set("last_autotune", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": {k: {"old": str(v["old"]), "new": str(v["new"]), "reason": v["reason"]}
                        for k, v in changes.items()},
        })
    else:
        logger.info("AUTOTUNE: no changes needed")

    return changes


def format_autotune_message(changes: dict) -> str:
    """Format autotune changes for Telegram notification."""
    if not changes:
        return ""

    lines = ["🔧 <b>AUTO-TUNE</b>"]
    for param, delta in changes.items():
        lines.append(f"  • <b>{param}</b>: {delta['old']} → {delta['new']}")
        lines.append(f"    {delta['reason']}")
    return "\n".join(lines)
