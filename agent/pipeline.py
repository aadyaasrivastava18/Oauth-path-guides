"""resolve -> fetch -> ONE model call -> verify quotes -> probe -> classify -> assemble."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from urllib.parse import urlparse

from agent.assemble import AssembleInput, _fact, assemble
from agent.config import Settings
from agent.fetch import Page, PageFetcher
from agent.llm import LLMClient, LLMError
from agent.managed import ManagedAuthChecker
from agent.net import host_of, registrable_domain
from agent.probe import ProbeOutcome, Prober
from agent.prompts import SYSTEM, build_prompt
from agent.resolve import InputError, display_name, is_likely_official, parse_query, rank_hits, search_queries, slugify
from agent.search import Hit, SearchError
from schema.models import Guide, Source, clean_url

log = logging.getLogger(__name__)
Emit = Callable[[str, str], Awaitable[None]]


class GuideError(Exception):
    """Message is safe to show to end users."""


@dataclass
class Services:
    settings: Settings
    llm: LLMClient
    searcher: object  # needs: async search(query, max_results, include_domains) -> list[Hit]
    fetcher: PageFetcher
    prober: Prober
    managed: ManagedAuthChecker


async def generate_guide(query: str, services: Services, emit: Optional[Emit] = None) -> Guide:
    async def say(step: str, msg: str) -> None:
        if emit:
            await emit(step, msg)

    parsed = parse_query(query)  # raises InputError
    slug = slugify(parsed.name)
    app = display_name(parsed)
    s = services.settings

    await say("resolve", f"Looking for {app}'s developer documentation")
    managed_task = asyncio.create_task(services.managed.check(parsed.name))
    try:
        results = await asyncio.gather(
            *(services.searcher.search(q, 8, dom) for q, dom in search_queries(parsed)), return_exceptions=True
        )
    except Exception:  # gather with return_exceptions should not raise, but never leak a task
        managed_task.cancel()
        raise
    hits: list[Hit] = []
    errors: list[Exception] = []
    for r in results:
        if isinstance(r, Exception):
            errors.append(r)
        else:
            hits.extend(r)
    if not hits and errors:
        managed_task.cancel()
        first = errors[0]
        raise GuideError(str(first) if isinstance(first, SearchError) else "Search failed. Please try again shortly.")

    chosen = rank_hits(hits, parsed, s.max_pages)
    if parsed.url and parsed.url.rstrip("/") not in {h.url.rstrip("/") for h in chosen}:
        if len(urlparse(parsed.url).path.strip("/")) > 0:
            chosen = [Hit(parsed.url, "", "")] + chosen[: max(s.max_pages - 1, 1)]
    if not chosen:
        managed_task.cancel()
        raise GuideError(f"Could not find developer documentation for '{parsed.raw}'. Try the app's website address instead.")

    await say("fetch", f"Found {len({h.url for h in hits})} results. Reading the {len(chosen)} most relevant pages")
    pages_all: list[Page] = await asyncio.gather(*(services.fetcher.fetch(h.url) for h in chosen))
    sources = [
        Source(url=p.url, title=p.title or h.title, fetched_ok=p.ok, likely_official=is_likely_official(p.url, parsed),
               note="" if p.ok else (p.error or "could not be read"))
        for p, h in zip(pages_all, chosen)
    ]
    pages = [p for p in pages_all if p.ok]
    managed = await managed_task
    official = next((p.url for p in pages if is_likely_official(p.url, parsed)), None)
    website = parsed.url if parsed.domain else (f"https://{registrable_domain(host_of(official))}" if official else None)

    if not pages:
        await say("assemble", "No readable documentation found")
        return assemble(AssembleInput(
            slug=slug, fallback_name=app, website=website, raw={}, pages={}, sources=sources, probes=ProbeOutcome(),
            managed=managed, model="none",
            notes=["None of the pages we found could be read, so no model call was made."],
        ))

    await say("extract", f"Read {len(pages)} of {len(chosen)} pages. Asking the model to extract OAuth requirements (10 to 40 seconds)")
    try:
        llm = await services.llm.generate_json(SYSTEM, build_prompt(app, pages))
    except LLMError as exc:
        raise GuideError(str(exc)) from exc

    corpus = "\n".join(p.text for p in pages)
    raw = llm.data
    auth_u = clean_url(_fact(raw.get("authorization_url"))[0])
    token_u = clean_url(_fact(raw.get("token_url"))[0])
    await say("probe", "Checking quotes against the pages and testing the OAuth endpoints with an invalid client")
    probes = await services.prober.run(auth_u, token_u, corpus)

    await say("assemble", "Classifying difficulty and building the guide")
    guide = assemble(AssembleInput(
        slug=slug, fallback_name=app, website=website, raw=raw, pages={p.url: p.text for p in pages},
        sources=sources, probes=probes, managed=managed, model=llm.model,
    ))
    return guide
