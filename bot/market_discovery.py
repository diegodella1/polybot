"""Discover active BTC 5-min Up/Down markets on Polymarket via Gamma API.

Markets are created every 5 minutes with slug pattern: btc-updown-5m-{unix_timestamp}
where the timestamp is the start of the 5-minute window, aligned to 300-second boundaries.
"""

import json
import logging
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
    )

    def __init__(self, condition_id: str, question: str, token_up_id: str,
                 token_down_id: str, start_time: datetime, end_time: datetime,
                 active: bool):
        self.condition_id = condition_id
        self.question = question
        self.token_up_id = token_up_id
        self.token_down_id = token_down_id
        self.start_time = start_time
        self.end_time = end_time
        self.active = active

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


def _parse_market(m: dict) -> Market | None:
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

    return Market(
        condition_id=condition_id,
        question=m.get("question", ""),
        token_up_id=token_up_id,
        token_down_id=token_down_id,
        start_time=start_time,
        end_time=end_time,
        active=True,
    )


async def discover_market() -> Market | None:
    """Find the current or next active BTC 5-min Up/Down market.

    Constructs slug from current time since these markets follow a predictable
    pattern: btc-updown-5m-{unix_timestamp} every 300 seconds.
    """
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    # Current 5-minute window start
    current_window = (now_ts // 300) * 300

    # Check current window and next 2 windows
    slugs = [f"btc-updown-5m-{current_window + i * 300}" for i in range(3)]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for slug in slugs:
                try:
                    resp = await client.get(
                        f"{GAMMA_API}/events",
                        params={"slug": slug},
                    )
                    resp.raise_for_status()
                    events = resp.json()
                except httpx.HTTPError:
                    continue

                if not events:
                    continue

                for m in events[0].get("markets", []):
                    if m.get("closed"):
                        continue

                    market = _parse_market(m)
                    if market is None:
                        continue

                    if market.seconds_remaining <= 0:
                        continue

                    if market.is_in_window:
                        logger.info(
                            "Found market: %s (%.0fs remaining) up=%s... down=%s...",
                            market.question,
                            market.seconds_remaining,
                            market.token_up_id[:16],
                            market.token_down_id[:16],
                        )
                    else:
                        logger.debug(
                            "Next market: %s (starts in %.0fs)",
                            market.question,
                            market.seconds_until_start,
                        )
                    return market

            return None

    except Exception as e:
        logger.error("Market discovery error: %s", e)
        return None
