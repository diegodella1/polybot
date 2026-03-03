"""Binance WebSocket client for BTC/USDT klines (1m).

Pre-loads historical klines via REST API at startup for instant signal generation.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import websockets

from bot.config import get

logger = logging.getLogger(__name__)

# Binance WS kline stream
BASE_URL = "wss://stream.binance.com:9443/ws"
REST_URL = "https://api.binance.com/api/v3/klines"


class BinanceKline:
    __slots__ = ("open_time", "open", "high", "low", "close", "volume", "is_closed")

    def __init__(self, data: dict):
        k = data["k"]
        self.open_time = k["t"]
        self.open = float(k["o"])
        self.high = float(k["h"])
        self.low = float(k["l"])
        self.close = float(k["c"])
        self.volume = float(k["v"])
        self.is_closed = k["x"]

    @classmethod
    def from_rest(cls, row: list):
        """Create from Binance REST API kline array."""
        obj = object.__new__(cls)
        obj.open_time = row[0]
        obj.open = float(row[1])
        obj.high = float(row[2])
        obj.low = float(row[3])
        obj.close = float(row[4])
        obj.volume = float(row[5])
        obj.is_closed = True
        return obj


class BinanceWS:
    def __init__(self, on_kline=None):
        self._on_kline = on_kline
        self._ws = None
        self._running = False

    async def _preload_history(self, symbol: str, limit: int = 50):
        """Fetch historical klines via REST API to warm up the price buffer."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(REST_URL, params={
                    "symbol": symbol.upper(),
                    "interval": "1m",
                    "limit": limit,
                })
                resp.raise_for_status()
                rows = resp.json()

            # Skip the last one (it's the current in-progress candle)
            closed = rows[:-1] if rows else []
            for row in closed:
                kline = BinanceKline.from_rest(row)
                if self._on_kline:
                    await self._on_kline(kline)

            logger.info("Pre-loaded %d historical klines from Binance REST", len(closed))
        except Exception as e:
            logger.warning("Failed to pre-load history: %s", e)

    async def start(self):
        self._running = True
        symbol = get("binance_symbol", "btcusdt")

        # Pre-load historical data for instant signal generation
        await self._preload_history(symbol)

        url = f"{BASE_URL}/{symbol}@kline_1m"
        logger.info("Connecting to Binance WS: %s", url)

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._ws = ws
                    logger.info("Binance WS connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            if data.get("e") == "kline":
                                kline = BinanceKline(data)
                                if self._on_kline:
                                    await self._on_kline(kline)
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning("Bad kline message: %s", e)
            except websockets.ConnectionClosed:
                logger.warning("Binance WS disconnected, reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Binance WS error: %s", e)
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
