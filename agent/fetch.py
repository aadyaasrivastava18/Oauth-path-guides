"""Fetch a docs page as clean text. Jina Reader first (renders JS-heavy docs), direct fetch as fallback.
Long pages are reduced to the passages most relevant to OAuth setup before they reach the model."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from agent.net import USER_AGENT, UnsafeURL, assert_public_url

log = logging.getLogger(__name__)
JINA_PREFIX = "https://r.jina.ai/"
MAX_DIRECT_BYTES = 1_500_000

KEYWORDS = (
    "oauth", "redirect", "scope", "client id", "client_id", "client secret", "authorize", "access token",
    "review", "verification", "verify", "approval", "approve", "partner", "register", "create an app",
    "create a app", "consent", "security assessment", "audit", "rate limit", "production", "publish",
    "distribution", "marketplace", "public", "private app", "permission", "admin",
)


@dataclass
class Page:
    url: str
    title: str
    text: str
    ok: bool
    error: Optional[str] = None
    via: str = ""


def _chunks(text: str, size: int = 1200):
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if len(block) <= size:
            yield block
            continue
        buf = ""
        for line in block.split("\n"):
            while len(line) > size:
                if buf:
                    yield buf
                    buf = ""
                yield line[:size]
                line = line[size:]
            if len(buf) + len(line) + 1 > size:
                if buf:
                    yield buf
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            yield buf


def focus_text(text: str, limit: int) -> str:
    """Keep the opening of the page plus the highest-scoring OAuth-related passages, in original order."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head_len = min(1500, limit // 3)
    head, rest = text[:head_len], text[head_len:]
    chunks = list(_chunks(rest))
    scored = [(sum(c.lower().count(k) for k in KEYWORDS), i, c) for i, c in enumerate(chunks)]
    budget = limit - head_len - 20
    chosen: set[int] = set()
    used = 0
    for score, i, c in sorted(scored, key=lambda t: (-t[0], t[1])):
        if score == 0:
            break
        if used + len(c) + 2 > budget:
            continue
        chosen.add(i)
        used += len(c) + 2
    if not chosen:
        return text[:limit]
    body = "\n\n".join(chunks[i] for i in sorted(chosen))
    return f"{head}\n\n[... page shortened to the OAuth-relevant passages ...]\n\n{body}"[:limit]


def parse_jina(raw: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"^Title:\s*(.*)$", raw, re.M)
    if m:
        title = m.group(1).strip()
    content = raw.split("Markdown Content:", 1)[1] if "Markdown Content:" in raw else raw
    return title, content.strip()


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text


class PageFetcher:
    def __init__(
        self,
        max_chars: int = 12000,
        jina_api_key: str = "",
        timeout_s: float = 20.0,
        client: Optional[httpx.AsyncClient] = None,
        url_guard: Callable[[str], Awaitable[None]] = assert_public_url,
        use_jina: bool = True,
    ):
        self.max_chars = max_chars
        self._jina_key = jina_api_key
        self._guard = url_guard
        self._use_jina = use_jina
        self._timeout = timeout_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> Page:
        try:
            await self._guard(url)
        except UnsafeURL as exc:
            return Page(url, "", "", False, f"blocked: {exc}")
        if self._use_jina:
            page = await self._via_jina(url)
            if page:
                return page
        return await self._direct(url)

    async def _via_jina(self, url: str) -> Optional[Page]:
        headers = {"Accept": "text/plain", "User-Agent": USER_AGENT}
        if self._jina_key:
            headers["Authorization"] = f"Bearer {self._jina_key}"
        try:
            r = await self._client.get(JINA_PREFIX + url, headers=headers, timeout=30.0)
        except httpx.HTTPError as exc:
            log.info("jina failed for %s: %s", url, exc)
            return None
        if r.status_code != 200 or len(r.text.strip()) < 200:
            return None
        title, content = parse_jina(r.text)
        return Page(url, title[:200], focus_text(content, self.max_chars), True, via="jina")

    async def _direct(self, url: str) -> Page:
        current = url
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}
        try:
            for _ in range(5):
                await self._guard(current)
                async with self._client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return Page(url, "", "", False, "redirect without location")
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code != 200:
                        return Page(url, "", "", False, f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").lower()
                    if "html" not in ctype and "text" not in ctype:
                        return Page(url, "", "", False, f"unsupported content type: {ctype or 'unknown'}")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DIRECT_BYTES:
                            break
                    html = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
                title, text = html_to_text(html)
                if len(text) < 200:
                    return Page(url, title[:200], "", False, "page has too little readable text (it may need JavaScript)")
                return Page(url, title[:200], focus_text(text, self.max_chars), True, via="direct")
            return Page(url, "", "", False, "too many redirects")
        except UnsafeURL as exc:
            return Page(url, "", "", False, f"blocked: {exc}")
        except (httpx.HTTPError, ValueError) as exc:
            return Page(url, "", "", False, f"fetch failed: {type(exc).__name__}")
