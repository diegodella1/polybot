"""Position sizing — signal-proportional with safety caps."""

from bot.config import get


def kelly_size(
    signal_strength: float,
    entry_price: float,
    bankroll: float,
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

    # Guard: invalid price or tiny bankroll
    if entry_price <= 0.01 or entry_price >= 0.99:
        no_trade["reason"] = f"Entry price {entry_price} out of safe range [0.01, 0.99]"
        return no_trade

    if bankroll < 5.0:
        no_trade["reason"] = f"Bankroll ${bankroll:.2f} too low"
        return no_trade

    # Signal-proportional sizing:
    # |signal| 0.10 → base_pct, |signal| 1.0 → max_trade_pct
    sig = abs(signal_strength)
    base_pct = get("base_trade_pct", 0.07)    # 7% at threshold (~$1 with $14 bankroll)
    max_trade_pct = get("max_trade_pct", 0.08)  # 8% at max signal

    # Linear interpolation: stronger signal → bigger fraction
    t = min(sig / 0.5, 1.0)  # normalize signal to 0..1 (0.5 = very strong)
    fraction = base_pct + t * (max_trade_pct - base_pct)

    size_usd = bankroll * fraction

    # Floor: Polymarket FOK minimum is $1 USD
    min_trade = get("min_trade_usd", 1.0)
    if size_usd < min_trade:
        size_usd = min_trade

    # Cap: max 20% of bankroll
    size_usd = min(size_usd, bankroll * 0.20)

    # Calculate shares
    shares = size_usd / entry_price

    return {
        "size_usd": round(size_usd, 2),
        "shares": round(shares, 4),
        "entry_price": entry_price,
        "reason": "",
    }
