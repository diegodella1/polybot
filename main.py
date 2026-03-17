"""Polybot — Trading bot for Polymarket BTC Up/Down 5-min markets.

Entry point: starts the trading engine + web server.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv()


def setup_logging():
    from logging.handlers import RotatingFileHandler

    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=3),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # Quiet noisy loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def main():
    from db import init_db
    from bot.engine import TradingEngine
    from bot.state import state
    from web.app import create_app
    from web.ws_handler import WebSocketManager
    from rag.pattern_store import PatternStore
    from notifications.telegram import TelegramNotifier

    # Init DB
    await init_db()
    logging.info("Database initialized")

    # Sync bankroll: try wallet balance first, fallback to INITIAL_BANKROLL
    from bot.executor import fetch_wallet_balance, has_polymarket_creds

    # Bankroll is tracked internally via resolve_trade() PnL.
    # Don't overwrite with wallet USDC (excludes unredeemed outcome tokens).
    bankroll = await state.get("bankroll")
    if bankroll is None:
        if has_polymarket_creds():
            wallet_balance = fetch_wallet_balance()
            if wallet_balance is not None and wallet_balance > 0:
                await state.set("bankroll", wallet_balance)
                logging.info("Initial bankroll from wallet: $%.2f", wallet_balance)
                bankroll = wallet_balance
        if bankroll is None:
            initial = float(os.environ.get("INITIAL_BANKROLL", "50.0"))
            await state.set("bankroll", initial)
            logging.info("No Polymarket creds, using INITIAL_BANKROLL: $%.2f", initial)

    # Create components
    engine = TradingEngine()
    ws_manager = WebSocketManager()
    pattern_store = PatternStore()
    telegram = TelegramNotifier()

    # Load RAG patterns and wire to engine
    await pattern_store.load()
    engine.pattern_store = pattern_store
    engine.telegram = telegram

    # Wire up callbacks
    async def on_trade(data):
        await ws_manager.broadcast("trade", data)
        await telegram.notify_trade(data)

    async def on_signal(data):
        await ws_manager.broadcast("signal", data)

    async def on_status(data):
        await ws_manager.broadcast("status", data)

    async def on_round_update(data):
        await ws_manager.broadcast("round_update", data)

    engine.on_trade = on_trade
    engine.on_signal = on_signal
    engine.on_status = on_status
    engine.on_round_update = on_round_update

    # Create FastAPI app
    app = create_app(engine, ws_manager)

    # Start everything
    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8888,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    tasks = [
        asyncio.create_task(engine.start()),
    ]

    # Run uvicorn in the same event loop
    await asyncio.gather(
        server.serve(),
        *tasks,
        return_exceptions=True,
    )


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down...")
