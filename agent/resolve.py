"""Turn what the user typed (app name or website) into a ParsedQuery, then rank search hits."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from agent.net import host_of, registrable_domain
from agent.search import Hit


class InputError(ValueError):
    """Message is safe to show to end users."""


@dataclass
class ParsedQuery:
    raw: str
    name: str
    domain: Optional[str] = None
    url: Optional[str] = None


_URL_RE = re.compile(r"^(https?://)\S+$", re.I)
_DOMAIN_RE = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(/\S*)?$", re.I)


def parse_query(raw: str) -> ParsedQuery:
    s = (raw or "").strip()
    if not s:
        raise InputError("Enter an app name or a website address.")
    if len(s) > 120:
        raise InputError("That is too long. Use an app name or a website address.")
    if any(ord(c) < 32 for c in s):
        raise InputError("Use plain text only.")
    if _URL_RE.match(s) or _DOMAIN_RE.match(s):
        url = s if s.lower().startswith(("http://", "https://")) else f"https://{s}"
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            raise InputError("That website address does not look valid.")
        if not host or "." not in host:
            raise InputError("That website address does not look valid.")
        reg = registrable_domain(host)
        return ParsedQuery(raw=s, name=reg.split(".")[0], domain=reg, url=url)
    name = re.sub(r"\s+", " ", s)
    if not re.search(r"[A-Za-z0-9]", name):
        raise InputError("Use letters or numbers in the app name.")
    return ParsedQuery(raw=s, name=name)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-")
    if not slug:
        raise InputError("Use letters or numbers in the app name.")
    return slug


def display_name(parsed: ParsedQuery) -> str:
    return parsed.name if parsed.domain is None else parsed.name.capitalize()


def search_queries(parsed: ParsedQuery) -> list[tuple[str, Optional[list[str]]]]:
    n = parsed.name
    qs: list[tuple[str, Optional[list[str]]]] = [
        (f"{n} OAuth 2.0 register create app developer documentation", None),
        (f"{n} API OAuth app review approval verification requirements production", None),
    ]
    if parsed.domain:
        qs.append((f"{n} OAuth app registration authorization", [parsed.domain]))
    return qs


_LOW_VALUE = ("reddit.com", "quora.com", "youtube.com", "medium.com", "facebook.com", "twitter.com", "x.com", "pinterest.com")
_DEV_HOSTS = ("developer.", "developers.", "docs.", "dev.", "api.", "learn.", "support.", "help.")
_GOOD_PATH = ("oauth", "auth", "authentication", "authorization", "register", "apps", "app-", "credentials", "verification", "review")


def is_likely_official(url: str, parsed: ParsedQuery) -> bool:
    reg = registrable_domain(host_of(url))
    if not reg:
        return False
    if parsed.domain:
        return reg == parsed.domain
    token = re.sub(r"[^a-z0-9]", "", parsed.name.lower())
    return len(token) >= 3 and token in re.sub(r"[^a-z0-9]", "", reg)


def rank_hits(hits: list[Hit], parsed: ParsedQuery, limit: int) -> list[Hit]:
    """Prefer official developer docs about auth; keep at most one third-party page as a cross-check."""
    seen: set[str] = set()
    scored: list[tuple[float, Hit, bool]] = []
    for h in hits:
        key = h.url.split("#")[0].rstrip("/")
        if key in seen or not h.url.lower().startswith(("http://", "https://")):
            continue
        seen.add(key)
        host, path = host_of(h.url), (urlparse(h.url).path or "").lower()
        official = is_likely_official(h.url, parsed)
        score = h.score
        score += 3 if official else 0
        score += 2 if host.startswith(_DEV_HOSTS) else 0
        score += 1.5 if any(p in path for p in _GOOD_PATH) else 0
        score += 1 if "oauth" in h.title.lower() else 0
        score -= 3 if any(host == d or host.endswith("." + d) for d in _LOW_VALUE) else 0
        score -= 4 if path.endswith((".pdf", ".zip", ".png", ".jpg")) else 0
        scored.append((score, h, official))
    scored.sort(key=lambda t: -t[0])
    chosen: list[Hit] = []
    third_party = 0
    for _, h, official in scored:
        if len(chosen) >= limit:
            break
        if not official:
            if third_party >= 1 and len(chosen) >= 2:
                continue
            third_party += 1
        chosen.append(h)
    return chosen
