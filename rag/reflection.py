"""Periodic trade reflection — passive analysis every N trades.

Generates insights from recent trades without modifying bot behavior.
Results are appended to data/reflections.jsonl for review.
"""

import json
import logging
import os
from datetime import datetime, timezone

from db import get_db

logger = logging.getLogger(__name__)

REFLECTION_INTERVAL = 50  # trades between reflections
REFLECTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "reflections.jsonl")


async def maybe_reflect(trade_count: int):
    """Run reflection if trade_count is a multiple of REFLECTION_INTERVAL."""
    if trade_count < REFLECTION_INTERVAL or trade_count % REFLECTION_INTERVAL != 0:
        return
    try:
        report = await _build_reflection(trade_count)
        if report:
            _save_reflection(report)
            logger.info("Reflection #%d saved (%d trades analyzed)", trade_count // REFLECTION_INTERVAL, report["trades_analyzed"])
    except Exception as e:
        logger.warning("Reflection failed: %s", e)


async def _build_reflection(trade_count: int) -> dict | None:
    """Analyze last REFLECTION_INTERVAL resolved trades."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, timestamp, side, signal_score, entry_price, outcome, pnl, btc_price
               FROM trades WHERE outcome IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (REFLECTION_INTERVAL,),
        )
        trades = await cursor.fetchall()
    finally:
        await db.close()

    if len(trades) < REFLECTION_INTERVAL:
        return None

    trades = [dict(t) for t in trades]
    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "win")
    losses = total - wins
    total_pnl = sum(t["pnl"] or 0 for t in trades)

    # WR by signal strength bucket
    signal_buckets = {}
    for lo, hi, label in [(0.10, 0.13, "weak_0.10-0.13"), (0.13, 0.20, "medium_0.13-0.20"), (0.20, 1.0, "strong_0.20+")]:
        bucket_trades = [t for t in trades if lo <= abs(t["signal_score"]) < hi]
        if bucket_trades:
            bw = sum(1 for t in bucket_trades if t["outcome"] == "win")
            bp = sum(t["pnl"] or 0 for t in bucket_trades)
            signal_buckets[label] = {
                "trades": len(bucket_trades),
                "wins": bw,
                "wr": round(bw / len(bucket_trades), 3),
                "pnl": round(bp, 2),
            }

    # WR by side
    side_stats = {}
    for side in ["up", "down"]:
        side_trades = [t for t in trades if t["side"] == side]
        if side_trades:
            sw = sum(1 for t in side_trades if t["outcome"] == "win")
            sp = sum(t["pnl"] or 0 for t in side_trades)
            side_stats[side] = {
                "trades": len(side_trades),
                "wins": sw,
                "wr": round(sw / len(side_trades), 3),
                "pnl": round(sp, 2),
            }

    # WR by entry price bucket
    price_buckets = {}
    for lo, hi, label in [(0.38, 0.50, "cheap_38-50"), (0.50, 0.58, "mid_50-58"), (0.58, 0.66, "expensive_58-65")]:
        pt = [t for t in trades if lo <= t["entry_price"] < hi]
        if pt:
            pw = sum(1 for t in pt if t["outcome"] == "win")
            pp = sum(t["pnl"] or 0 for t in pt)
            price_buckets[label] = {
                "trades": len(pt),
                "wins": pw,
                "wr": round(pw / len(pt), 3),
                "pnl": round(pp, 2),
            }

    # WR by hour (UTC)
    hour_stats = {}
    for t in trades:
        try:
            h = datetime.fromisoformat(t["timestamp"]).hour
        except Exception:
            continue
        if h not in hour_stats:
            hour_stats[h] = {"trades": 0, "wins": 0, "pnl": 0}
        hour_stats[h]["trades"] += 1
        if t["outcome"] == "win":
            hour_stats[h]["wins"] += 1
        hour_stats[h]["pnl"] += t["pnl"] or 0

    for h in hour_stats:
        s = hour_stats[h]
        s["wr"] = round(s["wins"] / s["trades"], 3) if s["trades"] > 0 else 0
        s["pnl"] = round(s["pnl"], 2)

    # Asymmetry
    win_pnls = [t["pnl"] for t in trades if t["outcome"] == "win" and t["pnl"]]
    loss_pnls = [t["pnl"] for t in trades if t["outcome"] != "win" and t["pnl"]]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    # Streaks
    max_win_streak = max_loss_streak = current = 0
    current_type = None
    for t in reversed(trades):  # oldest first
        if t["outcome"] == current_type:
            current += 1
        else:
            current_type = t["outcome"]
            current = 1
        if current_type == "win":
            max_win_streak = max(max_win_streak, current)
        else:
            max_loss_streak = max(max_loss_streak, current)

    # Best/worst signal bucket
    best_bucket = max(signal_buckets.items(), key=lambda x: x[1]["wr"]) if signal_buckets else None
    worst_bucket = min(signal_buckets.items(), key=lambda x: x[1]["wr"]) if signal_buckets else None

    # Synthesize insights
    insights = []
    if best_bucket and best_bucket[1]["wr"] >= 0.65:
        insights.append(f"Best signal range: {best_bucket[0]} ({best_bucket[1]['wr']*100:.0f}% WR)")
    if worst_bucket and worst_bucket[1]["wr"] < 0.50:
        insights.append(f"Losing signal range: {worst_bucket[0]} ({worst_bucket[1]['wr']*100:.0f}% WR) — consider filtering")
    if ratio < 0.70:
        insights.append(f"Win/loss asymmetry problem: avg win ${avg_win:+.2f} vs avg loss ${avg_loss:.2f} (ratio {ratio:.2f}x)")
    if "up" in side_stats and "down" in side_stats:
        up_wr = side_stats["up"]["wr"]
        down_wr = side_stats["down"]["wr"]
        if abs(up_wr - down_wr) > 0.15:
            better = "UP" if up_wr > down_wr else "DOWN"
            insights.append(f"{better} is significantly better: UP {up_wr*100:.0f}% vs DOWN {down_wr*100:.0f}%")
    for label, bs in price_buckets.items():
        if bs["wr"] >= 0.75 and bs["trades"] >= 5:
            insights.append(f"Entry price {label} has high WR: {bs['wr']*100:.0f}%")
        elif bs["wr"] < 0.40 and bs["trades"] >= 5:
            insights.append(f"Entry price {label} is losing: {bs['wr']*100:.0f}% — consider avoiding")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reflection_number": trade_count // REFLECTION_INTERVAL,
        "trades_analyzed": total,
        "overall": {
            "wins": wins,
            "losses": losses,
            "wr": round(wins / total, 3),
            "pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "win_loss_ratio": round(ratio, 2),
        },
        "by_signal_strength": signal_buckets,
        "by_side": side_stats,
        "by_entry_price": price_buckets,
        "by_hour_utc": {str(k): v for k, v in sorted(hour_stats.items())},
        "streaks": {
            "max_win": max_win_streak,
            "max_loss": max_loss_streak,
        },
        "insights": insights,
    }


def _save_reflection(report: dict):
    """Append reflection to JSONL file."""
    os.makedirs(os.path.dirname(REFLECTIONS_PATH), exist_ok=True)
    with open(REFLECTIONS_PATH, "a") as f:
        f.write(json.dumps(report) + "\n")
