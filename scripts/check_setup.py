"""Preflight:  python -m scripts.check_setup
Checks every external dependency with one tiny request each, so you find problems before the demo."""
from __future__ import annotations

import asyncio
import sys

from agent.config import get_settings

OK, BAD, SKIP = "PASS", "FAIL", "SKIP"


def line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name}" + (f": {detail}" if detail else ""))


async def main() -> int:
    s = get_settings()
    failed = 0

    # Gemini: test each configured model separately (quotas are per model)
    if not (s.gemini_api_key and s.models_list):
        line(BAD, "Gemini", "set GEMINI_API_KEY and GEMINI_MODELS in .env (see `python -m scripts.list_models`)")
        failed += 1
    else:
        from agent.llm import GeminiClient, LLMError

        for model in s.models_list:
            try:
                res = await GeminiClient(s.gemini_api_key, [model], 40, 200).generate_json("Reply with JSON only.", 'Return exactly {"ok": true}')
                line(OK if res.data.get("ok") else BAD, f"Gemini model {model}", "responded")
            except (LLMError, ValueError) as exc:
                failed += 1
                line(BAD, f"Gemini model {model}", str(exc))

    if not s.tavily_api_key:
        line(BAD, "Tavily", "set TAVILY_API_KEY in .env")
        failed += 1
    else:
        from agent.search import SearchError, TavilySearch

        t = TavilySearch(s.tavily_api_key)
        try:
            hits = await t.search("GitHub OAuth app create", 3)
            line(OK if hits else BAD, "Tavily search", f"{len(hits)} results")
            failed += 0 if hits else 1
        except SearchError as exc:
            failed += 1
            line(BAD, "Tavily search", str(exc))
        finally:
            await t.aclose()

    from agent.fetch import PageFetcher

    f = PageFetcher(s.max_chars_per_page, s.jina_api_key)
    try:
        page = await f.fetch("https://www.rfc-editor.org/rfc/rfc6749")
        line(OK if page.ok else BAD, "Page fetch (Jina / direct)", f"via {page.via}, {len(page.text)} chars" if page.ok else str(page.error))
        failed += 0 if page.ok else 1
    finally:
        await f.aclose()

    if not s.composio_api_key:
        line(SKIP, "Composio managed-auth check", "COMPOSIO_API_KEY not set. Guides will say managed auth is 'unknown'.")
    else:
        from agent.managed import ManagedAuthChecker

        r = await ManagedAuthChecker(s.composio_api_key).check("github")
        line(OK if r.available is not None else BAD, "Composio managed-auth check", r.note)
        failed += 0 if r.available is not None else 1

    print("\nAll good." if not failed else f"\n{failed} problem(s). Fix them before running generate_library or deploying.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
