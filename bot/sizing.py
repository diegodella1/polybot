"""Position sizing — signal-proportional with safety caps + drawdown scaling."""

from bot.config import get


def _drawdown_multiplier(daily_pnl: float, bankroll: float) -> float:
    """Linear multiplier that shrinks bets as daily losses accumulate.

    Returns 1.0 when no losses, scales down to min_drawdown_multiplier
    when daily loss reaches daily_loss_limit_pct of bankroll.
    """
    if daily_pnl >= 0 or bankroll <= 0:
        return 1.0
    limit_pct = get("daily_loss_limit_pct", 0.15)
    min_mult = get("min_drawdown_multiplier", 0.25)
    t = min(abs(daily_pnl) / (bankroll * limit_pct), 1.0)
    return 1.0 - t * (1.0 - min_mult)


def kelly_size(
    signal_strength: float,
    entry_price: float,
    bankroll: float,
    daily_pnl: float = 0.0,
) -> dict:
    """Size position proportional to signal strength.

    Stronger signal → bigger bet, capped at max_trade_pct of bankroll.
    Keeps the kelly_size name for backward compatibility.
    """
    no_trade = {
        "size_usd": 0,
        "shares": 0,
        "entry_price": entry_price,
        "reason": "",
    }

    # Guard: price outside safe range (risk/reward inverted at extremes)
    min_ep = get("min_entry_price", 0.25)
    max_ep = get("max_entry_price", 0.75)
    if entry_price < min_ep or entry_price > max_ep:
        no_trade["reason"] = f"Entry price {entry_price:.2f} outside safe range [{min_ep}, {max_ep}]"
        return no_trade

    bankroll_floor = get("bankroll_floor_usd", 5.0)
    if bankroll < bankroll_floor:
        no_trade["reason"] = f"Bankroll ${bankroll:.2f} below floor ${bankroll_floor:.2f}"
        return no_trade

    # Signal-proportional sizing:
    # At threshold → base_pct, at strong signal → max_trade_pct
    sig = abs(signal_strength)
    threshold = get("trade_threshold", 0.06)
    base_pct = get("base_trade_pct", 0.07)
    max_trade_pct = get("max_trade_pct", 0.12)

    # Linear interpolation using real range: threshold → 0.5 (strong signal)
    max_sig = 0.5
    t = min((sig - threshold) / (max_sig - threshold), 1.0) if sig > threshold else 0.0
    fraction = base_pct + t * (max_trade_pct - base_pct)

    size_usd = bankroll * fraction

    # Drawdown multiplier: reduce size linearly as daily losses accumulate
    drawdown_mult = _drawdown_multiplier(daily_pnl, bankroll)
    size_usd *= drawdown_mult

    # Cap: respect max_trade_pct from config
    size_usd = min(size_usd, bankroll * max_trade_pct)

    # Floor: Polymarket FOK minimum is $1 USD
    min_trade = get("min_trade_usd", 1.0)
    if size_usd < min_trade:
        # If bankroll can afford the minimum, use it; otherwise skip
        if bankroll * max_trade_pct >= min_trade:
            size_usd = min_trade
        else:
            no_trade["reason"] = f"Size ${size_usd:.2f} below Polymarket min ${min_trade:.2f}"
            return no_trade

    # Calculate shares
    shares = size_usd / entry_price

    return {
        "size_usd": round(size_usd, 2),
        "shares": round(shares, 4),
        "entry_price": entry_price,
        "drawdown_multiplier": round(drawdown_mult, 2),
        "reason": "",
    }
