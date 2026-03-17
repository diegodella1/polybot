"""REST API endpoints."""

import asyncio
import logging
import os
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse
from bot.config import load_config, save_config
from bot.state import state
from db import get_db
from web.auth import (
    verify_password, create_session, check_session, delete_session,
    check_rate_limit, record_failed_attempt, clear_attempts,
)

logger = logging.getLogger(__name__)


def _audit(request: Request, action: str, details: str = ""):
    ip = request.client.host if request.client else "unknown"
    logger.warning("AUDIT [%s] %s %s", ip, action, details)


def _require_admin(request: Request):
    """Raise 401 if not authenticated."""
    if not check_session(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def create_router(engine, ws_manager) -> APIRouter:
    router = APIRouter()

    # --- Auth ---

    @router.post("/auth/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "unknown"
        if check_rate_limit(ip):
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 5 minutes.")
        body = await request.json()
        password = body.get("password", "")
        if not verify_password(password):
            record_failed_attempt(ip)
            raise HTTPException(status_code=401, detail="Wrong password")
        clear_attempts(ip)
        _audit(request, "LOGIN_SUCCESS")
        response = JSONResponse({"status": "ok"})
        create_session(response)
        return response

    @router.post("/auth/logout")
    async def logout(request: Request):
        response = JSONResponse({"status": "ok"})
        delete_session(request, response)
        return response

    @router.get("/auth/check")
    async def auth_check(request: Request):
        return {"authenticated": check_session(request)}

    # --- Public read-only endpoints (dashboard) ---

    @router.get("/status")
    async def get_status():
        return await engine.get_status()

    @router.get("/trades")
    async def get_trades(limit: int = 50, offset: int = 0, mode: str = "all"):
        db = await get_db()
        try:
            where = _mode_filter(mode)
            cursor = await db.execute(
                f"SELECT * FROM trades {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    @router.get("/paper-trades")
    async def get_paper_trades(limit: int = 100):
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            trades = [dict(r) for r in rows]

            # Summary stats (exclude expired — unknown outcome)
            resolved = [t for t in trades if t.get("outcome") in ("win", "loss")]
            wins = sum(1 for t in resolved if t["outcome"] == "win")
            losses = sum(1 for t in resolved if t["outcome"] == "loss")
            total = wins + losses
            total_pnl = sum(t.get("pnl_simulated", 0) or 0 for t in resolved)

            return {
                "trades": trades,
                "summary": {
                    "total": total,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": wins / total if total > 0 else 0,
                    "total_pnl": round(total_pnl, 4),
                    "expired": sum(1 for t in trades if t.get("outcome") == "expired"),
                    "pending": sum(1 for t in trades if not t.get("outcome")),
                },
            }
        finally:
            await db.close()

    @router.get("/stats/daily")
    async def get_daily_stats(limit: int = 30):
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    @router.get("/stats/summary")
    async def get_summary(mode: str = "all"):
        db = await get_db()
        try:
            mode_cond = _mode_filter(mode, prefix="AND")
            cursor = await db.execute(
                f"""SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN outcome IN ('win','take_profit') THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome IN ('loss','stop_loss') THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(MAX(pnl), 0) as best_trade,
                    COALESCE(MIN(pnl), 0) as worst_trade
                FROM trades WHERE outcome IS NOT NULL {mode_cond}"""
            )
            row = await cursor.fetchone()
            data = dict(row)
            wins = data["wins"] or 0
            losses = data["losses"] or 0
            total = wins + losses
            data["wins"] = wins
            data["losses"] = losses
            data["win_rate"] = wins / total if total > 0 else 0
            return data
        finally:
            await db.close()

    @router.get("/signals")
    async def get_signals():
        status = await engine.get_status()
        return status.get("signals", {})

    @router.websocket("/trades/live")
    async def trades_live(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    @router.get("/rag/patterns")
    async def get_patterns(limit: int = 10):
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT p.id, p.outcome, p.created_at, t.side, t.signal_score, t.pnl
                   FROM patterns p
                   LEFT JOIN trades t ON p.trade_id = t.id
                   ORDER BY p.id DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    @router.post("/rag/purge")
    async def purge_rag(request: Request):
        """Purge all RAG patterns."""
        _require_admin(request)
        if engine.pattern_store is not None:
            await engine.pattern_store.clear()
            return {"status": "ok", "message": "All patterns purged"}
        return {"status": "ok", "message": "No pattern store"}

    # --- Admin-only endpoints (require login) ---

    @router.get("/config")
    async def get_config(request: Request):
        _require_admin(request)
        return load_config()

    @router.put("/config")
    async def update_config(request: Request):
        _require_admin(request)
        data = await request.json()
        _audit(request, "CONFIG_UPDATE", f"keys={list(data.keys())}")
        save_config(data)
        return {"status": "ok"}

    @router.post("/trades/resolve-pending")
    async def resolve_pending(request: Request):
        """Manually trigger resolution of pending trades."""
        _require_admin(request)
        _audit(request, "RESOLVE_PENDING")
        bankroll = await state.get("bankroll", 0.0)
        await engine._recover_pending_trades(bankroll)
        # Return updated pending count
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE outcome IS NULL "
                "AND (order_status IS NULL OR order_status = 'filled')"
            )
            row = await cursor.fetchone()
            remaining = row["cnt"] if row else 0
        finally:
            await db.close()
        return {"status": "ok", "remaining_pending": remaining}

    @router.post("/bot/start")
    async def start_bot(request: Request):
        _require_admin(request)
        _audit(request, "BOT_START")
        import asyncio
        await state.set("enabled", True)
        if not engine._running:
            asyncio.create_task(engine.start())
        return {"status": "started"}

    @router.post("/bot/stop")
    async def stop_bot(request: Request):
        _require_admin(request)
        _audit(request, "BOT_STOP")
        await engine.stop()
        return {"status": "stopped"}

    @router.post("/keys/save")
    async def save_keys(request: Request):
        """Save Polymarket API keys to .env file."""
        _require_admin(request)
        body = await request.json()
        _audit(request, "KEYS_SAVE")
        _update_env(body)
        # Reload into current process
        for key in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_KEY",
                     "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"):
            if key in body and body[key]:
                os.environ[key] = body[key]
        # Reset cached client
        import bot.executor
        bot.executor._client = None
        return {"status": "ok"}

    @router.post("/keys/derive")
    async def derive_keys(request: Request):
        """Derive API credentials from private key."""
        _require_admin(request)
        body = await request.json()
        pk = body.get("POLYMARKET_PRIVATE_KEY", "")
        if not pk:
            raise HTTPException(400, "Private key required")
        if not pk.startswith("0x"):
            pk = "0x" + pk
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
            )
            creds = client.create_or_derive_api_creds()
            return {
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "api_passphrase": creds.api_passphrase,
                "address": client.signer.address(),
            }
        except Exception as e:
            raise HTTPException(500, str(e))

    @router.get("/keys/status")
    async def keys_status(request: Request):
        """Check if API keys are configured and get balance."""
        _require_admin(request)
        from bot.executor import has_polymarket_creds, fetch_wallet_balance
        result = {"has_creds": has_polymarket_creds(), "balance": None}
        if result["has_creds"]:
            bal = fetch_wallet_balance()
            result["balance"] = bal
        return result

    @router.post("/keys/telegram")
    async def save_telegram(request: Request):
        """Save Telegram credentials."""
        _require_admin(request)
        body = await request.json()
        _update_env(body)
        for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
            if key in body and body[key]:
                os.environ[key] = body[key]
        return {"status": "ok"}

    # --- Wallet Management (admin-only) ---

    @router.get("/wallet/balances")
    async def wallet_balances(request: Request):
        """Get EOA USDC.e, exchange USDC, MATIC, and unredeemed token count."""
        _require_admin(request)
        import asyncio
        from bot.wallet import (
            get_eoa_usdc_balance, get_eoa_matic_balance,
            get_exchange_usdc_balance, get_usdt_balance, scan_redeemable_tokens,
        )

        result = {
            "eoa_usdc": None,
            "eoa_usdt": None,
            "exchange_usdc": None,
            "matic": None,
            "unredeemed_count": 0,
            "unredeemed_value": 0.0,
        }

        try:
            eoa_usdc, eoa_usdt, exchange_usdc, matic = await asyncio.gather(
                asyncio.to_thread(get_eoa_usdc_balance),
                asyncio.to_thread(get_usdt_balance),
                asyncio.to_thread(get_exchange_usdc_balance),
                asyncio.to_thread(get_eoa_matic_balance),
            )
            result["eoa_usdc"] = round(eoa_usdc, 4)
            result["eoa_usdt"] = round(eoa_usdt, 4)
            result["exchange_usdc"] = round(exchange_usdc, 4)
            result["matic"] = round(matic, 4)
        except Exception as e:
            logger.warning("Failed to fetch on-chain balances: %s", e)

        try:
            redeemable = await asyncio.to_thread(scan_redeemable_tokens)
            result["unredeemed_count"] = len(redeemable)
            result["unredeemed_value"] = round(
                sum(r["balance"] for r in redeemable), 4
            )
        except Exception as e:
            logger.warning("Failed to scan redeemable tokens: %s", e)

        return result

    @router.post("/wallet/redeem-all")
    async def wallet_redeem_all(request: Request):
        """Scan and redeem all winning tokens."""
        _require_admin(request)
        _audit(request, "WALLET_REDEEM_ALL")
        from bot.wallet import scan_redeemable_tokens, redeem_positions

        try:
            redeemable = await asyncio.to_thread(scan_redeemable_tokens)
        except Exception as e:
            raise HTTPException(500, f"Scan failed: {e}")

        if not redeemable:
            return {"status": "ok", "message": "No tokens to redeem", "redeemed": 0}

        results = []
        for item in redeemable:
            r = await redeem_positions(item["condition_id"], [item["index_set"]])
            results.append({
                "condition_id": item["condition_id"][:16] + "...",
                "side": item["side"],
                "balance": item["balance"],
                "success": r.success,
                "tx_hash": r.tx_hash,
                "error": r.error,
            })

        ok = sum(1 for r in results if r["success"])
        return {
            "status": "ok",
            "redeemed": ok,
            "total": len(results),
            "results": results,
        }

    @router.post("/wallet/send")
    async def wallet_send(request: Request):
        """Transfer USDC.e to an external address."""
        _require_admin(request)
        from bot.wallet import transfer_usdc

        body = await request.json()
        address = body.get("address", "").strip()
        amount = body.get("amount")

        if not address or not re.match(r'^0x[0-9a-fA-F]{40}$', address):
            raise HTTPException(400, "Invalid Ethereum address format")
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid amount")
        if amount_f <= 0 or amount_f > 1000:
            raise HTTPException(400, "Amount must be between 0 and $1000")

        _audit(request, "WALLET_SEND", f"to={address} amount=${amount_f:.2f}")
        result = await transfer_usdc(address, amount_f)
        if not result.success:
            raise HTTPException(500, result.error)

        return {
            "status": "ok",
            "tx_hash": result.tx_hash,
            "amount": float(amount),
            "to": address,
        }

    @router.post("/wallet/swap-usdt")
    async def wallet_swap_usdt(request: Request):
        """Swap USDT → USDC.e via Uniswap V3. Optionally specify amount."""
        _require_admin(request)
        _audit(request, "WALLET_SWAP_USDT")
        from bot.wallet import swap_usdt_to_usdce

        body = await request.json()
        amount = body.get("amount")  # None = swap all

        result = await swap_usdt_to_usdce(float(amount) if amount else None)
        if not result.success:
            raise HTTPException(500, result.error)

        return {
            "status": "ok",
            "tx_hash": result.tx_hash,
            "details": result.details,
        }

    # --- Analytics endpoints (v2) ---

    @router.get("/stats/by-hour")
    async def stats_by_hour(days: int = 30, mode: str = "all"):
        """Trades/wins/pnl grouped by UTC hour (for schedule analysis)."""
        db = await get_db()
        try:
            mode_cond = _mode_filter(mode, prefix="AND")
            cursor = await db.execute(
                f"""SELECT
                    CAST(strftime('%H', timestamp) AS INTEGER) as hour_utc,
                    COUNT(*) as trades,
                    SUM(CASE WHEN outcome IN ('win','take_profit') THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome IN ('loss','stop_loss') THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(pnl), 0) as pnl
                FROM trades
                WHERE outcome IS NOT NULL
                  AND timestamp >= datetime('now', '-{min(days, 365)} days')
                  {mode_cond}
                GROUP BY hour_utc
                ORDER BY hour_utc"""
            )
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                total = d["wins"] + d["losses"]
                d["win_rate"] = d["wins"] / total if total > 0 else 0
                result.append(d)
            return result
        finally:
            await db.close()

    @router.get("/stats/slippage")
    async def stats_slippage(limit: int = 100):
        """Recent slippage p50/p95/p99."""
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT slippage FROM slippage_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return {"count": 0, "p50": 0, "p95": 0, "p99": 0}

            slippages = sorted(r["slippage"] for r in rows)
            n = len(slippages)

            def percentile(data, pct):
                idx = int(len(data) * pct / 100)
                return data[min(idx, len(data) - 1)]

            return {
                "count": n,
                "p50": round(percentile(slippages, 50), 6),
                "p95": round(percentile(slippages, 95), 6),
                "p99": round(percentile(slippages, 99), 6),
                "mean": round(sum(slippages) / n, 6),
                "max": round(slippages[-1], 6),
            }
        finally:
            await db.close()

    return router


def _mode_filter(mode: str, prefix: str = "WHERE") -> str:
    """Build SQL clause to filter trades by dry_run mode."""
    if mode == "paper":
        return f"{prefix} dry_run = 1"
    elif mode == "live":
        return f"{prefix} dry_run = 0"
    return ""  # 'all' — no filter


def _update_env(data: dict):
    """Update .env file with new key-value pairs."""
    import os
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            if key in data and data[key]:
                new_lines.append(f"{key}={data[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Add keys that weren't in the file
    for key, value in data.items():
        if key not in updated_keys and value:
            new_lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)
