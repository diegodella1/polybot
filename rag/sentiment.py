"""Periodic sentiment analysis via DuckDuckGo + LLM."""

import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL = 1800  # 30 minutes


class SentimentFetcher:
    def __init__(self):
        self._cache_score: float = 0.0
        self._cache_time: float = 0.0
        self._api_key = os.environ.get("OPENAI_API_KEY", "")

    async def fetch(self) -> float:
        """Fetch BTC sentiment score. Returns cached value if fresh."""
        if time.time() - self._cache_time < CACHE_TTL:
            return self._cache_score

        try:
            snippets = await self._search()
            if not snippets:
                logger.warning("No search results for sentiment")
                return self._cache_score

            score = await self._analyze(snippets)
            self._cache_score = score
            self._cache_time = time.time()
            return score
        except Exception as e:
            logger.error("Sentiment fetch error: %s", e)
            return self._cache_score

    async def _search(self) -> list[str]:
        """Search DuckDuckGo for BTC sentiment."""
        try:
            from duckduckgo_search import DDGS

            results = []
            with DDGS() as ddg:
                for r in ddg.text(
                    "bitcoin BTC price analysis today sentiment",
                    max_results=5,
                ):
                    body = r.get("body", "")
                    if body:
                        results.append(body[:500])

            return results
        except Exception as e:
            logger.error("DuckDuckGo search error: %s", e)
            return []

    async def _analyze(self, snippets: list[str]) -> float:
        """Use LLM to score sentiment from search snippets."""
        if not self._api_key:
            logger.debug("No OpenAI key, skipping sentiment LLM")
            return 0.0

        combined = "\n---\n".join(snippets)
        prompt = (
            "Based on these recent search results about Bitcoin, "
            "score the current BTC market sentiment from -1.0 (very bearish) "
            "to +1.0 (very bullish). Respond with ONLY a JSON object: "
            '{"score": <float>, "reason": "<brief reason>"}\n\n'
            f"Search results:\n{combined}"
        )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 100,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                # Strip code fences if present
                content = content.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    content = "\n".join(
                        l for l in lines if not l.strip().startswith("```")
                    )

                result = json.loads(content)
                score = float(result.get("score", 0))
                reason = result.get("reason", "")
                logger.info("Sentiment: %.2f (%s)", score, reason)
                return max(-1.0, min(1.0, score))

        except Exception as e:
            logger.error("Sentiment LLM error: %s", e)
            return 0.0
