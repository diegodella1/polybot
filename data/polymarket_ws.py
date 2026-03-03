"""Polymarket WebSocket client for orderbook data."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
import websockets

logger = logging.getLogger(__name__)

POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_REST = "https://clob.polymarket.com"


@dataclass
class OrderbookSnapshot:
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size)
    asks: list[tuple[float, float]] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def bid_volume(self, levels: int = 5) -> float:
        return sum(s for _, s in self.bids[:levels])

    def ask_volume(self, levels: int = 5) -> float:
        return sum(s for _, s in self.asks[:levels])

    def imbalance(self, levels: int = 5) -> float:
        bv = self.bid_volume(levels)
        av = self.ask_volume(levels)
        total = bv + av
        if total == 0:
            return 0.0
        return (bv - av) / total


class PolymarketWS:
    def __init__(self):
        self._ws = None
        self._running = False
        self._subscribed_token: str | None = None
        self.orderbook = OrderbookSnapshot()

    async def subscribe(self, token_id: str, force: bool = False):
        """Subscribe to orderbook updates for a token.

        Skips if already subscribed to the same token (unless force=True).
        """
        if token_id == self._subscribed_token and not force:
            return  # Already subscribed, no need to spam WS
        if token_id != self._subscribed_token:
            self.orderbook = OrderbookSnapshot()  # Reset on token change
        self._subscribed_token = token_id
        if self._ws:
            msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": [token_id],
            }
            await self._ws.send(json.dumps(msg))
            logger.info("Subscribed to orderbook: %s", token_id[:16])

    async def unsubscribe(self):
        if self._ws and self._subscribed_token:
            msg = {
                "type": "unsubscribe",
                "channel": "book",
                "assets_ids": [self._subscribed_token],
            }
            try:
                await self._ws.send(json.dumps(msg))
            except Exception:
                pass
            self._subscribed_token = None
            self.orderbook = OrderbookSnapshot()

    async def start(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    POLYMARKET_WS, ping_interval=20
                ) as ws:
                    self._ws = ws
                    logger.info("Polymarket WS connected")

                    # Re-subscribe if we had an active subscription
                    if self._subscribed_token:
                        await self.subscribe(self._subscribed_token)

                    async for msg in ws:
                        if not self._running:
                            break
                        # Skip empty messages (WS acks/pings)
                        if not msg or not msg.strip():
                            continue
                        try:
                            data = json.loads(msg)
                            # WS can send single messages or batches (arrays)
                            if isinstance(data, list):
                                for item in data:
                                    if isinstance(item, dict):
                                        self._handle_message(item)
                            elif isinstance(data, dict):
                                self._handle_message(data)
                        except (json.JSONDecodeError, KeyError, ValueError) as e:
                            logger.debug("Ignoring non-JSON WS message: %s", e)
            except websockets.ConnectionClosed:
                logger.warning("Polymarket WS disconnected, reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Polymarket WS error: %s", e)
                await asyncio.sleep(5)

    @staticmethod
    def _parse_levels(raw: list) -> list[tuple[float, float]]:
        """Parse orderbook levels — handles both dict and array formats."""
        levels = []
        for entry in raw:
            if isinstance(entry, dict):
                p = float(entry.get("price", 0))
                s = float(entry.get("size", 0))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                p, s = float(entry[0]), float(entry[1])
            else:
                continue
            if s > 0:
                levels.append((p, s))
        return levels

    def _handle_message(self, data: dict):
        event_type = data.get("event_type") or data.get("type", "")

        if event_type in ("book", "snapshot"):
            bids = self._parse_levels(data.get("bids") or [])
            asks = self._parse_levels(data.get("asks") or [])
            bids.sort(key=lambda x: -x[0])
            asks.sort(key=lambda x: x[0])
            self.orderbook = OrderbookSnapshot(
                bids=bids,
                asks=asks,
                timestamp=data.get("timestamp", 0),
            )

        elif event_type in ("book_delta", "delta"):
            for change in data.get("changes", []):
                if isinstance(change, dict):
                    side = change.get("side", "")
                    price = float(change.get("price", 0))
                    size = float(change.get("size", 0))
                elif isinstance(change, (list, tuple)) and len(change) >= 3:
                    side, price, size = str(change[0]), float(change[1]), float(change[2])
                else:
                    continue
                if side == "buy":
                    self._update_side(self.orderbook.bids, price, size, reverse=True)
                elif side == "sell":
                    self._update_side(self.orderbook.asks, price, size, reverse=False)

    @staticmethod
    def _update_side(
        levels: list[tuple[float, float]], price: float, size: float, reverse: bool
    ):
        # Remove existing level at this price
        levels[:] = [(p, s) for p, s in levels if abs(p - price) > 1e-9]
        if size > 0:
            levels.append((price, size))
            levels.sort(key=lambda x: x[0], reverse=reverse)
        # Keep top 20 levels
        if len(levels) > 20:
            del levels[20:]

    async def fetch_orderbook_rest(self, token_id: str) -> bool:
        """Fetch orderbook via REST API as fallback when WS is empty.

        Returns True if orderbook was populated.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{CLOB_REST}/book",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                data = resp.json()

            bids = self._parse_levels(data.get("bids") or [])
            asks = self._parse_levels(data.get("asks") or [])
            bids.sort(key=lambda x: -x[0])
            asks.sort(key=lambda x: x[0])

            if bids or asks:
                self.orderbook = OrderbookSnapshot(
                    bids=bids,
                    asks=asks,
                    timestamp=time.time(),
                )
                logger.info(
                    "REST orderbook: %d bids, %d asks (best_bid=%.2f¢ best_ask=%.2f¢)",
                    len(bids), len(asks),
                    (bids[0][0] * 100) if bids else 0,
                    (asks[0][0] * 100) if asks else 0,
                )
                return True
            return False
        except Exception as e:
            logger.warning("REST orderbook fetch failed: %s", e)
            return False

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
