"""Credential-free probes. We send ONE request per endpoint using a deliberately invalid client,
so nothing here can create, modify or authenticate anything at the provider.

An endpoint is only probed if its URL literally appears in the docs we fetched (so a hallucinated
URL is never contacted) and it passes the SSRF guard."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from agent.net import USER_AGENT, UnsafeURL, assert_public_url
from schema.models import Evidence, ProbeResult

log = logging.getLogger(__name__)

PROBE_CLIENT_ID = "oauth-path-guides-probe"
PROBE_REDIRECT = "https://example.invalid/callback"
WELL_KNOWN = ("/.well-known/openid-configuration", "/.well-known/oauth-authorization-server")


@dataclass
class ProbeOutcome:
    results: list[ProbeResult] = field(default_factory=list)
    pkce: Optional[bool] = None
    discovery: dict = field(default_factory=dict)


def _tested(url: str) -> Evidence:
    return Evidence(level="tested", source_url=url, note="Request sent with a deliberately invalid client. No credentials were used.")


def _with_params(url: str, extra: dict[str, str]) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.update(extra)
    return urlunparse(p._replace(query=urlencode(q)))


def _reachable(status: Optional[int]) -> bool:
    return status is not None and status < 500 and status not in (404, 405, 410)


def _in_corpus(url: str, corpus: str) -> bool:
    u = url.strip().rstrip("/")
    return bool(u) and u in corpus


class Prober:
    def __init__(
        self,
        timeout_s: float = 8.0,
        client: Optional[httpx.AsyncClient] = None,
        url_guard: Callable[[str], Awaitable[None]] = assert_public_url,
    ):
        self._client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
        self._guard = url_guard
        self._timeout = timeout_s

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run(self, authorization_url: Optional[str], token_url: Optional[str], corpus: str) -> ProbeOutcome:
        out = ProbeOutcome()
        auth = authorization_url if authorization_url and _in_corpus(authorization_url, corpus) else None
        token = token_url if token_url and _in_corpus(token_url, corpus) else None
        if authorization_url and not auth:
            out.results.append(
                ProbeResult(name="Authorization endpoint", url=authorization_url, ok=False,
                            observed="Not probed: this URL was not found verbatim in the docs we read.",
                            evidence=Evidence(level="unverified"))
            )
        if token_url and not token:
            out.results.append(
                ProbeResult(name="Token endpoint", url=token_url, ok=False,
                            observed="Not probed: this URL was not found verbatim in the docs we read.",
                            evidence=Evidence(level="unverified"))
            )
        if auth:
            out.results.append(await self._probe_authorize(auth))
        if token:
            out.results.append(await self._probe_token(token))
        origins: list[str] = []
        for u in (auth, token):
            if u:
                p = urlparse(u)
                o = f"{p.scheme}://{p.netloc}"
                if o not in origins:
                    origins.append(o)
        if origins:
            await self._discover(origins[:2], auth, token, out)
        return out

    async def _probe_authorize(self, url: str) -> ProbeResult:
        name = "Authorization endpoint"
        target = _with_params(url, {"response_type": "code", "client_id": PROBE_CLIENT_ID, "redirect_uri": PROBE_REDIRECT, "state": "probe"})
        try:
            await self._guard(url)
            r = await self._client.get(target, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
        except UnsafeURL as exc:
            return ProbeResult(name=name, url=url, ok=False, observed=f"Not probed: {exc}.", evidence=Evidence(level="unverified"))
        except httpx.HTTPError as exc:
            return ProbeResult(name=name, url=url, ok=False, observed=f"No response ({type(exc).__name__}).", evidence=_tested(url))
        loc = r.headers.get("location")
        loc_host = urlparse(loc).netloc if loc else ""
        observed = f"HTTP {r.status_code}"
        if loc_host:
            observed += f", redirects to {loc_host}"
        ctype = r.headers.get("content-type", "").split(";")[0]
        if ctype:
            observed += f" ({ctype})"
        observed += ". Endpoint is live." if _reachable(r.status_code) else ". Endpoint did not behave like a live authorization page."
        return ProbeResult(name=name, url=url, ok=_reachable(r.status_code), observed=observed, evidence=_tested(url))

    async def _probe_token(self, url: str) -> ProbeResult:
        name = "Token endpoint"
        data = {"grant_type": "authorization_code", "code": "invalid-code", "client_id": PROBE_CLIENT_ID,
                "client_secret": "invalid", "redirect_uri": PROBE_REDIRECT}
        try:
            await self._guard(url)
            r = await self._client.post(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        except UnsafeURL as exc:
            return ProbeResult(name=name, url=url, ok=False, observed=f"Not probed: {exc}.", evidence=Evidence(level="unverified"))
        except httpx.HTTPError as exc:
            return ProbeResult(name=name, url=url, ok=False, observed=f"No response ({type(exc).__name__}).", evidence=_tested(url))
        observed = f"HTTP {r.status_code}"
        err = None
        try:
            body = r.json()
            if isinstance(body, dict):
                e = body.get("error")
                err = e if isinstance(e, str) else (e.get("type") or e.get("code") or e.get("message") if isinstance(e, dict) else None)
                if err is None and body.get("ok") is False and isinstance(body.get("error"), str):
                    err = body["error"]
        except ValueError:
            pass
        if err:
            observed += f", error = {str(err)[:60]}"
            if r.status_code in (400, 401, 403):
                observed += " (standard OAuth-style rejection of an invalid client)"
        elif _reachable(r.status_code):
            observed += ", no machine-readable error code"
        observed += ". Endpoint is live." if _reachable(r.status_code) else ". Endpoint did not accept a POST."
        return ProbeResult(name=name, url=url, ok=_reachable(r.status_code), observed=observed, evidence=_tested(url))

    async def _discover(self, origins: list[str], auth: Optional[str], token: Optional[str], out: ProbeOutcome) -> None:
        for origin in origins:
            for path in WELL_KNOWN:
                url = origin + path
                try:
                    await self._guard(url)
                    r = await self._client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                except (UnsafeURL, httpx.HTTPError):
                    continue
                if r.status_code != 200:
                    continue
                try:
                    doc = json.loads(r.text)
                except ValueError:
                    continue
                if not isinstance(doc, dict) or not (doc.get("authorization_endpoint") or doc.get("token_endpoint")):
                    continue
                out.discovery = doc
                parts = ["Discovery document found."]
                for label, key, ours in (("authorization_endpoint", "authorization_endpoint", auth), ("token_endpoint", "token_endpoint", token)):
                    theirs = doc.get(key)
                    if isinstance(theirs, str) and ours:
                        same = theirs.rstrip("/") == ours.rstrip("/")
                        parts.append(f"{label} {'matches the docs' if same else 'DIFFERS from the docs: ' + theirs[:120]}.")
                methods = doc.get("code_challenge_methods_supported")
                if isinstance(methods, list):
                    out.pkce = "S256" in methods
                    parts.append("PKCE S256 " + ("is advertised." if out.pkce else "is not advertised."))
                out.results.append(ProbeResult(name="Discovery document", url=url, ok=True, observed=" ".join(parts), evidence=_tested(url)))
                return
        out.results.append(
            ProbeResult(name="Discovery document", url=None, ok=False,
                        observed="None found at the standard /.well-known locations (many providers do not publish one).",
                        evidence=Evidence(level="tested", note="Checked the two standard /.well-known paths."))
        )
