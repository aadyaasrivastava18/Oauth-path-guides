"""Does Composio provide the OAuth app for this toolkit? Uses the documented SDK call
`composio.toolkits.get(slug).composio_managed_auth_schemes` (docs.composio.dev/toolkits/managed-auth)."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from schema.models import Evidence, ManagedAuth

log = logging.getLogger(__name__)
DIRECTORY_URL = "https://docs.composio.dev/toolkits/managed-auth"


def candidate_slugs(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9]+", "", name.lower())
    alt = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    out = []
    for s in (base, alt):
        if s and s not in out:
            out.append(s)
    return out[:2]


def _schemes(toolkit: Any) -> list[str]:
    raw = getattr(toolkit, "composio_managed_auth_schemes", None)
    if raw is None and isinstance(toolkit, dict):
        raw = toolkit.get("composio_managed_auth_schemes")
    return [str(getattr(s, "value", s)) for s in (raw or [])]


class ManagedAuthChecker:
    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self._cache: dict[str, ManagedAuth] = {}

    def _lookup_sync(self, slug: str) -> list[str]:
        from composio import Composio

        return _schemes(Composio(api_key=self.api_key).toolkits.get(slug))

    async def check(self, name: str) -> ManagedAuth:
        unknown = ManagedAuth(
            available=None,
            note="Not checked automatically. Look the app up in Composio's managed OAuth directory.",
            evidence=Evidence(level="unverified", source_url=DIRECTORY_URL),
        )
        if not self.api_key:
            return unknown
        key = name.lower().strip()
        if key in self._cache:
            return self._cache[key]
        result = unknown
        for slug in candidate_slugs(name):
            try:
                schemes = await asyncio.wait_for(asyncio.to_thread(self._lookup_sync, slug), timeout=15)
            except Exception as exc:  # SDK raises its own types; any failure just means "unknown"
                log.info("composio lookup failed for %s: %s", slug, type(exc).__name__)
                continue
            oauth = [s for s in schemes if "OAUTH" in s.upper()]
            ev = Evidence(level="tested", source_url=DIRECTORY_URL, note=f"Live Composio toolkit lookup for '{slug}'.")
            if oauth:
                result = ManagedAuth(available=True, note=f"Composio manages the OAuth app for this toolkit ({', '.join(oauth)}).", evidence=ev)
            else:
                result = ManagedAuth(available=False, note="Composio reports no managed OAuth scheme for this toolkit, so you register your own OAuth app.", evidence=ev)
            break
        self._cache[key] = result
        return result
