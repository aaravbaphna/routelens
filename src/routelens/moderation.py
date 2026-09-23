"""A small, self-contained client for OpenAI's moderation endpoint.

Independent of chainroute's `ModerationGuard` on purpose -- these are two separate projects
(see the READMEs), so RouteLens's moderation support doesn't require installing or depending on
chainroute at all. If you already run chainroute's `ModerationGuard`, you don't need this too;
this exists for RouteLens users who want moderation *and* the dashboard from one package.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

MODERATIONS_URL = "https://api.openai.com/v1/moderations"


class ModerationResult:
    __slots__ = ("flagged", "categories", "scores", "latency_ms", "error")

    def __init__(self, flagged: bool = False, categories: Optional[List[str]] = None,
                 scores: Optional[Dict[str, float]] = None, latency_ms: float = 0.0,
                 error: Optional[str] = None) -> None:
        self.flagged = flagged
        self.categories = categories or []
        self.scores = scores or {}
        self.latency_ms = latency_ms
        self.error = error


class ModerationChecker:
    """Stateless per call except for a small cache of identical text. One instance is shared
    across every request; `check()` is safe to call concurrently."""

    def __init__(self, api_key: Optional[str] = None, model: str = "omni-moderation-latest",
                 base_url: str = MODERATIONS_URL, timeout_s: float = 2.0, cache_size: int = 2000) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        self._cache: "OrderedDict[str, ModerationResult]" = OrderedDict()
        self._cache_size = cache_size
        self._client: Any = None  # an httpx.AsyncClient, created lazily on first use

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def check(self, text: str) -> ModerationResult:
        if not text.strip():
            return ModerationResult(flagged=False)
        key = hashlib.sha256(text.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        result = await self._call(text)
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    async def _call(self, text: str) -> ModerationResult:
        import asyncio
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient()
        start = time.time()
        try:
            resp = await asyncio.wait_for(
                self._client.post(self.base_url,
                                   headers={"Authorization": "Bearer %s" % self.api_key},
                                   json={"input": text, "model": self.model}),
                timeout=self.timeout_s)
            resp.raise_for_status()
        except Exception as e:
            return ModerationResult(error="%s: %s" % (type(e).__name__, e), latency_ms=(time.time() - start) * 1000)
        latency_ms = (time.time() - start) * 1000
        try:
            result = resp.json()["results"][0]
        except Exception as e:
            return ModerationResult(error="unexpected response shape: %r" % e, latency_ms=latency_ms)
        categories = [c for c, flagged in (result.get("categories") or {}).items() if flagged]
        scores = {c: round(float(s), 4) for c, s in (result.get("category_scores") or {}).items()}
        return ModerationResult(flagged=bool(result.get("flagged")), categories=categories,
                                 scores=scores, latency_ms=latency_ms)
