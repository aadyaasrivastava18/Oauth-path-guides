"""Build the starter library:  python -m scripts.generate_library [--force] [--delay 8] [names...]
Writes data/guides/<slug>.json. Review each guide before committing it (see VERIFICATION.md)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from agent.config import get_settings
from agent.pipeline import GuideError, generate_guide
from agent.resolve import InputError, parse_query, slugify
from app.main import build_services
from app.store import FileBackend


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="App names or websites. Default: data/seed_apps.txt")
    ap.add_argument("--force", action="store_true", help="Regenerate guides that already exist")
    ap.add_argument("--delay", type=float, default=8.0, help="Seconds between apps (protects free quotas)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    settings = get_settings()
    services = build_services(settings)
    if services is None:
        print("Missing GEMINI_API_KEY, GEMINI_MODELS or TAVILY_API_KEY in .env. Run `python -m scripts.check_setup`.")
        return 2
    names = args.names or [
        l.strip() for l in (settings.data_dir / "seed_apps.txt").read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.lstrip().startswith("#")
    ]
    store = FileBackend(settings.seed_dir)
    failures = 0
    try:
        for i, name in enumerate(names):
            try:
                slug = slugify(parse_query(name).name)
            except InputError as exc:
                print(f"SKIP  {name!r}: {exc}")
                continue
            if (settings.seed_dir / f"{slug}.json").exists() and not args.force:
                print(f"SKIP  {name}: guide exists (use --force to regenerate)")
                continue
            print(f"\n=== {name} ===")

            async def emit(step: str, msg: str) -> None:
                print(f"  [{step}] {msg}")

            try:
                guide = await generate_guide(name, services, emit)
            except (GuideError, InputError) as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
            else:
                guide.slug = slug
                await store.put(guide)
                c = guide.evidence_counts()
                print(f"OK    {name}: status={guide.status} tier={guide.tier} ({guide.tier_label}, {guide.tier_confidence} confidence) "
                      f"evidence={c}")
            if i < len(names) - 1:
                await asyncio.sleep(args.delay)
    finally:
        for obj in (services.fetcher, services.prober, services.searcher):
            close = getattr(obj, "aclose", None)
            if close:
                await close()
    print(f"\nDone. {failures} failure(s). Guides are in {settings.seed_dir}. Review them before committing.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
