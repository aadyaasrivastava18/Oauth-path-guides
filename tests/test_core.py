import pytest

from agent.assemble import norm, verify_quote
from agent.classify import FlagState, classify
from agent.fetch import focus_text
from agent.llm import parse_json_loose
from agent.net import UnsafeURL, assert_public_url
from agent.pipeline import GuideError, generate_guide
from agent.probe import Prober
from agent.resolve import InputError, parse_query, slugify
from agent.search import SearchError
from schema.models import Duration, Evidence, Guide, Step, fmt_minutes
from tests.conftest import AUTH_URL, DOC_TEXT, DOC_URL, TOKEN_URL, FakeFetcher, FakeLLM, FakeSearch, allow_all, make_services, probe_transport
import httpx


def E(level="docs"):
    return Evidence(level=level)


def test_classify_tiers():
    f = lambda **kw: {k: FlagState(v, E()) for k, v in kw.items()}
    assert classify(f(self_serve_registration=True, review_required_for_production="none")).tier == 1
    assert classify(f(review_required_for_production="optional")).tier == 2
    assert classify(f(review_required_for_production="required", security_assessment_required=True)).tier == 3
    assert classify(f(partner_program_required=True)).tier == 4
    assert classify({}).tier == 0 and classify({}).setup is None


def test_classify_confidence_drops_when_unverified():
    flags = {"partner_program_required": FlagState(True, Evidence(level="inferred"))}
    assert classify(flags).confidence == "low"


def test_quote_verification():
    pages = {"u": norm("Create an **[OAuth app](http://x)** and set the redirect URI before launch.")}
    assert verify_quote("create an OAuth app and set the redirect URI", "u", pages).level == "docs"
    assert verify_quote("invented text that is not there", "u", pages).level == "inferred"
    assert verify_quote(None, None, pages).level == "inferred"
    assert verify_quote("short", None, pages).level == "inferred"


def test_schema_sanitises_urls_and_roundtrips():
    g = Guide(slug="a", app_name="A", steps=[Step(title="t", url="javascript:alert(1)")], setup_effort=Duration(min_minutes=15, max_minutes=90))
    assert g.steps[0].url is None
    assert Guide.model_validate_json(g.model_dump_json()).setup_effort.label == "15 min to 1.5 h"
    assert fmt_minutes(1440 * 3) == "3 days"


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a":1}\n```') == {"a": 1}
    assert parse_json_loose('here {"a": 2} done') == {"a": 2}
    with pytest.raises(ValueError):
        parse_json_loose("no json")
    with pytest.raises(ValueError):
        parse_json_loose("[1,2]")


def test_focus_text_keeps_relevant_passages():
    big = "intro " * 400 + "\n\n" + "\n\n".join(f"filler {i} " * 30 for i in range(80)) + "\n\nCreate an OAuth app and set the redirect URI.\n\n" + "tail " * 500
    out = focus_text(big, 6000)
    assert len(out) <= 6000 and "redirect URI" in out


def test_parse_query_and_slug():
    assert parse_query("HubSpot").name == "HubSpot"
    pq = parse_query("https://developers.hubspot.com/docs")
    assert pq.domain == "hubspot.com" and pq.name == "hubspot"
    for bad in ("", " ", "x" * 200, "!!!", "a\x00b"):
        with pytest.raises(InputError):
            parse_query(bad)
    assert slugify("HubSpot CRM!") == "hubspot-crm"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/", "http://localhost/", "http://10.1.1.1/", "http://[::1]/",
                                  "ftp://example.com/", "https://u:p@example.com/", "https://example.com:8443/", "http://[::ffff:127.0.0.1]/"])
async def test_ssrf_guard_blocks(url):
    with pytest.raises(UnsafeURL):
        await assert_public_url(url)


async def test_pipeline_end_to_end(services):
    events = []

    async def emit(step, msg):
        events.append(step)

    g = await generate_guide("Acme", services, emit)
    assert events == ["resolve", "fetch", "extract", "probe", "assemble"]
    assert services.llm.calls == 1  # quota: exactly one model call per guide
    assert g.status == "ok" and g.slug == "acme" and g.tier == 3
    # real quote -> docs, invented quote -> inferred
    assert g.oauth.authorization_url.evidence.level == "docs"
    sec = [r for r in g.tier_reasons if "security assessment" in r.text][0]
    assert sec.evidence.level == "inferred"
    # unsafe model-provided URL is removed
    assert g.steps[0].url is None
    # probes ran and were recorded as tested
    names = {p.name: p for p in g.probes}
    assert names["Authorization endpoint"].ok and "302" in names["Authorization endpoint"].observed
    assert "invalid_client" in names["Token endpoint"].observed
    assert g.oauth.pkce_supported.evidence.level == "tested"
    assert any("heuristic" in (u.lower()) or "rule-of-thumb" in u.lower() for u in g.uncertainties)
    assert g.approval_wait.evidence.level == "unverified"


async def test_pipeline_no_readable_pages_makes_no_llm_call(tmp_path):
    svc = make_services(tmp_path, fetcher=FakeFetcher(ok=False))
    g = await generate_guide("Acme", svc)
    assert g.status == "insufficient_info" and svc.llm.calls == 0 and g.sources and not g.sources[0].fetched_ok


async def test_pipeline_no_oauth(tmp_path):
    raw = {"app_name": "Acme", "supports_oauth": {"value": False, "quote": "Filler about unrelated topics", "source_url": DOC_URL}, "summary": "No OAuth."}
    g = await generate_guide("Acme", make_services(tmp_path, llm=FakeLLM(raw)))
    assert g.status == "no_oauth" and g.tier == 0 and g.setup_effort is None


async def test_pipeline_survives_garbage_model_output(tmp_path):
    garbage = {"supports_oauth": "maybe", "flags": "nope", "registration_steps": "x", "scopes": [1, None, {"text": 5}], "blockers": {"a": 1}, "drafts": [None]}
    g = await generate_guide("Acme", make_services(tmp_path, llm=FakeLLM(garbage)))
    assert g.status == "insufficient_info" and g.tier == 0


async def test_pipeline_errors_are_user_safe(tmp_path):
    from agent.llm import LLMError
    with pytest.raises(GuideError, match="rate-limited"):
        await generate_guide("Acme", make_services(tmp_path, llm=FakeLLM(error=LLMError("The AI model is rate-limited or busy."))))
    with pytest.raises(GuideError, match="quota"):
        await generate_guide("Acme", make_services(tmp_path, search=FakeSearch(error=SearchError("free quota is used up"))))
    with pytest.raises(GuideError, match="Could not find"):
        await generate_guide("Acme", make_services(tmp_path, search=FakeSearch(hits=[])))


async def test_probe_only_contacts_urls_found_in_docs():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(400, json={"error": "invalid_client"})

    p = Prober(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), url_guard=allow_all)
    out = await p.run("https://evil.example/hallucinated/authorize", TOKEN_URL, DOC_TEXT)
    assert not any("evil.example" in u for u in seen)
    assert any("Not probed" in r.observed for r in out.results)


async def test_probe_blocks_private_hosts():
    called = []
    p = Prober(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: called.append(1) or httpx.Response(200))))
    corpus = "see http://127.0.0.1/oauth/token and http://169.254.169.254/authorize"
    out = await p.run("http://169.254.169.254/authorize", "http://127.0.0.1/oauth/token", corpus)
    assert not called and all(not r.ok for r in out.results)
