"""Discover active BTC Up/Down markets on Polymarket via Gamma API.

Markets are created every 5 or 15 minutes with slug patterns:
  btc-updown-5m-{unix_timestamp}   (aligned to 300s boundaries)
  btc-updown-15m-{unix_timestamp}  (aligned to 900s boundaries)
"""

import json
import logging
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


class Market:
    __slots__ = (
        "condition_id",
        "question",
        "token_up_id",
        "token_down_id",
        "start_time",
        "end_time",
        "active",
        "duration_seconds",
    )

    def __init__(self, condition_id: str, question: str, token_up_id: str,
                 token_down_id: str, start_time: datetime, end_time: datetime,
                 active: bool, duration_seconds: int = 300):
        self.condition_id = condition_id
        self.question = question
        self.token_up_id = token_up_id
        self.token_down_id = token_down_id
        self.start_time = start_time
        self.end_time = end_time
        self.active = active
        self.duration_seconds = duration_seconds

    @property
    def seconds_remaining(self) -> float:
        return (self.end_time - datetime.now(timezone.utc)).total_seconds()

    @property
    def seconds_until_start(self) -> float:
        return (self.start_time - datetime.now(timezone.utc)).total_seconds()

    @property
    def is_in_window(self) -> bool:
        """True if we're inside the market's trading window."""
        now = datetime.now(timezone.utc)
        return self.start_time <= now <= self.end_time


def _extract_tokens(m: dict) -> tuple[str | None, str | None]:
    """Extract (token_up_id, token_down_id) from market data."""
    clob_ids_raw = m.get("clobTokenIds", "[]")
    outcomes_raw = m.get("outcomes", "[]")

    clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

    if len(clob_ids) >= 2 and len(outcomes) >= 2:
        token_up = None
        token_down = None
        for token_id, outcome in zip(clob_ids, outcomes):
            o = outcome.lower()
            if o in ("up", "yes"):
                token_up = token_id
            elif o in ("down", "no"):
                token_down = token_id
        if token_up and token_down:
            return (token_up, token_down)

    # Fallback: tokens[] array
    tokens = m.get("tokens", [])
    if len(tokens) >= 2:
        token_up = None
        token_down = None
        for t in tokens:
            outcome = (t.get("outcome") or "").lower()
            if outcome in ("yes", "up"):
                token_up = t.get("token_id")
            elif outcome in ("no", "down"):
                token_down = t.get("token_id")
        if token_up and token_down:
            return (token_up, token_down)

    return (None, None)


def _duration_from_slug(slug: str) -> int:
    """Extract duration in seconds from a market slug.

    e.g. 'btc-updown-5m-1234' → 300, 'btc-updown-15m-1234' → 900.
    Falls back to computing from start/end if slug doesn't match.
    """
    match = re.search(r"(\d+)m-\d+$", slug)
    if match:
        return int(match.group(1)) * 60
    return 0  # Caller will compute from timestamps


def _parse_market(m: dict, slug: str = "") -> Market | None:
    """Parse a market dict into a Market object."""
    token_up_id, token_down_id = _extract_tokens(m)
    if not token_up_id or not token_down_id:
        return None

    end_str = m.get("endDate") or m.get("end_date_iso", "")
    start_str = m.get("eventStartTime", "")
    if not end_str or not start_str:
        return None

    try:
        end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except ValueError:
        return None

    condition_id = m.get("conditionId") or m.get("condition_id", "")
    if not condition_id:
        return None

    # Determine duration: from slug first, fallback to timestamps
    duration = _duration_from_slug(slug)
    if duration == 0:
        duration = max(int((end_time - start_time).total_seconds()), 300)

    return Market(
        condition_id=condition_id,
        question=m.get("question", ""),
        token_up_id=token_up_id,
        token_down_id=token_down_id,
        start_time=start_time,
        end_time=end_time,
        active=True,
        duration_seconds=duration,
    )


_TIMEFRAME_CFG = {
    "5m": {"interval": 300, "label": "5m"},
    "15m": {"interval": 900, "label": "15m"},
}


async def _fetch_markets_for_timeframe(
    client: httpx.AsyncClient, tf: str, now_ts: int,
) -> list[Market]:
    """Fetch candidate markets for a single timeframe."""
    cfg = _TIMEFRAME_CFG.get(tf)
    if cfg is None:
        return []

    interval = cfg["interval"]
    label = cfg["label"]
    current_window = (now_ts // interval) * interval

    # Check current window and next 2
    slugs = [f"btc-updown-{label}-{current_window + i * interval}" for i in range(3)]
    results: list[Market] = []

    for slug in slugs:
        try:
            resp = await client.get(f"{GAMMA_API}/events", params={"slug": slug})
            resp.raise_for_status()
            events = resp.json()
        except httpx.HTTPError:
            continue

        if not events:
            continue

        for m in events[0].get("markets", []):
            if m.get("closed"):
                continue
            market = _parse_market(m, slug=slug)
            if market is None or market.seconds_remaining <= 0:
                continue
            results.append(market)

    return results


async def discover_markets(market_types: list[str] | None = None) -> list[Market]:
    """Find all active BTC Up/Down markets for the configured timeframes.

    Returns markets sorted by trading priority: in-window markets with
    tau_norm in [0.07, 0.40] first, then shorter timeframes preferred.
    """
    if market_types is None:
        from bot.config import get
        market_types = get("market_types", ["5m"])

    now_ts = int(datetime.now(timezone.utc).timestamp())
    all_markets: list[Market] = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for tf in market_types:
                markets = await _fetch_markets_for_timeframe(client, tf, now_ts)
                all_markets.extend(markets)
    except Exception as e:
        logger.error("Market discovery error: %s", e)

    # Sort: in-window first, then by optimal trading range, then shorter tf
    def _sort_key(m: Market) -> tuple:
        in_window = m.is_in_window
        tau_norm = m.seconds_remaining / m.duration_seconds if m.duration_seconds else 0
        in_optimal = 0.07 <= tau_norm <= 0.40
        return (not in_window, not in_optimal, m.duration_seconds)

    all_markets.sort(key=_sort_key)

    for m in all_markets:
        if m.is_in_window:
            logger.info(
                "Found %ds market: %s (%.0fs remaining) up=%s... down=%s...",
                m.duration_seconds, m.question,
                m.seconds_remaining,
                m.token_up_id[:16], m.token_down_id[:16],
            )
        else:
            logger.debug(
                "Next %ds market: %s (starts in %.0fs)",
                m.duration_seconds, m.question, m.seconds_until_start,
            )

    return all_markets


async def discover_market(market_types: list[str] | None = None) -> Market | None:
    """Find the best available market. Wrapper around discover_markets()."""
    markets = await discover_markets(market_types)
    return markets[0] if markets else None
