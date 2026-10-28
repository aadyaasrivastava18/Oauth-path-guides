import json
from pathlib import Path

import httpx
import pytest

from agent.config import Settings
from agent.fetch import Page
from agent.llm import LLMError, LLMResult
from agent.managed import ManagedAuthChecker
from agent.pipeline import Services
from agent.probe import Prober
from agent.search import Hit

DOC_URL = "https://developers.acme.example/docs/oauth"
AUTH_URL = "https://auth.acme.example/oauth/authorize"
TOKEN_URL = "https://auth.acme.example/oauth/token"
DOC_TEXT = f"""# Acme OAuth

To connect users, create an app in the developer console at https://developers.acme.example/apps.
Your app must register at least one redirect URI over HTTPS.
Send users to {AUTH_URL} and exchange the code at {TOKEN_URL}.
Apps that request restricted scopes must pass a security review before production use.
""" + ("Filler about unrelated topics. " * 20)

RAW = {
    "app_name": "Acme",
    "provider_name": "Acme Inc",
    "supports_oauth": {"value": True, "quote": "create an app in the developer console", "source_url": DOC_URL},
    "authorization_url": {"value": AUTH_URL, "quote": f"Send users to {AUTH_URL}", "source_url": DOC_URL},
    "token_url": {"value": TOKEN_URL, "quote": f"exchange the code at {TOKEN_URL}", "source_url": DOC_URL},
    "registration_url": {"value": "https://developers.acme.example/apps", "quote": "developer console at https://developers.acme.example/apps", "source_url": DOC_URL},
    "flags": {
        "self_serve_registration": {"value": True, "quote": "create an app in the developer console", "source_url": DOC_URL},
        "review_required_for_production": {"value": "required", "quote": "must pass a security review before production use", "source_url": DOC_URL},
        "security_assessment_required": {"value": True, "quote": "THIS QUOTE WAS INVENTED BY THE MODEL", "source_url": DOC_URL},
    },
    "registration_steps": [
        {"title": "Create the app", "detail": "Open the console.", "url": "javascript:alert(1)", "actor": "human",
         "quote": "create an app in the developer console", "source_url": DOC_URL},
    ],
    "redirect_uri_rules": [{"text": "Register an HTTPS redirect URI.", "quote": "register at least one redirect URI over HTTPS", "source_url": DOC_URL}],
    "blockers": [{"problem": "<script>alert('x')</script> Review stalls", "what_to_do": "Contact support.", "quote": None, "source_url": None}],
    "agent_can": ["Draft the scope justification"],
    "human_must": ["Submit the review form"],
    "drafts": [{"title": "Scope justification", "body": "We need [SCOPE] because [REASON]."}],
    "next_actions": ["Create the app", "Draft your privacy policy"],
    "summary": "Self-serve to start, but a review gates production.",
    "uncertainties": ["Review duration is not stated."],
}


class FakeLLM:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data if data is not None else RAW, error, 0

    async def generate_json(self, system, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        assert "UNTRUSTED" in system and DOC_URL in prompt
        return LLMResult(json.loads(json.dumps(self.data)), "fake-model")


class FakeSearch:
    def __init__(self, hits=None, error=None):
        self.hits = hits if hits is not None else [Hit(DOC_URL, "Acme OAuth", "", 1.0)]
        self.error, self.queries = error, []

    async def search(self, query, max_results=8, include_domains=None):
        self.queries.append(query)
        if self.error:
            raise self.error
        return list(self.hits)


class FakeFetcher:
    def __init__(self, text=DOC_TEXT, ok=True):
        self.text, self.ok = text, ok

    async def fetch(self, url):
        return Page(url, "Acme OAuth", self.text if self.ok else "", self.ok, None if self.ok else "HTTP 404", "fake")


async def allow_all(url):
    return None


def probe_transport(token_status=400):
    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if request.url.path.endswith("/oauth/authorize"):
            return httpx.Response(302, headers={"location": "https://auth.acme.example/login"})
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(token_status, json={"error": "invalid_client"})
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"authorization_endpoint": AUTH_URL, "token_endpoint": TOKEN_URL, "code_challenge_methods_supported": ["S256"]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_services(tmp_path: Path, llm=None, search=None, fetcher=None, transport=None) -> Services:
    settings = Settings(data_dir=tmp_path, _env_file=None)
    prober = Prober(client=httpx.AsyncClient(transport=transport or probe_transport()), url_guard=allow_all)
    return Services(settings, llm or FakeLLM(), search or FakeSearch(), fetcher or FakeFetcher(), prober, ManagedAuthChecker(""))


@pytest.fixture
def services(tmp_path):
    return make_services(tmp_path)
