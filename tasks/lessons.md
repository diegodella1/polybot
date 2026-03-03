# Polybot Lessons Learned

## Trading Strategy
- **UP signals inverted**: Model predicts UP wrong (9% WR). Inverting to DOWN brought both sides to ~55%+. Flag: `invert_up_signal` in config.yaml
- **Weak signals lose**: Signals below 0.20 are noise, not edge. Raised `trade_threshold` to 0.20
- **Burst trades kill**: 5 trades in 2 minutes = 5 losses. Added `trade_cooldown_seconds: 600` (10 min minimum)
- **Expensive contracts lose**: Buying at >0.55¢ means market already priced it. Lowered `max_entry_price` to 0.55
- **Daily loss hard stop wasteful**: Bot sat idle all day after -15% loss. Replaced with drawdown multiplier (sizing scales down progressively instead of stopping)

## Architecture
- `trade_threshold` already serves as min signal threshold — no need for duplicate config
- `max_entry_price` already serves as max contract price — no need for duplicate config
- Trade cooldown (time-based) lives in risk.py check #8; uses `last_trade_timestamp` in state
- Signal inversion happens in engine.py after composite is computed, before side determination
