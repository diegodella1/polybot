"""REST API endpoints."""

import asyncio
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse
from bot.config import load_config, save_config
from bot.state import state
from db import get_db
from web.auth import verify_password, create_session, check_session, delete_session

logger = logging.getLogger(__name__)


def _require_admin(request: Request):
    """Raise 401 if not authenticated."""
    if not check_session(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def create_router(engine, ws_manager) -> APIRouter:
    router = APIRouter()

    # --- Auth ---

    @router.post("/auth/login")
    async def login(request: Request):
        body = await request.json()
        password = body.get("password", "")
        if not verify_password(password):
            raise HTTPException(status_code=401, detail="Wrong password")
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
    async def get_trades(limit: int = 50, offset: int = 0):
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT * FROM trades
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
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
    async def get_summary():
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(MAX(pnl), 0) as best_trade,
                    COALESCE(MIN(pnl), 0) as worst_trade
                FROM trades WHERE outcome IS NOT NULL"""
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

    # --- Admin-only endpoints (require login) ---

    @router.get("/config")
    async def get_config(request: Request):
        _require_admin(request)
        return load_config()

    @router.put("/config")
    async def update_config(request: Request):
        _require_admin(request)
        data = await request.json()
        save_config(data)
        return {"status": "ok"}

    @router.post("/bot/start")
    async def start_bot(request: Request):
        _require_admin(request)
        import asyncio
        await state.set("enabled", True)
        if not engine._running:
            asyncio.create_task(engine.start())
        return {"status": "started"}

    @router.post("/bot/stop")
    async def stop_bot(request: Request):
        _require_admin(request)
        await engine.stop()
        return {"status": "stopped"}

    @router.post("/keys/save")
    async def save_keys(request: Request):
        """Save Polymarket API keys to .env file."""
        _require_admin(request)
        body = await request.json()
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
            get_exchange_usdc_balance, scan_redeemable_tokens,
        )

        result = {
            "eoa_usdc": None,
            "exchange_usdc": None,
            "matic": None,
            "unredeemed_count": 0,
            "unredeemed_value": 0.0,
        }

        try:
            eoa_usdc, exchange_usdc, matic = await asyncio.gather(
                asyncio.to_thread(get_eoa_usdc_balance),
                asyncio.to_thread(get_exchange_usdc_balance),
                asyncio.to_thread(get_eoa_matic_balance),
            )
            result["eoa_usdc"] = round(eoa_usdc, 4)
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

        if not address:
            raise HTTPException(400, "Address required")
        if not amount or float(amount) <= 0:
            raise HTTPException(400, "Valid amount required")

        result = await transfer_usdc(address, float(amount))
        if not result.success:
            raise HTTPException(500, result.error)

        return {
            "status": "ok",
            "tx_hash": result.tx_hash,
            "amount": float(amount),
            "to": address,
        }

    return router


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
