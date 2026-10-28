import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.config import Settings
from agent.fetch import PageFetcher
from agent.llm import GeminiClient, LLMError, parse_json_loose
from agent.net import UnsafeURL, assert_public_url
from app.main import create_app
from app.ratelimit import RateLimiter
from app.store import FileBackend, Store, SupabaseBackend, valid_slug
from schema.models import Guide
from tests.conftest import FakeLLM, make_services


def make_client(tmp_path, services="default", per_ip=4):
    settings = Settings(data_dir=tmp_path, rate_limit_per_ip=per_ip, _env_file=None)
    (tmp_path / "guides").mkdir(exist_ok=True)
    svc = make_services(tmp_path) if services == "default" else services
    return TestClient(create_app(settings=settings, services=svc)), svc


def stream_events(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]


def seed(tmp_path, **kw):
    g = Guide(slug=kw.pop("slug", "demo"), app_name=kw.pop("app_name", "Demo"), **kw)
    (tmp_path / "guides").mkdir(exist_ok=True)
    (tmp_path / "guides" / f"{g.slug}.json").write_text(g.model_dump_json())
    return g


def test_pages_and_headers(tmp_path):
    seed(tmp_path, tier=1, tier_label="Instant self-serve")
    c, _ = make_client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "Demo" in r.text and "Instant self-serve" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert c.get("/guide/demo").status_code == 200
    assert c.get("/methodology").status_code == 200
    assert c.get("/healthz").json()["ok"] is True
    assert c.get("/api/guides/demo").json()["slug"] == "demo"


def test_missing_and_invalid_slugs(tmp_path):
    c, _ = make_client(tmp_path)
    assert c.get("/guide/nope").status_code == 404
    assert c.get("/guide/..%2f..%2fetc%2fpasswd").status_code == 404
    assert c.get("/guide/UPPER_case!").status_code == 404
    assert c.get("/api/guides/nope").status_code == 404
    assert c.get("/does-not-exist").status_code == 404
    assert not valid_slug("../x") and valid_slug("hubspot-crm")


def test_generate_streams_and_saves_then_serves_from_cache(tmp_path):
    c, svc = make_client(tmp_path)
    r = c.post("/api/generate", json={"q": "Acme"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = stream_events(r)
    assert [e["step"] for e in ev if e["type"] == "progress"] == ["resolve", "fetch", "extract", "probe", "assemble"]
    assert ev[-1] == {"type": "done", "slug": "acme", "cached": False}
    page = c.get("/guide/acme")
    assert page.status_code == 200 and "Acme" in page.text
    # same app again: cache hit, no extra model call
    ev2 = stream_events(c.post("/api/generate", json={"q": "acme"}))
    assert ev2 == [{"type": "done", "slug": "acme", "cached": True}] and svc.llm.calls == 1


def test_guide_page_escapes_model_text_and_unsafe_urls(tmp_path):
    c, _ = make_client(tmp_path)
    c.post("/api/generate", json={"q": "Acme"})
    html = c.get("/guide/acme").text
    assert "<script>alert('x')</script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert "Not yet hand-verified" in html and "Tested" in html and "Inferred" in html


def test_generate_validation_errors(tmp_path):
    c, _ = make_client(tmp_path)
    assert c.post("/api/generate", json={"q": ""}).status_code == 422
    assert c.post("/api/generate", json={"q": "x" * 500}).status_code == 422
    assert c.post("/api/generate", content=b"not json").status_code == 400
    assert c.post("/api/generate", content=b"x" * 5000).status_code == 413
    assert c.get("/api/generate").status_code == 405


def test_generate_disabled_without_keys_but_library_works(tmp_path):
    seed(tmp_path)
    c, _ = make_client(tmp_path, services=None)
    r = c.post("/api/generate", json={"q": "Newapp"})
    assert r.status_code == 503 and "not configured" in r.json()["error"]
    assert stream_events(c.post("/api/generate", json={"q": "demo"}))[-1]["cached"] is True
    assert "switched off" in c.get("/").text


def test_rate_limit(tmp_path):
    c, _ = make_client(tmp_path, per_ip=2)
    assert c.post("/api/generate", json={"q": "One"}).status_code == 200
    assert c.post("/api/generate", json={"q": "Two"}).status_code == 200
    r = c.post("/api/generate", json={"q": "Three"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_generation_failure_is_reported_not_crashed(tmp_path):
    svc = make_services(tmp_path, llm=FakeLLM(error=RuntimeError("boom")))
    c, _ = make_client(tmp_path, services=svc)
    ev = stream_events(c.post("/api/generate", json={"q": "Acme"}))
    assert ev[-1]["type"] == "error" and "boom" not in ev[-1]["message"]
    assert c.get("/guide/acme").status_code == 404


def test_rate_limiter_window_and_daily_cap():
    t = [1000.0]
    rl = RateLimiter(per_ip=2, window_s=100, daily_cap=3, clock=lambda: t[0])
    assert rl.check_and_consume("a")[0] and rl.check_and_consume("a")[0]
    ok, msg, retry = rl.check_and_consume("a")
    assert not ok and retry > 0
    t[0] += 101
    assert rl.check_and_consume("a")[0]
    ok, msg, _ = rl.check_and_consume("b")
    assert not ok and "daily limit" in msg
    t[0] += 86400
    assert rl.check_and_consume("b")[0]


async def test_file_backend_corrupt_and_atomic(tmp_path):
    fb = FileBackend(tmp_path)
    (tmp_path / "bad.json").write_text("{not json")
    assert await fb.get("bad") is None
    await fb.put(Guide(slug="ok", app_name="Ok"))
    assert (await fb.get("ok")).app_name == "Ok" and not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        await fb.put(Guide(slug="../evil", app_name="x"))


async def test_supabase_backend_request_shapes():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"data": Guide(slug="s", app_name="S").model_dump(mode="json")}])
        return httpx.Response(201)

    sb = SupabaseBackend("https://x.supabase.co", "key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await sb.get("s")).app_name == "S"
    await sb.put(Guide(slug="s", app_name="S"))
    assert seen[0].url.params["slug"] == "eq.s" and seen[0].headers["apikey"] == "key"
    assert "merge-duplicates" in seen[1].headers["prefer"] and json.loads(seen[1].content)["slug"] == "s"
    assert await sb.get("../bad") is None


async def test_fetcher_blocks_redirect_to_private_host():
    hosts_guarded = []

    async def guard(url):
        hosts_guarded.append(url)
        if "127.0.0.1" in url:
            raise UnsafeURL("private")

    def handler(request):
        if request.url.host == "docs.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(200, text="secret")

    f = PageFetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), url_guard=guard, use_jina=False)
    page = await f.fetch("https://docs.example/oauth")
    assert not page.ok and "blocked" in page.error


async def test_fetcher_direct_extracts_text():
    html = "<html><title>Docs</title><body><nav>menu</nav><p>" + "OAuth redirect URI details. " * 30 + "</p></body></html>"

    async def guard(url):
        return None

    f = PageFetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"}))),
                    url_guard=guard, use_jina=False)
    page = await f.fetch("https://docs.example/oauth")
    assert page.ok and page.title == "Docs" and "menu" not in page.text and "redirect URI" in page.text


class _Boom(Exception):
    pass


async def test_gemini_falls_back_to_next_model_on_rate_limit(monkeypatch):
    from google.genai import errors

    g = GeminiClient("fake-key", ["model-a", "model-b"])
    calls = []

    async def fake_call(model, system, prompt, temperature):
        calls.append(model)
        if model == "model-a":
            raise errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        return '{"ok": true}'

    monkeypatch.setattr(g, "_call", fake_call)
    res = await g.generate_json("s", "p")
    assert calls == ["model-a", "model-b"] and res.model == "model-b" and res.data == {"ok": True}


async def test_gemini_bad_key_and_all_models_failing(monkeypatch):
    from google.genai import errors

    g = GeminiClient("fake-key", ["model-a", "model-b"])

    async def bad_key(model, system, prompt, temperature):
        raise errors.ClientError(403, {"error": {"message": "API key not valid"}})

    monkeypatch.setattr(g, "_call", bad_key)
    with pytest.raises(LLMError, match="key was rejected"):
        await g.generate_json("s", "p")

    async def always_busy(model, system, prompt, temperature):
        raise errors.ServerError(503, {"error": {"message": "overloaded"}})

    monkeypatch.setattr(g, "_call", always_busy)
    with pytest.raises(LLMError, match="rate-limited or busy"):
        await g.generate_json("s", "p")


async def test_gemini_retries_malformed_json_once(monkeypatch):
    g = GeminiClient("fake-key", ["model-a"])
    outputs = iter(["this is not json", '{"fixed": 1}'])

    async def flaky(model, system, prompt, temperature):
        return next(outputs)

    monkeypatch.setattr(g, "_call", flaky)
    assert (await g.generate_json("s", "p")).data == {"fixed": 1}


def test_pages_are_csp_compatible_no_inline_style_or_script(tmp_path):
    import re
    seed(tmp_path, tier=2, tier_label="Self-serve with a gate")
    c, _ = make_client(tmp_path)
    c.post("/api/generate", json={"q": "Acme"})
    for path in ("/", "/guide/demo", "/guide/acme", "/methodology", "/guide/missing"):
        html = c.get(path).text
        assert not re.search(r"<[^>]+\sstyle=", html), f"inline style attribute on {path}"
        assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), f"inline script on {path}"
        assert not re.search(r"\son\w+=", html), f"inline event handler on {path}"
    assert 'class="bar"' in c.get("/guide/acme").text
