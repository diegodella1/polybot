# Polybot Lessons Learned

## Trading Strategy
- **Signal simplification (2026-03-05)**: 6 signals mal calibradas (23% WR) reemplazadas por 3: momentum (70%), RSI (15%), book_skew (15%). Fair value estaba roto conceptualmente (comparaba midpoint consigo mismo), RAG sesgado positivo, sentiment demasiado lento para 5min. Eliminadas: fair_value, rag, sentiment, chainlink, volatility_multiplier, negaciones hardcodeadas
- **UP signals inverted**: Model predicts UP wrong (9% WR). Inverting to DOWN brought both sides to ~55%+. Flag: `invert_up_signal` in config.yaml (currently disabled post-rewrite)
- **Weak signals lose**: Signals below 0.20 are noise, not edge. `trade_threshold: 0.20`
- **Burst trades kill**: 5 trades in 2 minutes = 5 losses. Added `trade_cooldown_seconds: 60`
- **Expensive contracts lose**: Buying at >0.55¢ means market already priced it. Lowered `max_entry_price` to 0.55
- **Daily loss hard stop wasteful**: Bot sat idle all day after -15% loss. Replaced with drawdown multiplier (sizing scales down progressively instead of stopping)

## Architecture
- `trade_threshold` already serves as min signal threshold — no need for duplicate config
- `max_entry_price` already serves as max contract price — no need for duplicate config
- Trade cooldown (time-based) lives in risk.py check #8; uses `last_trade_timestamp` in state
- Signal inversion happens in engine.py after composite is computed, before side determination

## Polymarket Fees & Economics (2026-03-04)
- **5-min crypto markets charge 10% taker fee** (`fee_rate_bps: 1000`), NOT 2%
- Fee is on BUY (entry) and SELL (early exit), NOT on redemption (CTF → USDC.e)
- Confirmed via `client.get_trades()` which shows actual fills with fee_rate_bps
- Win PnL = `shares × $1.00 - size_usd × 1.10`
- Loss PnL = `-(size_usd × 1.10)`
- With 10% entry fee, need to buy at ≤0.45¢ to have decent risk/reward
- At 0.50¢ entry: win $1.00 - $0.55 cost = $0.45 profit (45%), lose $0.55 (55%). Break-even at ~55% WR
- At 0.25¢ entry: win $1.00 - $0.275 cost = $0.725 profit (263%), lose $0.275. Break-even at ~27.5% WR
- **Conclusión**: Apuntar a entries baratas (<0.40¢) maximiza profit por trade

## Data Integrity & Reconciliation
- DB `size_usd` = `shares × limit_price` (with slippage), NOT actual fill price
- Actual fill prices are LOWER (better) — Polymarket CLOB fills at or below limit
- Use `client.get_trades()` with API creds to see real fills
- `_sync_balance` corrects bankroll drift vs on-chain wallet every 10 rounds
- Skip sync while `_pending_redeems` exists (timing fix: sync AFTER redeems settle)
- `initial_deposit` in bot_state: set once at first start, never overwritten

## Token Redemption — CRITICAL LESSONS
- **NEVER manually call `redeem_positions` with guessed index_sets**
  - Passing wrong index_sets BURNS tokens without paying out USDC.e
  - Once burned, tokens are GONE — irreversible on-chain
  - The bot's auto-redeem in engine.py uses the correct index_sets automatically
- Binary market index_sets: Up=outcome 0, Down=outcome 1
  - indexSet 1 (binary 01) = outcome 0 (Up)
  - indexSet 2 (binary 10) = outcome 1 (Down)
  - indexSet 3 (binary 11) = both outcomes (complete set)
- Always verify payout status BEFORE redeeming: check `payoutNumerators` to confirm which outcome won
- `scan_redeemable_tokens()` only scans known condition_ids from DB trades
- Cost of mistake: lost $3.97 in trade 286 tokens by incorrect manual redeem

## Investigation Methodology (for future "missing money" audits)
1. **Start with Polymarket API**: `client.get_trades()` shows ALL real trades, fills, and fees
2. **Cross-reference timestamps**: PM trades have `match_time`, DB has `created_at`
3. **Check for phantom trades**: "dry_run" in DB ≠ no real trades on PM (bot may have been live before config change)
4. **Count from on-chain**: `get_eoa_usdc_balance()` + `scan_redeemable_tokens()` = real total
5. **Verify resolution**: `is_condition_resolved()` + `payoutNumerators` confirm which side won
6. **Fee audit**: fee_rate_bps in PM trade data shows actual fee, don't assume 2%
7. **Cash flow tracing**: total_spent(buys×1.10) - total_received(sells×0.90) + redeems = expected wallet delta

## Resolution Bug (2026-03-04) — CRITICAL FIX
- **Old fallback**: `get_token_balance(token_id) > 0` → mark as WIN. WRONG! Tokens exist until redeemed, regardless of win/loss
- **Result**: 6 of 11 March 4 trades marked as WIN when they actually LOST
- **Fix**: Use `get_winning_outcome(condition_id)` which calls `payoutNumerators(conditionId, outcomeIndex)` on CTF contract
- Binary markets: outcomeIndex 0 = Up, outcomeIndex 1 = Down. If payoutNumerators > 0 for that index, that outcome won
- `get_winning_outcome()` added to `bot/wallet.py`, returns "up" or "down" or None if unresolved
- **Never use token balance to determine win/loss** — only payoutNumerators or CLOB API `winner` field

## Money Safety Fixes (2026-03-04)
- **Hard daily loss stop**: Added check #2 in `check_risk()` — blocks trading if `|daily_pnl| >= bankroll * daily_loss_limit_pct`
- **No more price guess**: `_check_resolution_with_retry()` returns `None` instead of guessing from BTC price movement. Trade stays PENDING for next recovery
- **No more token balance fallback**: `_recover_pending_trades()` uses `get_winning_outcome()` on-chain. If unresolved → `continue` (PENDING)
- **Pre-order DB INSERT**: Live orders now INSERT with `order_status='pending_fill'` BEFORE `post_order()`. If order fails/times out, row is deleted. Crash-safe
- **Fill price verification**: After GTC fill, reads `associate_trades` from order response to get real fill price/size
- **EOA balance primary**: `fetch_wallet_balance()` uses on-chain `get_eoa_usdc_balance()` first, CLOB exchange as fallback
- **Broader token scan**: `scan_redeemable_tokens()` scans ALL trades with condition_id, not just wins. Checks `is_condition_resolved()` before adding
- **Recovery of pending_fill**: `recover_pending_fills()` runs on startup, checks CLOB order status, marks filled or deletes

## Common Pitfalls
- Bankroll tracking can diverge from wallet if trades happen outside the DB window (e.g., pre-reset trades)
- `daily_pnl` is adjusted by `_sync_balance` reconciliation — can go negative even with all-win streak
- Equity curve `bankroll_after` must be scaled to match wallet reality when there are external trades
- "Win rate 92%" with negative P&L is possible when fees eat profits or external factors reduce wallet
- **PM API `match_time` is Unix timestamp (seconds)**, not ISO string — convert with `datetime.fromtimestamp(ts, tz=timezone.utc)`
