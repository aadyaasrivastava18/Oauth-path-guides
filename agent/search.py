"""Tavily search. Returns plain hits; never raises anything but SearchError."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)
TAVILY_URL = "https://api.tavily.com/search"


class SearchError(Exception):
    """Message is safe to show to end users."""


@dataclass
class Hit:
    url: str
    title: str
    snippet: str
    score: float = 0.0


class TavilySearch:
    def __init__(self, api_key: str, timeout_s: float = 20.0, client: Optional[httpx.AsyncClient] = None):
        if not api_key:
            raise ValueError("TAVILY_API_KEY is missing")
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, max_results: int = 8, include_domains: Optional[list[str]] = None) -> list[Hit]:
        payload: dict = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False}
        if include_domains:
            payload["include_domains"] = include_domains
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                r = await self._client.post(TAVILY_URL, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last = exc
                await asyncio.sleep(0.8)
                continue
            if r.status_code in (401, 403):
                raise SearchError("The server's search API key was rejected. The site owner needs to fix it.")
            if r.status_code == 429 or r.status_code == 432 or r.status_code == 433:
                raise SearchError("The search provider's free quota is used up. Try again later.")
            if r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}")
                await asyncio.sleep(0.8)
                continue
            if r.status_code != 200:
                raise SearchError("The search provider rejected the request.")
            try:
                results = r.json().get("results", [])
            except ValueError as exc:
                raise SearchError("The search provider returned an unreadable response.") from exc
            hits = []
            for item in results if isinstance(results, list) else []:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    hits.append(
                        Hit(
                            url=item["url"],
                            title=str(item.get("title") or "")[:200],
                            snippet=str(item.get("content") or "")[:500],
                            score=float(item.get("score") or 0.0),
                        )
                    )
            return hits
        log.warning("tavily failed: %s", last)
        raise SearchError("The search provider is unreachable right now. Try again shortly.")
