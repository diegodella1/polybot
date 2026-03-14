"""Main trading engine loop: discover → signal → risk → size → execute → resolve."""

import asyncio
import logging
import time
from datetime import datetime, timezone

import numpy as np

from bot.config import get
from bot.state import state
from bot.fair_value import estimate_fair_value, find_edge, TAKER_FEE
from bot.risk import check_risk, record_outcome, decrement_cooldown, reset_daily
from bot.sizing import kelly_size
from bot.executor import execute_trade, exit_position, resolve_trade, update_daily_stats, recover_pending_fills
from bot.market_discovery import discover_market
from rag.reflection import maybe_reflect
from data.binance_ws import BinanceWS
from data.buffer import PriceBuffer
from data.polymarket_ws import PolymarketWS, fetch_orderbook_snapshot
from db import get_db

logger = logging.getLogger(__name__)

# Max seconds to wait for resolution before giving up
MAX_RESOLUTION_WAIT = 120


class TradingEngine:
    def __init__(self):
        self.price_buffer = PriceBuffer(max_size=100)
        self.binance_ws = BinanceWS(on_kline=self._on_kline)
        self.polymarket_ws = PolymarketWS()
        self._running = False
        self._round = 0

        # Callbacks for WebSocket push to dashboard
        self.on_trade = None       # async callback(trade_data)
        self.on_signal = None      # async callback(signal_data)
        self.on_status = None      # async callback(status_data)
        self.on_round_update = None  # async callback(round_data)

        self.pattern_store = None  # Set by main.py

        # Current market time remaining (used by _build_features for RAG)
        self._market_seconds_remaining = 150.0  # Default: midpoint of 5min

        # Cache last round's signals for consistent dashboard display
        self._last_signals = None

        # Wallet balance cache (updated by _sync_balance)
        self._cached_wallet_balance = None
        self._cached_unredeemed = 0.0

        # Retry queue for failed auto-redeems
        self._pending_redeems: list[dict] = []

    async def _on_kline(self, kline):
        self.price_buffer.update(kline)

    async def start(self):
        """Start the engine: data feeds + trading loop."""
        self._running = True
        await state.set("enabled", True)

        bankroll = await state.get("bankroll")
        if bankroll is None:
            import os
            bankroll = float(os.environ.get("INITIAL_BANKROLL", "50.0"))
            await state.set("bankroll", bankroll)

        # Track initial deposit (set once, never overwritten)
        if await state.get("initial_deposit") is None:
            await state.set("initial_deposit", bankroll)

        logger.info("Engine starting with bankroll: $%.2f", bankroll)

        # Crash recovery: resolve any pending trades from previous run
        await self._recover_pending_trades(bankroll)

        # Start data feeds
        asyncio.create_task(self.binance_ws.start())
        asyncio.create_task(self.polymarket_ws.start())

        # Wait for initial data
        logger.info("Waiting for price data...")
        for _ in range(30):
            if self.price_buffer.size >= 5:
                break
            await asyncio.sleep(1)

        if self.price_buffer.size < 5:
            logger.warning("Limited price data after 30s, proceeding anyway")

        # Main loop
        await self._trading_loop()

    async def stop(self):
        """Stop the engine gracefully."""
        self._running = False
        await state.set("enabled", False)
        await self.binance_ws.stop()
        await self.polymarket_ws.stop()
        logger.info("Engine stopped")
        if self.on_status:
            await self.on_status({"status": "stopped"})

    async def _recover_pending_trades(self, bankroll: float):
        """On startup, find trades with outcome IS NULL and resolve them."""
        # First: recover any trades stuck in pending_fill (crash during order placement)
        await recover_pending_fills()

        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT id, condition_id, side, size_usd, token_id
                   FROM trades WHERE outcome IS NULL
                   AND (order_status IS NULL OR order_status = 'filled')
                   ORDER BY id ASC"""
            )
            pending = await cursor.fetchall()
        finally:
            await db.close()

        if not pending:
            return

        logger.warning("Crash recovery: found %d pending trades", len(pending))

        for trade in pending:
            trade_id = trade["id"]
            condition_id = trade["condition_id"]
            side = trade["side"]

            # Try to get actual resolution from CLOB API
            won = None
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"https://clob.polymarket.com/markets/{condition_id}"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("closed"):
                        token_id_str = trade["token_id"]
                        for tok in data.get("tokens", []):
                            if tok.get("token_id") == token_id_str:
                                won = tok.get("winner", False)
                                break
                        if won is None:
                            # Fallback: match by outcome name
                            for tok in data.get("tokens", []):
                                outcome = (tok.get("outcome") or "").lower()
                                if (side == "up" and outcome in ("up", "yes")) or \
                                   (side == "down" and outcome in ("down", "no")):
                                    won = tok.get("winner", False)
                                    break
            except Exception as e:
                logger.warning("Could not check resolution for trade %d: %s", trade_id, e)

            if won is None:
                # CLOB API failed — try on-chain payoutNumerators with retries
                from bot.wallet import get_winning_outcome
                for oc_attempt in range(3):
                    try:
                        winning_side = get_winning_outcome(condition_id)
                        if winning_side is not None:
                            won = (winning_side == side)
                            logger.info(
                                "Trade %d: on-chain resolution %s won, our side=%s → %s",
                                trade_id, winning_side.upper(), side.upper(),
                                "WIN" if won else "LOSS",
                            )
                            break
                        else:
                            if oc_attempt < 2:
                                await asyncio.sleep(10)
                    except Exception as e:
                        logger.warning("Trade %d: on-chain attempt %d failed (%s)", trade_id, oc_attempt + 1, e)
                        if oc_attempt < 2:
                            await asyncio.sleep(5)
                if won is None:
                    logger.info("Trade %d: not yet resolved, leaving PENDING", trade_id)
                    continue

            new_bankroll = await resolve_trade(trade_id, won, bankroll)
            pnl = new_bankroll - bankroll
            await record_outcome(won, pnl, new_bankroll)
            await update_daily_stats(pnl, won, new_bankroll)
            bankroll = new_bankroll
            logger.info("Recovered trade %d: %s, pnl=$%.2f", trade_id, "WIN" if won else "LOSS", pnl)

            # Auto-redeem winning tokens on recovery
            if won and not get("dry_run", True):
                try:
                    from bot.wallet import redeem_positions
                    index_set = 1 if side == "up" else 2
                    r = await redeem_positions(condition_id, [index_set])
                    if r.success:
                        logger.info("Auto-redeemed recovered trade %d: tx=%s", trade_id, r.tx_hash)
                    else:
                        logger.warning("Auto-redeem failed for trade %d: %s — queued for retry", trade_id, r.error)
                        self._pending_redeems.append({
                            "condition_id": condition_id,
                            "index_set": index_set,
                            "retries": 0,
                        })
                except Exception as e:
                    logger.warning("Auto-redeem error for trade %d: %s — queued for retry", trade_id, e)
                    index_set = 1 if side == "up" else 2
                    self._pending_redeems.append({
                        "condition_id": condition_id,
                        "index_set": index_set,
                        "retries": 0,
                    })

        # Clear position state after recovery
        await state.set("has_open_position", False)
        await state.set("current_exposure", 0.0)

    async def _trading_loop(self):
        """Main loop: runs once per round (~every 30 seconds polling)."""
        while self._running:
            try:
                enabled = await state.get("enabled", True)
                if not enabled:
                    await asyncio.sleep(5)
                    continue

                # Check for daily reset
                await self._check_daily_reset()

                # Retry failed redeems every 5 rounds
                if self._round % 5 == 0 and self._pending_redeems:
                    await self._process_pending_redeems()

                # Resolve paper trades every 12 rounds (~1 min)
                if self._round % 12 == 3:
                    await self._resolve_paper_trades()

                # Resolve any pending trades every 12 rounds (~1 min)
                if self._round % 12 == 6:
                    bankroll = await state.get("bankroll", 0.0)
                    await self._recover_pending_trades(bankroll)

                # Sync wallet balance every 10 rounds (~5 min)
                # Skip if there are pending redeems (wait for tokens → USDC.e)
                if self._round % 10 == 0 and not self._pending_redeems:
                    await self._sync_balance()

                self._round += 1
                await self._run_round()

            except Exception as e:
                logger.error("Round error: %s", e, exc_info=True)

            # Poll every 5 seconds for new markets
            await asyncio.sleep(5)

    async def _broadcast_round(self, decision: str, reason: str,
                               signal: float | None = None,
                               market_name: str = ""):
        """Broadcast round update to dashboard."""
        if not self.on_round_update:
            return
        await self.on_round_update({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": market_name,
            "signal": signal,
            "decision": decision,
            "reason": reason,
        })

    def _risk_reason_es(self, reason: str) -> str:
        """Translate risk.reason to Spanish for the dashboard."""
        r = reason.lower()
        if "below floor" in r:
            return "Riesgo: bankroll debajo del mínimo de emergencia"
        if "trade cooldown" in r:
            import re
            m = re.search(r"(\d+m\d+s)", reason)
            remaining = m.group(1) if m else "?"
            return f"Cooldown entre trades ({remaining})"
        if "cooldown" in r:
            import re
            m = re.search(r"(\d+) rounds", reason)
            rounds = m.group(1) if m else "?"
            return f"Riesgo: cooldown activo ({rounds} rounds)"
        if "circuit breaker" in r:
            return "Riesgo: circuit breaker activado"
        if "exposure" in r:
            return "Riesgo: exposición máxima alcanzada"
        if "position already" in r:
            return "Posición abierta, esperando resolución"
        return f"Riesgo: {reason}"

    async def _run_round(self):
        """Execute one trading round."""
        # 1. DISCOVER
        market = await discover_market()
        if market is None:
            logger.debug("Round %d: no market found", self._round)
            await self._broadcast_round("wait", "Sin mercado activo en este momento")
            # Don't decrement cooldown when no market exists
            return

        market_name = market.question or ""

        # Only trade inside the market's time window
        if not market.is_in_window:
            mins = market.seconds_until_start / 60
            logger.debug(
                "Round %d: market not in window yet (starts in %.0fs)",
                self._round,
                market.seconds_until_start,
            )
            await self._broadcast_round(
                "wait",
                f"Mercado no abrió todavía (faltan {mins:.0f}min)",
                market_name=market_name,
            )
            return

        # Decrement cooldown only when a market is available (meaningful round)
        await decrement_cooldown()

        # Update time remaining for RAG feature vector
        self._market_seconds_remaining = market.seconds_remaining

        min_time = get("min_time_remaining_sec", 30)
        if market.seconds_remaining < min_time:
            logger.debug(
                "Round %d: %.0fs remaining < %ds minimum",
                self._round,
                market.seconds_remaining,
                min_time,
            )
            await self._broadcast_round(
                "skip",
                f"Poco tiempo restante ({market.seconds_remaining:.0f}s < {min_time}s)",
                market_name=market_name,
            )
            return

        # Late-entry strategy: wait for trend to develop before entering
        max_time = get("max_time_remaining_sec", 0)
        if max_time > 0 and market.seconds_remaining > max_time:
            logger.info(
                "Round %d: %.0fs remaining > %ds max (waiting for trend)",
                self._round,
                market.seconds_remaining,
                max_time,
            )
            await self._broadcast_round(
                "wait",
                f"Esperando tendencia ({market.seconds_remaining:.0f}s > {max_time}s)",
                market_name=market_name,
            )
            return

        # Check data freshness
        if self.price_buffer.is_stale:
            logger.warning("Round %d: price data is stale, skipping", self._round)
            await self._broadcast_round(
                "skip", "Datos de precio insuficientes",
                market_name=market_name,
            )
            return

        # Subscribe to orderbook for this market's Up token
        await self.polymarket_ws.subscribe(market.token_up_id)
        await asyncio.sleep(1)  # Wait for orderbook snapshot

        # REST fallback if WS orderbook is empty
        ob = self.polymarket_ws.orderbook
        if not ob.bids and not ob.asks:
            await self.polymarket_ws.fetch_orderbook_rest(market.token_up_id)

        # 2. FAIR VALUE SIGNAL
        fv = estimate_fair_value(self.price_buffer)
        if fv is None:
            logger.debug("Round %d: fair value not ready", self._round)
            await self._broadcast_round(
                "skip", "Fair value no listo (pocos datos)",
                market_name=market_name,
            )
            return

        # Vol regime filter: skip low vol (no signal) and very high vol (chaos)
        min_vol = get("min_vol_5m", 0.0005)
        max_vol = get("max_vol_5m", 0.004)
        if fv.vol_5m < min_vol:
            await self._broadcast_round(
                "skip", f"Vol muy baja ({fv.vol_5m*100:.4f}% < {min_vol*100:.2f}%)",
                market_name=market_name,
            )
            return
        if fv.vol_5m > max_vol:
            await self._broadcast_round(
                "skip", f"Vol muy alta ({fv.vol_5m*100:.4f}% > {max_vol*100:.2f}%)",
                market_name=market_name,
            )
            return

        ob_up = self.polymarket_ws.orderbook
        price_up = ob_up.midpoint
        if price_up is None:
            price_up = ob_up.best_ask or ob_up.best_bid
        if price_up is None or price_up <= 0:
            await self._broadcast_round(
                "skip", "Sin precio de mercado",
                market_name=market_name,
            )
            return

        # Fetch Down token orderbook independently (no WS mutation)
        ob_down = await fetch_orderbook_snapshot(market.token_down_id)
        if ob_down.midpoint is not None:
            price_down = ob_down.midpoint
        else:
            price_down = 1.0 - price_up  # fallback

        edge_result = find_edge(fv, price_up, price_down)

        # Cache for dashboard
        self._last_signals = {
            "prob_up": round(fv.prob_up, 4),
            "prob_down": round(fv.prob_down, 4),
            "vol_5m": round(fv.vol_5m, 6),
            "drift_5m": round(fv.drift_5m, 6),
            "price_up": round(price_up, 4),
            "price_down": round(price_down, 4),
            "edge": round(edge_result.edge, 4),
            "side": edge_result.side,
            "has_edge": edge_result.has_edge,
        }

        if self.on_signal:
            await self.on_signal(self._last_signals)

        logger.info(
            "Round %d | FV P(up)=%.1f%% mkt=%.1f%% | P(dn)=%.1f%% mkt=%.1f%% | "
            "vol=%.4f%% drift=%.4f%% | edge=%s %.1f%%",
            self._round,
            fv.prob_up * 100, price_up * 100,
            fv.prob_down * 100, price_down * 100,
            fv.vol_5m * 100, fv.drift_5m * 100,
            edge_result.side or "none",
            edge_result.edge * 100,
        )

        # Record paper trade (always, for tracking)
        await self._fair_value_paper_round(market, fv, price_up, price_down, edge_result)

        if not edge_result.has_edge:
            await self._broadcast_round(
                "skip",
                f"Sin edge ({edge_result.edge*100:.1f}%)",
                market_name=market_name,
            )
            return

        # Edge band filter: best WR in 6-12% bucket (71%), 12-20% still +EV (57%)
        min_edge = get("min_edge", 0.03)
        max_edge = get("max_edge", 0.12)
        if edge_result.edge < min_edge:
            await self._broadcast_round(
                "skip",
                f"Edge muy bajo ({edge_result.edge*100:.1f}% < {min_edge*100:.0f}%)",
                market_name=market_name,
            )
            return
        if edge_result.edge > max_edge:
            await self._broadcast_round(
                "skip",
                f"Edge excesivo ({edge_result.edge*100:.1f}% > {max_edge*100:.0f}%) — modelo sobreconfiado",
                market_name=market_name,
            )
            return

        side = edge_result.side

        # 3. RISK CHECK
        bankroll = await state.get("bankroll", 50.0)
        risk = await check_risk(edge_result.edge, bankroll)
        if not risk:
            logger.info(
                "Round %d: FV edge %.1f%% %s → SKIP: %s",
                self._round, edge_result.edge * 100, side.upper(), risk.reason,
            )
            await self._broadcast_round(
                "skip",
                self._risk_reason_es(risk.reason),
                market_name=market_name,
            )
            return

        # 4. SIZE — get actual entry price from orderbook
        if side == "up":
            token_id = market.token_up_id
            entry_price = ob_up.best_ask or price_up
            trade_spread = ob_up.spread
        else:
            token_id = market.token_down_id
            trade_spread = ob_down.spread
            if ob_down.best_ask is not None and 0.05 < ob_down.best_ask < 0.95:
                entry_price = ob_down.best_ask
            elif ob_up.best_bid is not None and 0.05 < ob_up.best_bid < 0.95:
                # Derive from Up bid (no re-subscribe needed)
                entry_price = 1.0 - ob_up.best_bid
            else:
                logger.warning("Round %d: No Down pricing available, skipping", self._round)
                await self._broadcast_round(
                    "skip", "Sin precio para Down token",
                    market_name=market_name,
                )
                return

        # Entry price filter
        min_ep = get("min_entry_price", 0.25)
        max_ep = get("max_entry_price", 0.75)
        if entry_price < min_ep or entry_price > max_ep:
            logger.info(
                "Round %d: FV %s → SKIP: price %.2f outside [%.2f, %.2f]",
                self._round, side.upper(), entry_price, min_ep, max_ep,
            )
            await self._broadcast_round(
                "skip",
                f"Precio {entry_price:.2f} fuera de rango [{min_ep}, {max_ep}]",
                market_name=market_name,
            )
            return

        # Spread check
        max_spread = get("max_spread_cents", 5) / 100.0
        if trade_spread is not None and trade_spread > max_spread:
            await self._broadcast_round(
                "skip",
                f"Spread amplio ({trade_spread*100:.1f}¢ > {max_spread*100:.0f}¢)",
                market_name=market_name,
            )
            return

        daily_pnl = await state.get("daily_pnl", 0.0)
        sizing = kelly_size(edge_result.edge, entry_price, bankroll, daily_pnl=daily_pnl)
        if sizing["size_usd"] <= 0:
            reason_en = sizing.get("reason", "no edge per Kelly")
            reason_es = "Sin edge según Kelly" if "spread" not in reason_en.lower() else "Spread muy amplio"
            await self._broadcast_round("skip", reason_es, market_name=market_name)
            return

        # Max 1 trade per market
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) as c FROM trades WHERE condition_id = ? AND outcome IS NULL",
                (market.condition_id,),
            )
            row = await cursor.fetchone()
            pending_in_market = row["c"] if row else 0
        finally:
            await db.close()
        if pending_in_market >= 1:
            await self._broadcast_round(
                "skip", "Ya hay trade en este mercado",
                market_name=market_name,
            )
            return

        # 5. EXECUTE
        signal_details = {
            "model": "fair_value",
            "prob_estimated": edge_result.prob,
            "edge": edge_result.edge,
            "vol_5m": fv.vol_5m,
            "drift_5m": fv.drift_5m,
        }
        result = await execute_trade(
            condition_id=market.condition_id,
            token_id=token_id,
            side=side,
            signal_score=edge_result.edge,
            size_usd=sizing["size_usd"],
            shares=sizing["shares"],
            entry_price=entry_price,
            signal_details=signal_details,
            spread=trade_spread,
            btc_price=self.price_buffer.current_price,
        )

        if not result.success:
            logger.warning("Round %d: execution failed — %s", self._round, result.error)
            return

        await state.set("last_trade_timestamp", time.time())

        side_label = side.upper()
        logger.info(
            "FV TRADE: %s @ %.2f¢ (edge=%.1f%%, prob=%.1f%%), size $%.2f",
            side_label, entry_price * 100, edge_result.edge * 100,
            edge_result.prob * 100, sizing["size_usd"],
        )
        await self._broadcast_round(
            "trade",
            f"Trade: {side_label} ${sizing['size_usd']:.2f} (edge={edge_result.edge*100:.1f}%)",
            market_name=market_name,
        )

        trade_data = {
            "trade_id": result.trade_id,
            "side": side,
            "edge": edge_result.edge,
            "prob": edge_result.prob,
            "size_usd": sizing["size_usd"],
            "entry_price": entry_price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": market.question,
        }
        if self.on_trade:
            await self.on_trade(trade_data)

        # 6. RESOLVE in background
        asyncio.create_task(self._monitor_and_resolve(
            market=market,
            side=side,
            token_id=token_id,
            result=result,
            bankroll=bankroll,
            edge=edge_result.edge,
            market_name=market_name,
        ))

    async def _monitor_and_resolve(self, market, side, token_id, result,
                                    bankroll, edge, market_name):
        """Background task: monitor position for SL/TP, then resolve."""
        try:
            wait_seconds = min(
                max(0, market.seconds_remaining + 10),
                MAX_RESOLUTION_WAIT,
            )

            stop_loss_pct = get("stop_loss_pct", 0.08)
            take_profit_pct = get("take_profit_pct", 0.12)
            stop_price = result.filled_price * (1 - stop_loss_pct)
            take_profit_price = result.filled_price * (1 + take_profit_pct)
            early_exit = None
            exit_proceeds = None
            exit_failed = False

            logger.info(
                "Monitoring %.0fs | entry=%.2f¢ stop=%.2f¢ (-%d%%) tp=%.2f¢ (+%d%%)",
                wait_seconds, result.filled_price * 100, stop_price * 100,
                int(stop_loss_pct * 100), take_profit_price * 100,
                int(take_profit_pct * 100),
            )

            elapsed = 0
            while elapsed < wait_seconds:
                await asyncio.sleep(5)
                elapsed += 5

                await self.polymarket_ws.subscribe(token_id)
                await asyncio.sleep(1)
                elapsed += 1

                bid = self.polymarket_ws.orderbook.best_bid

                if bid is not None and self.on_trade:
                    unrealized_pnl = (bid - result.filled_price) * result.shares
                    unrealized_pct = ((bid / result.filled_price) - 1) * 100 if result.filled_price > 0 else 0
                    await self.on_trade({
                        "event_type": "position_update",
                        "trade_id": result.trade_id,
                        "current_bid": bid,
                        "entry_price": result.filled_price,
                        "unrealized_pnl": round(unrealized_pnl, 2),
                        "unrealized_pct": round(unrealized_pct, 1),
                        "elapsed": elapsed,
                        "wait_seconds": wait_seconds,
                    })

                if exit_failed or not get("use_tp_sl", True):
                    continue

                if bid is not None:
                    logger.debug(
                        "Monitor: bid=%.2f¢ stop=%.2f¢ tp=%.2f¢",
                        bid * 100, stop_price * 100, take_profit_price * 100,
                    )
                    if bid <= stop_price:
                        logger.warning(
                            "STOP-LOSS triggered: bid=%.2f¢ <= stop=%.2f¢",
                            bid * 100, stop_price * 100,
                        )
                        exit_result = await exit_position(token_id, result.shares, bid)
                        if exit_result["success"]:
                            early_exit = "stop_loss"
                            exit_proceeds = exit_result["proceeds"]
                            break
                        else:
                            logger.warning("SL exit failed, holding to resolution")
                            exit_failed = True
                    elif bid >= take_profit_price:
                        logger.info(
                            "TAKE-PROFIT triggered: bid=%.2f¢ >= tp=%.2f¢",
                            bid * 100, take_profit_price * 100,
                        )
                        exit_result = await exit_position(token_id, result.shares, bid)
                        if exit_result["success"]:
                            early_exit = "take_profit"
                            exit_proceeds = exit_result["proceeds"]
                            break
                        else:
                            logger.warning("TP exit failed, holding to resolution")
                            exit_failed = True

            if early_exit:
                new_bankroll = await resolve_trade(
                    result.trade_id, won=False, bankroll=bankroll,
                    exit_proceeds=exit_proceeds,
                    exit_type=early_exit,
                )
                pnl = new_bankroll - bankroll
                outcome_label = early_exit

                if early_exit == "stop_loss":
                    loss_pct = abs(pnl) / result.filled_size * 100 if result.filled_size > 0 else 0
                    await self._broadcast_round(
                        "stop_loss",
                        f"Stop-loss: vendido a {exit_result['exit_price'] * 100:.0f}¢ (-{loss_pct:.0f}%)",
                        signal=edge,
                        market_name=market_name,
                    )
                else:
                    gain_pct = pnl / result.filled_size * 100 if result.filled_size > 0 else 0
                    await self._broadcast_round(
                        "take_profit",
                        f"Take-profit: vendido a {exit_result['exit_price'] * 100:.0f}¢ (+{gain_pct:.0f}%)",
                        signal=edge,
                        market_name=market_name,
                    )
            else:
                won = await self._check_resolution_with_retry(market, side)
                if won is None:
                    logger.warning("Trade %d: resolution unknown, leaving PENDING", result.trade_id)
                    await state.set("has_open_position", False)
                    await state.set("current_exposure", 0.0)
                    await self._broadcast_round(
                        "skip",
                        "Resolución no disponible, trade queda PENDING",
                        signal=edge,
                        market_name=market_name,
                    )
                    return
                new_bankroll = await resolve_trade(result.trade_id, won, bankroll)
                pnl = new_bankroll - bankroll
                outcome_label = "win" if won else "loss"

            # Auto-redeem winning tokens on-chain
            if outcome_label == "win" and not get("dry_run", True):
                index_set = 1 if side == "up" else 2
                try:
                    from bot.wallet import redeem_positions
                    r = await redeem_positions(market.condition_id, [index_set])
                    if r.success:
                        logger.info("Auto-redeemed: tx=%s", r.tx_hash)
                        await self._sync_balance()
                    else:
                        logger.warning("Auto-redeem failed: %s — queued for retry", r.error)
                        self._pending_redeems.append({
                            "condition_id": market.condition_id,
                            "index_set": index_set,
                            "retries": 0,
                        })
                except Exception as e:
                    logger.warning("Auto-redeem error: %s — queued for retry", e)
                    self._pending_redeems.append({
                        "condition_id": market.condition_id,
                        "index_set": index_set,
                        "retries": 0,
                    })

            # Update risk state
            won_for_stats = outcome_label in ("win", "take_profit")
            await record_outcome(won_for_stats, pnl, new_bankroll)
            await update_daily_stats(pnl, won_for_stats, new_bankroll)

            # Store RAG pattern
            if self.pattern_store is not None:
                try:
                    features = self._build_features()
                    if features is not None:
                        rag_outcome = "win" if won_for_stats else "loss"
                        await self.pattern_store.store(result.trade_id, features, rag_outcome)
                        logger.info("Stored RAG pattern for trade %d (%s)", result.trade_id, rag_outcome)
                except Exception as e:
                    logger.warning("Failed to store RAG pattern: %s", e)

            # Notify dashboard
            resolve_data = {
                "trade_id": result.trade_id,
                "outcome": outcome_label,
                "pnl": pnl,
                "bankroll": new_bankroll,
            }
            if self.on_trade:
                await self.on_trade(resolve_data)

            logger.info(
                "Resolved: %s pnl=$%.2f bankroll=$%.2f",
                outcome_label.upper(), pnl, new_bankroll,
            )

            # Periodic reflection (every 50 trades, passive analysis)
            try:
                from db import get_db
                db = await get_db()
                try:
                    cursor = await db.execute("SELECT COUNT(*) c FROM trades WHERE outcome IS NOT NULL")
                    total_trades = (await cursor.fetchone())["c"]
                finally:
                    await db.close()
                await maybe_reflect(total_trades)
            except Exception as e:
                logger.debug("Reflection check skipped: %s", e)

        except Exception as e:
            logger.error("Background resolution error for trade %d: %s", result.trade_id, e, exc_info=True)
            await state.set("has_open_position", False)
            await state.set("current_exposure", 0.0)

    def _build_features(self) -> np.ndarray | None:
        """Build 8-float feature vector from current price buffer state.

        Feature 8 is time_in_candle (normalized 0-1: how much time remains in market).
        """
        snap = self.price_buffer.snapshot()
        if snap is None:
            return None
        price = snap["price"]
        if price is None or price == 0:
            return None

        def safe(v):
            return v if v is not None else 0.0

        time_in_candle = max(0.0, min(1.0, self._market_seconds_remaining / 300.0))

        features = [
            safe(snap["ret_1m"]),
            safe(snap["ret_5m"]),
            safe(snap["ret_15m"]),
            (safe(snap["ema_fast"]) / price) - 1,
            (safe(snap["ema_slow"]) / price) - 1,
            safe(snap["vol_ratio"]),
            safe(snap["rsi_14"]) / 100.0,
            time_in_candle,
        ]
        return np.array(features, dtype=np.float32)

    async def _check_resolution_with_retry(self, market, side: str) -> bool | None:
        """Check if our trade won or lost, with retries for live mode.

        Queries the market resolution. In dry_run, simulates based on price movement.
        """
        dry_run = get("dry_run", True)

        if dry_run:
            # Simulate: use actual BTC price movement
            ret = self.price_buffer.ret(5)
            if ret is None:
                # 50/50 if no data
                import random
                return random.random() > 0.5
            btc_went_up = ret > 0
            if side == "up":
                return btc_went_up
            else:
                return not btc_went_up

        # Real resolution: query CLOB API with retry
        import httpx
        token_id = market.token_up_id if side == "up" else market.token_down_id
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"https://clob.polymarket.com/markets/{market.condition_id}"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("closed"):
                        # Find our token in the response
                        for tok in data.get("tokens", []):
                            if tok.get("token_id") == token_id:
                                won = tok.get("winner", False)
                                logger.info("Resolution via CLOB: %s (token matched)", "WIN" if won else "LOSS")
                                return won
                        # Fallback: match by outcome name
                        for tok in data.get("tokens", []):
                            outcome = (tok.get("outcome") or "").lower()
                            if (side == "up" and outcome in ("up", "yes")) or \
                               (side == "down" and outcome in ("down", "no")):
                                won = tok.get("winner", False)
                                logger.info("Resolution via CLOB: %s (outcome matched)", "WIN" if won else "LOSS")
                                return won
                        logger.warning("Market closed but couldn't match token/outcome")
                    else:
                        logger.info("Market not yet closed (attempt %d/4), waiting 10s...", attempt + 1)
                    await asyncio.sleep(10)
            except Exception as e:
                logger.warning("Resolution check attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(5)

        # All CLOB retries exhausted — fallback to on-chain with retries
        # 5-min markets can take up to ~2 min to resolve on-chain
        logger.warning("CLOB resolution not available after 4 retries, trying on-chain")
        from bot.wallet import get_winning_outcome
        for on_chain_attempt in range(8):
            try:
                winning_side = get_winning_outcome(market.condition_id)
                if winning_side is not None:
                    won = (winning_side == side)
                    logger.info(
                        "On-chain fallback: %s won, our side=%s → %s",
                        winning_side.upper(), side.upper(), "WIN" if won else "LOSS",
                    )
                    return won
                else:
                    logger.info("On-chain attempt %d/8: not yet resolved, waiting 15s...", on_chain_attempt + 1)
                    await asyncio.sleep(15)
            except Exception as e:
                logger.warning("On-chain attempt %d/8 failed: %s", on_chain_attempt + 1, e)
                await asyncio.sleep(5)

        # All resolution methods exhausted — return None to leave trade PENDING
        logger.warning("All resolution methods failed, leaving trade PENDING for next recovery")
        return None

    async def _sync_balance(self):
        """Reconcile internal bankroll with on-chain USDC balance.

        If drift exceeds $0.50, update bankroll to match reality.
        Caches wallet balance for dashboard display.
        """
        dry_run = get("dry_run", True)
        if dry_run:
            return
        try:
            from bot.executor import fetch_wallet_balance
            wallet_balance = await asyncio.wait_for(
                asyncio.to_thread(fetch_wallet_balance),
                timeout=15,
            )
            if wallet_balance is None:
                return

            # Cache unredeemed tokens for dashboard display (separate from bankroll)
            unredeemed_value = 0.0
            try:
                from bot.wallet import scan_redeemable_tokens
                tokens = await asyncio.wait_for(
                    asyncio.to_thread(scan_redeemable_tokens),
                    timeout=15,
                )
                unredeemed_value = sum(t.get("balance", 0) for t in tokens)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("Unredeemed token scan failed/timed out: %s", e)

            self._cached_wallet_balance = wallet_balance
            self._cached_unredeemed = unredeemed_value

            # Bankroll = on-chain USDC.e only (liquid, tradeable funds)
            # Unredeemed tokens are value but NOT available for trading
            bankroll = await state.get("bankroll", 0.0)
            drift = abs(wallet_balance - bankroll)

            if drift > 0.50:
                logger.warning(
                    "Balance drift: USDC.e=$%.2f vs bankroll=$%.2f (drift=$%.2f). "
                    "Adjusting bankroll to on-chain. Unredeemed=$%.2f (not included)",
                    wallet_balance, bankroll, drift, unredeemed_value,
                )
                await state.set("bankroll", wallet_balance)
            else:
                logger.info(
                    "Balance sync OK: USDC.e=$%.2f bankroll=$%.2f (drift=$%.2f) unredeemed=$%.2f",
                    wallet_balance, bankroll, drift, unredeemed_value,
                )
        except Exception as e:
            logger.warning("Balance sync failed: %s", e)

    async def _process_pending_redeems(self):
        """Retry failed auto-redeems. Max 5 retries per item."""
        if not self._pending_redeems:
            return

        from bot.wallet import redeem_positions

        still_pending = []
        redeemed_any = False
        for item in self._pending_redeems:
            item["retries"] += 1
            if item["retries"] > 5:
                logger.warning(
                    "Giving up on redeem for condition %s after 5 retries",
                    item["condition_id"][:16],
                )
                continue
            try:
                r = await redeem_positions(item["condition_id"], [item["index_set"]])
                if r.success:
                    logger.info(
                        "Retry redeem OK: condition=%s tx=%s",
                        item["condition_id"][:16], r.tx_hash,
                    )
                    redeemed_any = True
                else:
                    logger.warning(
                        "Retry redeem failed (attempt %d): %s",
                        item["retries"], r.error,
                    )
                    still_pending.append(item)
            except Exception as e:
                logger.warning("Retry redeem error (attempt %d): %s", item["retries"], e)
                still_pending.append(item)

        self._pending_redeems = still_pending

        # Sync balance after successful redeems to capture the returned USDC.e
        if redeemed_any:
            await self._sync_balance()

    async def _check_daily_reset(self):
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        last_reset = await state.get("daily_reset_date", "")
        if last_reset != today:
            await reset_daily()

    async def _fair_value_paper_round(self, market, fv, price_up, price_down, edge_result):
        """Record paper trade if edge is found (for ongoing model validation)."""
        try:
            if not edge_result.has_edge:
                return

            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT COUNT(*) c FROM paper_trades WHERE condition_id = ? AND outcome IS NULL",
                    (market.condition_id,),
                )
                row = await cursor.fetchone()
                if row and row["c"] > 0:
                    return

                btc_price = self.price_buffer.current_price
                await db.execute(
                    """INSERT INTO paper_trades
                       (timestamp, condition_id, side, prob_estimated, market_price,
                        edge, vol_5m, drift_5m, price_up, price_down, btc_price)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        market.condition_id,
                        edge_result.side,
                        edge_result.prob,
                        edge_result.market_price,
                        edge_result.edge,
                        fv.vol_5m,
                        fv.drift_5m,
                        price_up,
                        price_down,
                        btc_price,
                    ),
                )
                await db.commit()

                logger.info(
                    "PAPER TRADE: %s @ %.1f¢ (prob=%.1f%%, edge=+%.1f%%)",
                    edge_result.side.upper(),
                    edge_result.market_price * 100,
                    edge_result.prob * 100,
                    edge_result.edge * 100,
                )
            finally:
                await db.close()

        except Exception as e:
            logger.warning("Fair value paper round error: %s", e)

    async def _resolve_paper_trades(self):
        """Resolve pending paper trades by checking market outcomes."""
        db = await get_db()
        try:
            # Auto-expire paper trades older than 15 min (market should resolve in ~6 min)
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
            expired = await db.execute(
                """UPDATE paper_trades SET outcome = 'expired', pnl_simulated = 0,
                   resolved_at = ? WHERE outcome IS NULL AND timestamp < ?""",
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            if expired.rowcount > 0:
                await db.commit()
                logger.info("Expired %d stale paper trades (>15 min unresolved)", expired.rowcount)

            cursor = await db.execute(
                """SELECT id, condition_id, side, market_price, prob_estimated
                   FROM paper_trades WHERE outcome IS NULL
                   ORDER BY id ASC LIMIT 20"""
            )
            pending = await cursor.fetchall()
        finally:
            await db.close()

        if not pending:
            return

        import httpx
        for pt in pending:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"https://clob.polymarket.com/markets/{pt['condition_id']}"
                    )
                    resp.raise_for_status()
                    data = resp.json()

                if not data.get("closed"):
                    continue

                # Determine winner
                side = pt["side"]
                won = None
                for tok in data.get("tokens", []):
                    outcome_name = (tok.get("outcome") or "").lower()
                    if (side == "up" and outcome_name in ("up", "yes")) or \
                       (side == "down" and outcome_name in ("down", "no")):
                        won = tok.get("winner", False)
                        break

                if won is None:
                    continue

                # Calculate simulated PnL
                # Simulated $1 bet: win = $1/price - $1*(1+fee), loss = -$1*(1+fee)
                entry_cost = 1.0 * (1 + 0.10)  # $1 bet + 10% fee
                if won:
                    pnl = (1.0 / pt["market_price"]) - entry_cost
                    outcome = "win"
                else:
                    pnl = -entry_cost
                    outcome = "loss"

                db = await get_db()
                try:
                    await db.execute(
                        """UPDATE paper_trades SET outcome = ?, pnl_simulated = ?,
                           resolved_at = ? WHERE id = ?""",
                        (outcome, round(pnl, 4),
                         datetime.now(timezone.utc).isoformat(), pt["id"]),
                    )
                    await db.commit()
                finally:
                    await db.close()

                logger.info(
                    "PAPER RESOLVED: #%d %s %s → pnl=$%.3f (prob_est=%.1f%% mkt=%.1f¢)",
                    pt["id"], side.upper(), outcome.upper(), pnl,
                    pt["prob_estimated"] * 100, pt["market_price"] * 100,
                )

            except Exception as e:
                logger.debug("Paper trade resolution error for #%d: %s", pt["id"], e)

    async def get_status(self) -> dict:
        """Get current engine status for API/dashboard."""
        bankroll = await state.get("bankroll", 0)
        daily_pnl = await state.get("daily_pnl", 0)
        enabled = await state.get("enabled", False)
        consec_losses = await state.get("consecutive_losses", 0)
        cooldown = await state.get("cooldown_remaining", 0)

        from bot.executor import has_polymarket_creds
        from bot.risk import _recent_win_rate
        from bot.sizing import _drawdown_multiplier

        # Circuit breaker check
        circuit_breaker = False
        try:
            win_rate = await _recent_win_rate()
            min_wr = get("min_win_rate", 0.30)
            if win_rate is not None and win_rate < min_wr:
                circuit_breaker = True
        except Exception:
            pass

        initial_deposit = await state.get("initial_deposit", bankroll)
        bankroll_open = await state.get("bankroll_open", bankroll)
        daily_fees = await state.get("daily_fees", 0.0)

        return {
            "running": self._running and enabled,
            "round": self._round,
            "bankroll": bankroll,
            "daily_pnl": daily_pnl,
            "initial_deposit": initial_deposit,
            "bankroll_open": bankroll_open,
            "daily_fees": daily_fees,
            "drawdown_multiplier": round(_drawdown_multiplier(daily_pnl, bankroll), 2),
            "consecutive_losses": consec_losses,
            "cooldown_remaining": cooldown,
            "circuit_breaker": circuit_breaker,
            "trade_cooldown_seconds": get("trade_cooldown_seconds", 0),
            "daily_loss_limit_pct": get("daily_loss_limit_pct", 0.15),
            "min_edge": get("min_edge", 0.03),
            "max_edge": get("max_edge", 0.12),
            "price_buffer_size": self.price_buffer.size,
            "current_price": self.price_buffer.current_price,
            "dry_run": get("dry_run", True),
            "has_creds": has_polymarket_creds(),
            "wallet_balance": self._cached_wallet_balance,
            "unredeemed_value": self._cached_unredeemed,
            "signals": self._last_signals,
        }
