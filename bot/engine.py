"""Main trading engine loop: discover → signal → risk → size → execute → resolve."""

import asyncio
import logging
from datetime import datetime, timezone

import numpy as np

from bot.config import get
from bot.state import state
from bot.signals import compute_signal
from bot.risk import check_risk, record_outcome, decrement_cooldown, reset_daily
from bot.sizing import kelly_size
from bot.executor import execute_trade, exit_position, resolve_trade, update_daily_stats
from bot.market_discovery import discover_market
from data.binance_ws import BinanceWS
from data.buffer import PriceBuffer
from data.polymarket_ws import PolymarketWS
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

        # RAG + sentiment (set by main.py)
        self.rag_signal = 0.0
        self.sentiment_signal = 0.0
        self.pattern_store = None  # Set by main.py

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
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT id, condition_id, side, size_usd
                   FROM trades WHERE outcome IS NULL
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

            # Try to get actual resolution from Gamma API
            won = None
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"https://gamma-api.polymarket.com/markets/{condition_id}"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    resolution = (data.get("resolution") or "").lower()
                    if resolution:
                        if side == "up":
                            won = resolution in ("yes", "up", "1")
                        else:
                            won = resolution in ("no", "down", "0")
            except Exception as e:
                logger.warning("Could not check resolution for trade %d: %s", trade_id, e)

            if won is None:
                # Market not yet resolved or API failed — mark as loss (conservative)
                logger.warning("Trade %d: no resolution found, marking as loss (conservative)", trade_id)
                won = False

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
                        logger.warning("Auto-redeem failed for trade %d: %s", trade_id, r.error)
                except Exception as e:
                    logger.warning("Auto-redeem error for trade %d: %s", trade_id, e)

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

                # Sync wallet balance every 10 rounds (~5 min)
                if self._round % 10 == 0:
                    await self._sync_balance()

                self._round += 1
                await self._run_round()

            except Exception as e:
                logger.error("Round error: %s", e, exc_info=True)

            # Poll every 15 seconds for new markets
            await asyncio.sleep(15)

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
        if "daily loss" in r:
            return "Riesgo: límite diario alcanzado"
        if "cooldown" in r:
            # Extract rounds remaining
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

        min_time = get("min_time_remaining_sec", 90)
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

        # 2. SIGNAL
        signals = compute_signal(
            self.price_buffer,
            self.polymarket_ws,
            rag_signal=self.rag_signal,
            sentiment_signal=self.sentiment_signal,
        )

        if self.on_signal:
            await self.on_signal(signals)

        composite = signals["composite"]
        if composite is None:
            logger.debug("Round %d: signals not ready", self._round)
            await self._broadcast_round(
                "skip", "Señales no listas",
                market_name=market_name,
            )
            return

        logger.info(
            "Round %d | signal=%.3f | mom=%.3f skew=%s fv=%s rag=%.3f sent=%.3f",
            self._round,
            composite,
            signals["momentum"] or 0,
            f"{signals['book_skew']:.3f}" if signals["book_skew"] is not None else "N/A",
            f"{signals['fair_value']:.3f}" if signals["fair_value"] is not None else "N/A",
            signals["rag_pattern"],
            signals["sentiment"],
        )

        threshold = get("trade_threshold", 0.15)
        if not signals["tradeable"]:
            logger.debug("Round %d: signal below threshold", self._round)
            await self._broadcast_round(
                "skip",
                f"Señal débil ({abs(composite):.2f} < {threshold} necesario)",
                signal=composite,
                market_name=market_name,
            )
            return

        # 3. RISK CHECK
        bankroll = await state.get("bankroll", 50.0)
        risk = await check_risk(composite, bankroll)
        if not risk:
            logger.info("Round %d: risk check failed — %s", self._round, risk.reason)
            await self._broadcast_round(
                "skip",
                self._risk_reason_es(risk.reason),
                signal=composite,
                market_name=market_name,
            )
            return

        # 4. SIZE — need orderbook for pricing
        ob = self.polymarket_ws.orderbook
        if not ob.asks and not ob.bids:
            logger.debug("Round %d: orderbook empty, skipping", self._round)
            await self._broadcast_round(
                "skip", "Orderbook vacío — sin liquidez",
                signal=composite, market_name=market_name,
            )
            return

        # Determine side and entry price
        if composite > 0:
            side = "up"
            token_id = market.token_up_id
            entry_price = ob.best_ask or 0.50
        else:
            side = "down"
            token_id = market.token_down_id
            # Subscribe to Down token orderbook for accurate pricing
            await self.polymarket_ws.subscribe(market.token_down_id)
            await asyncio.sleep(2)
            ob = self.polymarket_ws.orderbook
            # REST fallback if WS orderbook is empty for Down token
            if not ob.bids and not ob.asks:
                await self.polymarket_ws.fetch_orderbook_rest(market.token_down_id)
                ob = self.polymarket_ws.orderbook
            # Use Down ask if available and sane, otherwise fallback
            if ob.best_ask is not None and 0.05 < ob.best_ask < 0.95:
                entry_price = ob.best_ask
            else:
                # Try complement of Up best_bid
                await self.polymarket_ws.subscribe(market.token_up_id)
                await asyncio.sleep(1)
                ob = self.polymarket_ws.orderbook
                up_bid = ob.best_bid
                if up_bid is not None and 0.05 < up_bid < 0.95:
                    entry_price = 1.0 - up_bid
                else:
                    entry_price = 0.50

        sizing = kelly_size(abs(composite), entry_price, bankroll)
        if sizing["size_usd"] <= 0:
            reason_en = sizing.get("reason", "no edge per Kelly")
            logger.info(
                "Round %d: no trade — %s", self._round, reason_en,
            )
            reason_es = "Sin edge según Kelly"
            if "spread" in reason_en.lower():
                reason_es = "Spread muy amplio"
            await self._broadcast_round(
                "skip", reason_es,
                signal=composite,
                market_name=market_name,
            )
            return

        # 5. EXECUTE
        spread = ob.spread
        result = await execute_trade(
            condition_id=market.condition_id,
            token_id=token_id,
            side=side,
            signal_score=composite,
            size_usd=sizing["size_usd"],
            shares=sizing["shares"],
            entry_price=entry_price,
            signal_details=signals,
            spread=spread,
            btc_price=self.price_buffer.current_price,
        )

        if not result.success:
            logger.warning("Round %d: execution failed — %s", self._round, result.error)
            return

        # Broadcast trade decision
        side_label = "UP" if side == "up" else "DOWN"
        await self._broadcast_round(
            "trade",
            f"Trade ejecutado: {side_label} ${sizing['size_usd']:.2f}",
            signal=composite,
            market_name=market_name,
        )

        # Notify dashboard
        trade_data = {
            "trade_id": result.trade_id,
            "side": side,
            "signal": composite,
            "size_usd": sizing["size_usd"],
            "entry_price": entry_price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": market.question,
        }
        if self.on_trade:
            await self.on_trade(trade_data)

        # 6. RESOLVE — monitor for stop-loss, then wait for resolution
        wait_seconds = min(
            max(0, market.seconds_remaining + 10),
            MAX_RESOLUTION_WAIT,
        )

        stop_loss_pct = get("stop_loss_pct", 0.30)
        take_profit_pct = get("take_profit_pct", 0.30)
        stop_price = result.filled_price * (1 - stop_loss_pct)
        take_profit_price = result.filled_price * (1 + take_profit_pct)
        early_exit = None  # "stop_loss" or "take_profit"
        exit_proceeds = None

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

            # Refresh orderbook for current token
            await self.polymarket_ws.subscribe(token_id)
            await asyncio.sleep(1)
            elapsed += 1

            bid = self.polymarket_ws.orderbook.best_bid
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
                    signal=composite,
                    market_name=market_name,
                )
            else:
                gain_pct = pnl / result.filled_size * 100 if result.filled_size > 0 else 0
                await self._broadcast_round(
                    "take_profit",
                    f"Take-profit: vendido a {exit_result['exit_price'] * 100:.0f}¢ (+{gain_pct:.0f}%)",
                    signal=composite,
                    market_name=market_name,
                )
        else:
            # Normal resolution
            won = await self._check_resolution_with_retry(market, side)
            new_bankroll = await resolve_trade(result.trade_id, won, bankroll)
            pnl = new_bankroll - bankroll
            outcome_label = "win" if won else "loss"

        # Auto-redeem winning tokens on-chain
        if outcome_label == "win" and not get("dry_run", True):
            try:
                from bot.wallet import redeem_positions
                index_set = 1 if side == "up" else 2
                r = await redeem_positions(market.condition_id, [index_set])
                if r.success:
                    logger.info("Auto-redeemed: tx=%s", r.tx_hash)
                else:
                    logger.warning("Auto-redeem failed: %s", r.error)
            except Exception as e:
                logger.warning("Auto-redeem error: %s", e)

        # Update risk state
        won_for_stats = outcome_label in ("win", "take_profit")
        await record_outcome(won_for_stats, pnl, new_bankroll)
        await update_daily_stats(pnl, won_for_stats, new_bankroll)

        # Store RAG pattern for future k-NN queries
        if self.pattern_store is not None:
            try:
                features = self._build_features(composite)
                if features is not None:
                    await self.pattern_store.store(result.trade_id, features, side)
                    logger.info("Stored RAG pattern for trade %d (%s)", result.trade_id, side)
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
            "Round %d complete: %s pnl=$%.2f bankroll=$%.2f",
            self._round,
            outcome_label.upper(),
            pnl,
            new_bankroll,
        )

    def _build_features(self, signal_score: float = 0.0) -> np.ndarray | None:
        """Build 8-float feature vector from current price buffer state."""
        snap = self.price_buffer.snapshot()
        if snap is None:
            return None
        price = snap["price"]
        if price is None or price == 0:
            return None

        def safe(v):
            return v if v is not None else 0.0

        features = [
            safe(snap["ret_1m"]),
            safe(snap["ret_5m"]),
            safe(snap["ret_15m"]),
            (safe(snap["ema_fast"]) / price) - 1,
            (safe(snap["ema_slow"]) / price) - 1,
            safe(snap["vol_ratio"]),
            safe(snap["rsi_14"]) / 100.0,
            signal_score,
        ]
        return np.array(features, dtype=np.float32)

    async def _check_resolution_with_retry(self, market, side: str) -> bool:
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

        # Real resolution: query Gamma API with retry
        import httpx
        for attempt in range(5):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"https://gamma-api.polymarket.com/markets/{market.condition_id}"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    resolution = (data.get("resolution") or "").lower()
                    if resolution:
                        if side == "up":
                            return resolution in ("yes", "up", "1")
                        else:
                            return resolution in ("no", "down", "0")
                    # Resolution not yet available, wait and retry
                    logger.info("Resolution not yet available (attempt %d/5), waiting 10s...", attempt + 1)
                    await asyncio.sleep(10)
            except Exception as e:
                logger.warning("Resolution check attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(5)

        # All retries exhausted — fallback to price movement
        logger.warning("Resolution not available after 5 retries, using price fallback")
        ret = self.price_buffer.ret(5)
        if ret is None:
            return False  # Conservative: assume loss
        btc_went_up = ret > 0
        return (side == "up") == btc_went_up

    async def _sync_balance(self):
        """Log wallet USDC for reference (does NOT overwrite bankroll).

        Bankroll is tracked internally via resolve_trade() PnL to include
        unredeemed outcome tokens, not just liquid USDC.
        """
        dry_run = get("dry_run", True)
        if dry_run:
            return
        try:
            from bot.executor import fetch_wallet_balance
            balance = fetch_wallet_balance()
            if balance is not None:
                logger.info("Wallet USDC (reference only): $%.2f", balance)
        except Exception as e:
            logger.warning("Balance check failed: %s", e)

    async def _check_daily_reset(self):
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        last_reset = await state.get("daily_reset_date", "")
        if last_reset != today:
            await reset_daily()

    async def get_status(self) -> dict:
        """Get current engine status for API/dashboard."""
        bankroll = await state.get("bankroll", 0)
        daily_pnl = await state.get("daily_pnl", 0)
        enabled = await state.get("enabled", False)
        consec_losses = await state.get("consecutive_losses", 0)
        cooldown = await state.get("cooldown_remaining", 0)

        from bot.executor import has_polymarket_creds
        from bot.risk import _recent_win_rate

        # Circuit breaker check
        circuit_breaker = False
        try:
            win_rate = await _recent_win_rate()
            min_wr = get("min_win_rate", 0.45)
            if win_rate is not None and win_rate < min_wr:
                circuit_breaker = True
        except Exception:
            pass

        return {
            "running": self._running and enabled,
            "round": self._round,
            "bankroll": bankroll,
            "daily_pnl": daily_pnl,
            "consecutive_losses": consec_losses,
            "cooldown_remaining": cooldown,
            "circuit_breaker": circuit_breaker,
            "daily_loss_limit_pct": get("daily_loss_limit_pct", 0.15),
            "price_buffer_size": self.price_buffer.size,
            "current_price": self.price_buffer.current_price,
            "dry_run": get("dry_run", True),
            "has_creds": has_polymarket_creds(),
            "signals": compute_signal(
                self.price_buffer,
                self.polymarket_ws,
                self.rag_signal,
                self.sentiment_signal,
            ),
        }
