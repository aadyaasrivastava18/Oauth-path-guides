"""FastAPI app. Run with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.classify import TIERS
from agent.config import Settings, get_settings
from agent.pipeline import GuideError, Services, generate_guide
from agent.resolve import InputError, parse_query, slugify
from app.ratelimit import RateLimiter
from app.store import FileBackend, Store, SupabaseBackend, valid_slug

log = logging.getLogger("app")
APP_DIR = Path(__file__).resolve().parent
EXAMPLES = ["Notion", "HubSpot", "Zoom", "Asana", "Dropbox", "Shopify"]


def build_services(settings: Settings) -> Optional[Services]:
    """Returns None (live generation disabled, library still works) when keys are missing."""
    if not (settings.gemini_api_key and settings.models_list and settings.tavily_api_key):
        log.warning("Live generation disabled: set GEMINI_API_KEY, GEMINI_MODELS and TAVILY_API_KEY.")
        return None
    from agent.fetch import PageFetcher
    from agent.llm import GeminiClient
    from agent.managed import ManagedAuthChecker
    from agent.probe import Prober
    from agent.search import TavilySearch

    return Services(
        settings=settings,
        llm=GeminiClient(settings.gemini_api_key, settings.models_list, settings.llm_timeout_s, settings.llm_max_output_tokens),
        searcher=TavilySearch(settings.tavily_api_key, settings.http_timeout_s),
        fetcher=PageFetcher(settings.max_chars_per_page, settings.jina_api_key, settings.http_timeout_s),
        prober=Prober(),
        managed=ManagedAuthChecker(settings.composio_api_key),
    )


def create_app(settings: Optional[Settings] = None, services: Optional[Services] = None, store: Optional[Store] = None) -> FastAPI:
    settings = settings or get_settings()
    if services is None:
        services = build_services(settings)
    if store is None:
        live = SupabaseBackend(settings.supabase_url, settings.supabase_service_key) if (settings.supabase_url and settings.supabase_service_key) else FileBackend(settings.live_dir)
        store = Store(FileBackend(settings.seed_dir), live)

    app = FastAPI(title="OAuth Path Guides", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    limiter = RateLimiter(settings.rate_limit_per_ip, settings.rate_limit_window_s, settings.daily_generation_cap)
    gen_sem = asyncio.Semaphore(settings.max_concurrent_generations)
    locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        return resp

    def client_ip(request: Request) -> str:
        if settings.trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()[:64] or "unknown"
        return request.client.host if request.client else "unknown"

    def render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
        ctx = {"live_enabled": services is not None, "examples": EXAMPLES, **ctx}
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return render(request, "index.html", {"guides": await store.library(), "tiers": TIERS})

    @app.get("/guide/{slug}", response_class=HTMLResponse)
    async def guide_page(request: Request, slug: str):
        guide = await store.get(slug) if valid_slug(slug) else None
        if not guide:
            return render(request, "notfound.html", {"slug": slug[:60]}, 404)
        return render(request, "guide.html", {"guide": guide, "tiers": TIERS})

    @app.get("/methodology", response_class=HTMLResponse)
    async def methodology(request: Request):
        return render(request, "methodology.html", {"tiers": TIERS})

    @app.get("/api/guides/{slug}")
    async def guide_json(slug: str):
        guide = await store.get(slug) if valid_slug(slug) else None
        if not guide:
            return JSONResponse({"error": "Guide not found."}, status_code=404)
        return JSONResponse(guide.model_dump(mode="json"))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "live_generation": services is not None}

    # ------------------------------------------------------------------ live generation
    def sse(item: dict) -> str:
        return f"data: {json.dumps(item)}\n\n"

    def sse_response(gen) -> StreamingResponse:
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.post("/api/generate")
    async def generate(request: Request):
        raw = await request.body()
        if len(raw) > 4096:
            return JSONResponse({"error": "Request too large."}, status_code=413)
        try:
            q = str(json.loads(raw or b"{}").get("q", ""))
        except (ValueError, AttributeError):
            return JSONResponse({"error": "Send JSON like {\"q\": \"HubSpot\"}."}, status_code=400)
        try:
            parsed = parse_query(q)
            slug = slugify(parsed.name)
        except InputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        if await store.get(slug):
            async def cached():
                yield sse({"type": "done", "slug": slug, "cached": True})
            return sse_response(cached())

        if services is None:
            return JSONResponse({"error": "Live generation is not configured on this server. The pre-built guides still work."}, status_code=503)

        ok, msg, retry = limiter.check_and_consume(client_ip(request))
        if not ok:
            return JSONResponse({"error": msg}, status_code=429, headers={"Retry-After": str(retry)})

        if len(locks) > 500:  # bound memory: forget idle locks
            for k in [k for k, v in locks.items() if not v.locked()]:
                locks.pop(k, None)
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(step: str, message: str) -> None:
            await queue.put({"type": "progress", "step": step, "message": message})

        async def run() -> None:
            try:
                async with locks[slug]:
                    if await store.get(slug):  # someone else finished it while we waited
                        await queue.put({"type": "done", "slug": slug, "cached": True})
                        return
                    if gen_sem.locked():
                        await emit("queue", "Waiting for other guides to finish")
                    async with gen_sem:
                        guide = await asyncio.wait_for(generate_guide(q, services, emit), timeout=settings.total_timeout_s)
                        guide.slug = slug
                        await store.put(guide)
                await queue.put({"type": "done", "slug": slug, "cached": False})
            except (InputError, GuideError) as exc:
                await queue.put({"type": "error", "message": str(exc)})
            except asyncio.TimeoutError:
                await queue.put({"type": "error", "message": "This took too long and was stopped. Try again, or try the app's website address."})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("generation failed for %r", q)
                await queue.put({"type": "error", "message": "Something went wrong while building this guide. Please try again."})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        async def stream():
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                yield sse(item)

        return sse_response(stream())

    # ------------------------------------------------------------------ errors
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail if isinstance(exc.detail, str) else "Error"}, status_code=exc.status_code)
        if exc.status_code == 404:
            return render(request, "notfound.html", {"slug": ""}, 404)
        return HTMLResponse("Something went wrong.", status_code=exc.status_code)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for t in list(tasks):
            t.cancel()
        if services:
            for obj in (services.fetcher, services.prober, services.searcher):
                close = getattr(obj, "aclose", None)
                if close:
                    await close()

    return app
