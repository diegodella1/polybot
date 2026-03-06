"""Chainlink BTC/USD on-chain price feed (Polygon mainnet)."""

import logging
import math
import time

from web3 import Web3

logger = logging.getLogger(__name__)

# Chainlink BTC/USD aggregator on Polygon (8 decimals)
AGGREGATOR = Web3.to_checksum_address("0xc907E116054Ad103354f2D350FD2514433D57F6f")

# Minimal ABI: only latestRoundData()
AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# Stale threshold: ignore prices older than this
STALE_SECONDS = 120

# Cache TTL: don't spam RPCs
CACHE_TTL = 10


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _normalize(value: float, scale: float = 1.0) -> float:
    return _clamp(math.tanh(value * scale))


class ChainlinkFeed:
    def __init__(self):
        self._prev_price: float | None = None
        self._last_price: float | None = None
        self._last_fetch: float = 0.0

    def fetch(self) -> tuple[float, int] | None:
        """Call latestRoundData(), return (price_usd, updated_at) or None."""
        now = time.time()
        if now - self._last_fetch < CACHE_TTL and self._last_price is not None:
            return None  # Cache hit — caller should use existing state

        from bot.wallet import _get_w3

        try:
            w3 = _get_w3()
            contract = w3.eth.contract(address=AGGREGATOR, abi=AGGREGATOR_ABI)
            _round_id, answer, _started, updated_at, _answered = (
                contract.functions.latestRoundData().call()
            )
            self._last_fetch = now

            # Stale check
            if now - updated_at > STALE_SECONDS:
                logger.debug("Chainlink stale: updated %ds ago", int(now - updated_at))
                return None

            price = answer / 1e8  # 8 decimals
            if price <= 0:
                return None

            # Track previous price for return calculation
            if self._last_price is not None:
                self._prev_price = self._last_price
            self._last_price = price

            return (price, updated_at)

        except Exception as e:
            logger.warning("Chainlink fetch error: %s", e)
            return None

    def signal(self, buf) -> float | None:
        """Compare Chainlink return vs Binance return.

        Returns [-1, 1] signal:
        - Same direction → reinforces (up to ±0.5)
        - Divergence → attenuates toward 0
        """
        self.fetch()  # Update price (respects cache)

        if self._prev_price is None or self._last_price is None:
            return None

        # Chainlink return
        cl_ret = (self._last_price - self._prev_price) / self._prev_price

        # Binance return (1-min)
        bn_ret = buf.ret(1)
        if bn_ret is None:
            return None

        # Agreement: positive if both move same direction
        agreement = cl_ret * bn_ret

        # Scale raw signal from chainlink return
        raw = math.copysign(min(abs(cl_ret), 0.01), cl_ret) * 50

        # Penalize divergence
        if agreement < 0:
            raw *= 0.3

        return _normalize(raw, scale=1.0)
