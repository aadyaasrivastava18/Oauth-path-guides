"""Guide storage. Seed guides ship in the repo (data/guides). Live guides go to local files, or to
Supabase when configured (so share links survive redeploys on hosts with ephemeral disks)."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional, Protocol

import httpx

from schema.models import Guide

log = logging.getLogger(__name__)
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug or ""))


class Backend(Protocol):
    async def get(self, slug: str) -> Optional[Guide]: ...
    async def put(self, guide: Guide) -> None: ...


class FileBackend:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._cache: dict[str, tuple[float, Guide]] = {}

    def _path(self, slug: str) -> Path:
        if not valid_slug(slug):
            raise ValueError("invalid slug")
        return self.dir / f"{slug}.json"

    async def get(self, slug: str) -> Optional[Guide]:
        if not valid_slug(slug):
            return None
        path = self._path(slug)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        cached = self._cache.get(slug)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            guide = Guide.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # corrupt file must never crash the site
            log.warning("could not load %s: %s", path, exc)
            return None
        self._cache[slug] = (mtime, guide)
        return guide

    async def put(self, guide: Guide) -> None:
        path = self._path(guide.slug)
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(guide.model_dump_json(indent=2))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def list_all(self) -> list[Guide]:
        out = []
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.json")):
                g = await self.get(p.stem)
                if g:
                    out.append(g)
        return out


class SupabaseBackend:
    """Table: guides(slug text primary key, data jsonb not null, created_at timestamptz default now())."""

    def __init__(self, url: str, service_key: str, client: Optional[httpx.AsyncClient] = None):
        self.base = url.rstrip("/") + "/rest/v1/guides"
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def get(self, slug: str) -> Optional[Guide]:
        if not valid_slug(slug):
            return None
        try:
            r = await self._client.get(self.base, params={"slug": f"eq.{slug}", "select": "data", "limit": "1"}, headers=self.headers)
            if r.status_code != 200:
                log.warning("supabase get -> %s", r.status_code)
                return None
            rows = r.json()
            return Guide.model_validate(rows[0]["data"]) if rows else None
        except Exception as exc:
            log.warning("supabase get failed: %s", exc)
            return None

    async def put(self, guide: Guide) -> None:
        headers = {**self.headers, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}
        r = await self._client.post(self.base, headers=headers, content=json.dumps({"slug": guide.slug, "data": guide.model_dump(mode="json")}))
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"supabase put failed: HTTP {r.status_code}")


class Store:
    """Seeds first (hand-verified wins), then live. put() writes live only."""

    def __init__(self, seed: FileBackend, live: Backend):
        self.seed, self.live = seed, live

    async def get(self, slug: str) -> Optional[Guide]:
        return await self.seed.get(slug) or await self.live.get(slug)

    async def put(self, guide: Guide) -> None:
        try:
            await self.live.put(guide)
        except Exception as exc:  # losing persistence must not lose the user's result
            log.error("could not persist guide %s: %s", guide.slug, exc)
            raise

    async def library(self) -> list[Guide]:
        guides = await self.seed.list_all()
        return sorted(guides, key=lambda g: (g.tier == 0, g.tier, g.app_name.lower()))
