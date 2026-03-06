"""Order execution via py-clob-client."""

import json
import logging
import math
import os
from datetime import datetime, timezone
from dataclasses import dataclass

from bot.config import get
from bot.state import state
from db import get_db

# Lazy import — only when executing real orders
_SELL = None

logger = logging.getLogger(__name__)


def has_polymarket_creds() -> bool:
    """Check if Polymarket API credentials are configured."""
    return bool(
        os.environ.get("POLYMARKET_PRIVATE_KEY")
        and os.environ.get("POLYMARKET_API_KEY")
        and os.environ.get("POLYMARKET_API_SECRET")
        and os.environ.get("POLYMARKET_API_PASSPHRASE")
    )


def fetch_wallet_balance() -> float | None:
    """Fetch USDC.e balance. Prefers on-chain EOA balance, falls back to CLOB exchange."""
    # Primary: on-chain EOA balance (source of truth)
    try:
        from bot.wallet import get_eoa_usdc_balance
        balance = get_eoa_usdc_balance()
        logger.info("EOA USDC.e balance: $%.2f", balance)
        return balance
    except Exception as e:
        logger.warning("EOA balance check failed (%s), trying CLOB fallback", e)

    # Fallback: CLOB exchange balance
    if not has_polymarket_creds():
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        bal = client.get_balance_allowance(
            params=BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
            )
        )
        usdc = float(bal.get("balance", "0")) / 1e6
        logger.info("CLOB USDC balance (fallback): $%.2f", usdc)
        return usdc
    except Exception as e:
        logger.error("Failed to fetch wallet balance: %s", e)
        return None


@dataclass
class TradeResult:
    success: bool
    trade_id: int | None = None
    order_id: str | None = None
    filled_price: float = 0.0
    filled_size: float = 0.0
    shares: float = 0.0
    side: str = ""
    error: str = ""


# Lazy init — only import when actually executing
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        host = "https://clob.polymarket.com"
        chain_id = 137  # Polygon mainnet
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        api_key = os.environ.get("POLYMARKET_API_KEY", "")
        api_secret = os.environ.get("POLYMARKET_API_SECRET", "")
        api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "")

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        _client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=pk,
            creds=creds,
        )
        return _client
    except Exception as e:
        logger.error("Failed to init CLOB client: %s", e)
        return None


async def execute_trade(
    condition_id: str,
    token_id: str,
    side: str,
    signal_score: float,
    size_usd: float,
    shares: float,
    entry_price: float,
    signal_details: dict,
    spread: float | None = None,
    btc_price: float | None = None,
) -> TradeResult:
    """Execute a trade on Polymarket.

    In dry_run mode, simulates the trade without placing real orders.
    """
    dry_run = get("dry_run", True)
    max_spread = get("max_spread_cents", 5) / 100.0

    # Spread check
    if spread is not None and spread > max_spread:
        return TradeResult(
            success=False,
            error=f"Spread too wide: {spread:.4f} > {max_spread:.4f}",
        )

    timestamp = datetime.now(timezone.utc).isoformat()

    if dry_run:
        logger.info(
            "[DRY RUN] %s | signal=%.3f | $%.2f @ %.4f | %s",
            side.upper(),
            signal_score,
            size_usd,
            entry_price,
            condition_id[:16],
        )
        order_id = f"dry_{int(datetime.now(timezone.utc).timestamp())}"

        # Record dry-run trade in DB
        db = await get_db()
        try:
            cursor = await db.execute(
                """INSERT INTO trades
                   (timestamp, condition_id, token_id, side, signal_score,
                    entry_price, size_usd, shares, signal_details, btc_price,
                    dry_run, order_id, order_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp, condition_id, token_id, side, signal_score,
                    entry_price, size_usd, shares, json.dumps(signal_details),
                    btc_price, 1, order_id, "filled",
                ),
            )
            await db.commit()
            trade_id = cursor.lastrowid
        finally:
            await db.close()

        await state.set("has_open_position", True)
        await state.set("current_exposure", size_usd)

        return TradeResult(
            success=True,
            trade_id=trade_id,
            order_id=order_id,
            filled_price=entry_price,
            filled_size=size_usd,
            shares=shares,
            side=side,
        )

    # --- LIVE execution ---
    client = _get_client()
    if client is None:
        return TradeResult(success=False, error="CLOB client not initialized")

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Proportional slippage: ~3% of price to cross spread aggressively
        slippage = max(0.02, round(entry_price * 0.03, 2))
        entry_price = min(entry_price + slippage, 0.95)

        # Round price to valid tick size
        tick_size = client.get_tick_size(token_id)
        if tick_size:
            tick = float(tick_size)
            entry_price = round(round(entry_price / tick) * tick, 4)

        # Integer shares: Polymarket requires minimum 5 shares per order.
        MIN_SHARES = 5
        min_trade = get("min_trade_usd", 1.0)
        shares = max(MIN_SHARES, math.floor(size_usd / entry_price))
        size_usd = round(shares * entry_price, 2)
        if size_usd < min_trade:
            shares = max(MIN_SHARES, math.ceil(min_trade / entry_price))
            size_usd = round(shares * entry_price, 2)

        # 3a. INSERT pre-order row so we never lose track of the trade
        db = await get_db()
        try:
            cursor = await db.execute(
                """INSERT INTO trades
                   (timestamp, condition_id, token_id, side, signal_score,
                    entry_price, size_usd, shares, signal_details, btc_price,
                    dry_run, order_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp, condition_id, token_id, side, signal_score,
                    entry_price, size_usd, shares, json.dumps(signal_details),
                    btc_price, 0, "pending_fill",
                ),
            )
            await db.commit()
            trade_id = cursor.lastrowid
        finally:
            await db.close()

        # Create signed order
        signed_order = client.create_order(
            order_args=OrderArgs(
                token_id=token_id,
                price=entry_price,
                size=shares,
                side=BUY,
            ),
        )

        # GTC limit order — sits on book until filled
        order_type = OrderType.GTC
        order = client.post_order(signed_order, orderType=order_type)

        if not order or not order.get("orderID"):
            # Order rejected — clean up pre-order row
            await _delete_trade(trade_id)
            return TradeResult(success=False, error="Order rejected")

        order_id = order["orderID"]

        # Update DB with order_id
        db = await get_db()
        try:
            await db.execute(
                "UPDATE trades SET order_id = ? WHERE id = ?",
                (order_id, trade_id),
            )
            await db.commit()
        finally:
            await db.close()

        logger.info(
            "ORDER PLACED: %s | %s | $%.2f @ %.4f | order=%s | type=%s",
            side.upper(),
            condition_id[:16],
            size_usd,
            entry_price,
            order_id,
            "GTC",
        )

        # Fill verification for GTC orders
        import asyncio
        filled = False
        fill_price = entry_price
        fill_size = size_usd
        for attempt in range(3):  # Check every 5s for 15s
            await asyncio.sleep(5)
            try:
                order_resp = client.get_order(order_id)
                status = (order_resp or {}).get("status", "").lower()
                if status in ("matched", "filled"):
                    filled = True
                    # F4: Extract real fill price/size from response
                    if order_resp.get("associate_trades"):
                        trades_list = order_resp["associate_trades"]
                        if trades_list:
                            total_cost = sum(
                                float(t.get("price", 0)) * float(t.get("size", 0))
                                for t in trades_list
                            )
                            total_shares = sum(float(t.get("size", 0)) for t in trades_list)
                            if total_shares > 0:
                                fill_price = round(total_cost / total_shares, 6)
                                fill_size = round(total_cost, 2)
                                shares = total_shares
                    break
                elif status in ("cancelled", "expired"):
                    logger.warning("GTC order %s was %s", order_id, status)
                    await _delete_trade(trade_id)
                    return TradeResult(success=False, error=f"Order {status}")
            except Exception:
                pass

        if not filled:
            # Cancel unfilled GTC order
            try:
                client.cancel(order_id)
                logger.warning("GTC order %s cancelled after 15s timeout", order_id)
            except Exception:
                pass
            # Verify cancel worked — order might have filled during cancel
            await asyncio.sleep(2)
            try:
                final_resp = client.get_order(order_id)
                final_status = (final_resp or {}).get("status", "").lower()
                if final_status in ("matched", "filled"):
                    logger.info("GTC order %s filled during cancel — recovering", order_id)
                    db = await get_db()
                    try:
                        await db.execute(
                            "UPDATE trades SET order_status = 'filled' WHERE id = ?",
                            (trade_id,),
                        )
                        await db.commit()
                    finally:
                        await db.close()
                    await state.set("has_open_position", True)
                    await state.set("current_exposure", size_usd)
                    return TradeResult(
                        success=True, trade_id=trade_id, order_id=order_id,
                        filled_price=entry_price, filled_size=size_usd,
                        shares=shares, side=side,
                    )
            except Exception:
                pass
            await _delete_trade(trade_id)
            return TradeResult(success=False, error="GTC order timed out (15s) — cancelled")

        # Update DB: mark filled with real prices
        db = await get_db()
        try:
            await db.execute(
                """UPDATE trades SET order_status = 'filled',
                   entry_price = ?, size_usd = ?, shares = ?
                   WHERE id = ?""",
                (fill_price, fill_size, shares, trade_id),
            )
            await db.commit()
        finally:
            await db.close()

        # Update balance/allowance for CONDITIONAL tokens so we can SELL later
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            logger.info("Updated CONDITIONAL allowance for token %s", token_id[:16])
        except Exception as e:
            logger.warning("Failed to update CONDITIONAL allowance: %s", e)

    except Exception as e:
        logger.error("Order execution failed: %s", e)
        # If we already have a trade_id, clean up
        if 'trade_id' in locals():
            await _delete_trade(trade_id)
        return TradeResult(success=False, error=str(e))

    await state.set("has_open_position", True)
    await state.set("current_exposure", fill_size)

    return TradeResult(
        success=True,
        trade_id=trade_id,
        order_id=order_id,
        filled_price=fill_price,
        filled_size=fill_size,
        shares=shares,
        side=side,
    )


async def _delete_trade(trade_id: int):
    """Remove a pre-order trade row that never got filled."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        await db.commit()
        logger.info("Cleaned up unfilled trade row %d", trade_id)
    finally:
        await db.close()


async def recover_pending_fills():
    """Recover trades stuck in pending_fill status (crash during order placement).

    Called on startup before the main trading loop.
    Checks order status via CLOB API and either marks filled or deletes.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, order_id FROM trades WHERE order_status = 'pending_fill'"
        )
        pending = await cursor.fetchall()
    finally:
        await db.close()

    if not pending:
        return

    logger.warning("Found %d pending_fill trades to recover", len(pending))
    client = _get_client()

    for trade in pending:
        trade_id = trade["id"]
        order_id = trade["order_id"]

        if not order_id or not client:
            # No order was ever placed — safe to delete
            await _delete_trade(trade_id)
            continue

        try:
            order_resp = client.get_order(order_id)
            status = (order_resp or {}).get("status", "").lower()
            if status in ("matched", "filled"):
                # Was actually filled — mark it
                db = await get_db()
                try:
                    await db.execute(
                        "UPDATE trades SET order_status = 'filled' WHERE id = ?",
                        (trade_id,),
                    )
                    await db.commit()
                finally:
                    await db.close()
                logger.info("Recovered pending_fill trade %d as FILLED (order=%s)", trade_id, order_id)
            else:
                # Not filled — cancel if still live, verify, then delete
                try:
                    client.cancel(order_id)
                except Exception:
                    pass
                import asyncio
                await asyncio.sleep(2)
                try:
                    recheck = client.get_order(order_id)
                    recheck_status = (recheck or {}).get("status", "").lower()
                    if recheck_status in ("matched", "filled"):
                        db = await get_db()
                        try:
                            await db.execute(
                                "UPDATE trades SET order_status = 'filled' WHERE id = ?",
                                (trade_id,),
                            )
                            await db.commit()
                        finally:
                            await db.close()
                        logger.info("Recovered pending_fill trade %d as FILLED (late fill during cancel)", trade_id)
                        continue
                except Exception:
                    pass
                await _delete_trade(trade_id)
                logger.info("Recovered pending_fill trade %d: order %s was %s — deleted", trade_id, order_id, status)
        except Exception as e:
            logger.warning("Could not recover pending_fill trade %d: %s — deleting", trade_id, e)
            await _delete_trade(trade_id)


async def exit_position(
    token_id: str, shares: float, bid: float
) -> dict:
    """Sell position for stop-loss. Returns {"success": bool, "exit_price": float, "proceeds": float}."""
    dry_run = get("dry_run", True)

    if dry_run:
        exit_price = max(0.01, bid - 0.01)
        proceeds = round(shares * exit_price, 2)
        logger.info(
            "[DRY RUN] STOP-LOSS SELL | %d shares @ %.2f¢ | proceeds=$%.2f",
            shares, exit_price * 100, proceeds,
        )
        return {"success": True, "exit_price": exit_price, "proceeds": proceeds}

    client = _get_client()
    if client is None:
        return {"success": False, "exit_price": 0, "proceeds": 0}

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        exit_price = max(0.01, bid - 0.01)

        tick_size = client.get_tick_size(token_id)
        if tick_size:
            tick = float(tick_size)
            exit_price = round(round(exit_price / tick) * tick, 4)

        int_shares = max(1, math.floor(shares))

        signed_order = client.create_order(
            order_args=OrderArgs(
                token_id=token_id,
                price=exit_price,
                size=int_shares,
                side=SELL,
            ),
        )
        # FOK = fill-or-kill: fills immediately or fails, no resting order
        order = client.post_order(signed_order, orderType=OrderType.FOK)

        if not order or not order.get("orderID"):
            logger.error("Exit SELL order rejected (FOK)")
            return {"success": False, "exit_price": 0, "proceeds": 0}

        order_id = order["orderID"]

        # Verify FOK fill — check order status
        import asyncio
        await asyncio.sleep(2)
        try:
            order_status = client.get_order(order_id)
            status = (order_status or {}).get("status", "").lower()
            if status not in ("matched", "filled"):
                logger.warning("FOK exit not filled (status=%s), order=%s", status, order_id)
                return {"success": False, "exit_price": 0, "proceeds": 0}
        except Exception as e:
            logger.warning("Could not verify FOK fill: %s", e)
            # Conservative: assume not filled
            return {"success": False, "exit_price": 0, "proceeds": 0}

        proceeds = round(int_shares * exit_price, 2)
        logger.info(
            "EXIT SELL (FOK): %d shares @ %.4f | order=%s | proceeds=$%.2f",
            int_shares, exit_price, order_id, proceeds,
        )
        return {"success": True, "exit_price": exit_price, "proceeds": proceeds}

    except Exception as e:
        logger.error("Stop-loss exit failed: %s", e)
        return {"success": False, "exit_price": 0, "proceeds": 0}


async def resolve_trade(trade_id: int, won: bool, bankroll: float,
                        exit_proceeds: float | None = None,
                        exit_type: str | None = None) -> float:
    """Resolve a trade and calculate P&L.

    Args:
        exit_type: "stop_loss" or "take_profit" when exiting early via sell.

    Returns the new bankroll.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        )
        trade = await cursor.fetchone()
        if trade is None:
            logger.error("Trade %d not found", trade_id)
            return bankroll

        # Polymarket taker fee on 5-min crypto markets: 10% (fee_rate_bps=1000)
        # Fee is charged on BUY (entry) and SELL (early exit), NOT on redemption
        TAKER_FEE = 0.10

        if exit_proceeds is not None:
            # Early exit: gross proceeds from sell, both entry and exit have fees
            sell_fee = round(exit_proceeds * TAKER_FEE, 4)
            entry_fee = round(trade["size_usd"] * TAKER_FEE, 4)
            fee = entry_fee + sell_fee
            pnl = (exit_proceeds - sell_fee) - (trade["size_usd"] + entry_fee)
            outcome = exit_type or "stop_loss"
        elif won:
            # Win: redeem shares for $1 each (no fee on redemption)
            # Entry fee was charged when buying
            payout = trade["shares"]  # shares * $1.00
            entry_fee = round(trade["size_usd"] * TAKER_FEE, 4)
            fee = entry_fee
            pnl = payout - trade["size_usd"] - entry_fee
            outcome = "win"
        else:
            # Loss: lost entry cost + entry fee
            entry_fee = round(trade["size_usd"] * TAKER_FEE, 4)
            fee = entry_fee
            pnl = -(trade["size_usd"] + entry_fee)
            outcome = "loss"

        # Track cumulative fees
        from bot.state import state
        daily_fees = await state.get("daily_fees", 0.0)
        await state.set("daily_fees", round(daily_fees + fee, 4))

        new_bankroll = bankroll + pnl

        await db.execute(
            """UPDATE trades
               SET outcome = ?, pnl = ?, bankroll_after = ?
               WHERE id = ?""",
            (outcome, pnl, new_bankroll, trade_id),
        )
        await db.commit()

        logger.info(
            "RESOLVED: trade=%d %s pnl=$%.2f bankroll=$%.2f",
            trade_id,
            outcome.upper(),
            pnl,
            new_bankroll,
        )
        return new_bankroll
    finally:
        await db.close()


async def update_daily_stats(pnl: float, won: bool, bankroll: float):
    """Update daily stats table."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (today,)
        )
        row = await cursor.fetchone()

        if row is None:
            await db.execute(
                """INSERT INTO daily_stats
                   (date, trades_count, wins, losses, pnl, bankroll_open, bankroll_close,
                    best_trade, worst_trade)
                   VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    today,
                    1 if won else 0,
                    0 if won else 1,
                    pnl,
                    bankroll - pnl,
                    bankroll,
                    max(0, pnl),
                    min(0, pnl),
                ),
            )
        else:
            await db.execute(
                """UPDATE daily_stats SET
                   trades_count = trades_count + 1,
                   wins = wins + ?,
                   losses = losses + ?,
                   pnl = pnl + ?,
                   bankroll_close = ?,
                   best_trade = MAX(best_trade, ?),
                   worst_trade = MIN(worst_trade, ?)
                   WHERE date = ?""",
                (
                    1 if won else 0,
                    0 if won else 1,
                    pnl,
                    bankroll,
                    pnl,
                    pnl,
                    today,
                ),
            )
        await db.commit()
    finally:
        await db.close()
