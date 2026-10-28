"""Turn the model's raw JSON + probe results + rule-based classification into a Guide.

Key anti-hallucination step: a fact only gets the 'docs' label if its quote really appears in a page
we fetched. Otherwise it is downgraded to 'inferred'."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agent.classify import TIERS, FlagState, classify
from agent.managed import DIRECTORY_URL
from agent.probe import ProbeOutcome
from schema.models import (
    Blocker, Claim, Draft, Evidence, Fact, Guide, ManagedAuth, OAuthDetails, ProbeResult, Source, Step, clean_url,
)

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_NOISE = re.compile(r"[*_`#>|~\\]")
MAX_ITEMS = 8


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = _MD_LINK.sub(r"\1", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = _MD_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def verify_quote(quote: Optional[str], source_url: Optional[str], pages_norm: dict[str, str]) -> Evidence:
    q = (quote or "").strip()
    if not q:
        return Evidence(level="inferred", note="The model gave no supporting quote.")
    frags = [f for f in re.split(r"\.{3}|\u2026", q) if len(norm(f)) >= 12]
    if not frags:
        return Evidence(level="inferred", note="The supporting quote was too short to check.")
    ordered = ([source_url] if source_url in pages_norm else []) + [u for u in pages_norm if u != source_url]
    for url in ordered:
        if all(norm(f) in pages_norm[url] for f in frags):
            return Evidence(level="docs", source_url=url, quote=q[:300])
    return Evidence(level="inferred", note="The quoted text was not found in the pages we fetched, so the docs do not confirm this.")


def _s(v: Any) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _tri(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "yes"):
            return True
        if t in ("false", "no"):
            return False
    return None


def _fact(x: Any) -> tuple[Any, Optional[str], Optional[str]]:
    if isinstance(x, dict):
        return x.get("value"), _s(x.get("quote")), _s(x.get("source_url"))
    return x, None, None


def _items(x: Any) -> list[dict]:
    out = []
    for e in x if isinstance(x, list) else []:
        if isinstance(e, dict):
            out.append(e)
        elif isinstance(e, str) and e.strip():
            out.append({"text": e})
    return out[:MAX_ITEMS]


def _strs(x: Any) -> list[str]:
    out = []
    for e in x if isinstance(x, list) else []:
        s = e if isinstance(e, str) else (e.get("text") if isinstance(e, dict) else None)
        if isinstance(s, str) and s.strip():
            out.append(s.strip())
    return out[:MAX_ITEMS]


def _review_value(v: Any) -> Optional[str]:
    if isinstance(v, bool):
        return "required" if v else "none"
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("none", "optional", "required"):
            return t
        if t in ("no", "false"):
            return "none"
        if t in ("yes", "true"):
            return "required"
    return None


@dataclass
class AssembleInput:
    slug: str
    fallback_name: str
    website: Optional[str]
    raw: dict[str, Any]
    pages: dict[str, str]  # url -> text the model saw
    sources: list[Source]
    probes: ProbeOutcome
    managed: ManagedAuth
    model: str
    notes: list[str] = field(default_factory=list)


def _claims(raw_items: list[dict], pages_norm: dict[str, str], text_key: str = "text") -> list[Claim]:
    out = []
    for it in raw_items:
        text = _s(it.get(text_key))
        if text:
            out.append(Claim(text=text, evidence=verify_quote(_s(it.get("quote")), _s(it.get("source_url")), pages_norm)))
    return out


def _own_app_claims(managed: ManagedAuth, app: str) -> tuple[Claim, Optional[Claim]]:
    reasons = Claim(
        text="Register your own OAuth app when you want your app name on the consent screen, custom scopes, or a separate rate-limit quota.",
        evidence=Evidence(level="docs", source_url=DIRECTORY_URL,
                          quote="You can also provide your own OAuth app to show your app name on the consent screen, request custom scopes, or use a separate rate-limit quota.",
                          note="Composio docs, read while building this tool."),
    )
    if managed.available is True:
        return Claim(text=f"Not to get started. Composio can provide the OAuth app for {app}, so your users can connect without you registering one.", evidence=managed.evidence), reasons
    if managed.available is False:
        return Claim(text=f"Yes. Composio has no managed OAuth app for {app}, so you register your own and create a custom auth config with its client ID and secret.", evidence=managed.evidence), None
    return Claim(text=f"Unknown. Check whether {app} is listed as managed in Composio's OAuth directory. If it is not, you register your own OAuth app.", evidence=managed.evidence), reasons


def assemble(inp: AssembleInput) -> Guide:
    raw = inp.raw
    pages_norm = {u: norm(t) for u, t in inp.pages.items()}
    ver = lambda q, u: verify_quote(q, u, pages_norm)  # noqa: E731

    app_name = _s(raw.get("app_name")) or inp.fallback_name
    if len(app_name) > 60:
        app_name = inp.fallback_name

    # ---- OAuth basics -------------------------------------------------------------------------
    def url_fact(key: str) -> Optional[Fact]:
        value, quote, src = _fact(raw.get(key))
        u = clean_url(value)
        return Fact(value=u, evidence=ver(quote, src)) if u else None

    oauth = OAuthDetails(
        authorization_url=url_fact("authorization_url"),
        token_url=url_fact("token_url"),
        registration_url=url_fact("registration_url"),
        redirect_uri_rules=_claims(_items(raw.get("redirect_uri_rules")), pages_norm),
        scopes=_claims(_items(raw.get("scopes")), pages_norm),
    )
    sa_value, sa_q, sa_src = _fact(raw.get("minimal_scope_advice"))
    sa_text = _s(raw.get("minimal_scope_advice", {}).get("text")) if isinstance(raw.get("minimal_scope_advice"), dict) else None
    if sa_text:
        oauth.scope_advice = Claim(text=sa_text, evidence=ver(sa_q, sa_src))

    # PKCE: a live discovery document beats a doc quote.
    pk_value, pk_q, pk_src = _fact(raw.get("pkce_supported"))
    if inp.probes.pkce is not None:
        oauth.pkce_supported = Fact(value="yes" if inp.probes.pkce else "not advertised",
                                    evidence=Evidence(level="tested", note="From the provider's discovery document."))
    elif _tri(pk_value) is not None:
        oauth.pkce_supported = Fact(value="yes" if _tri(pk_value) else "no", evidence=ver(pk_q, pk_src))

    # ---- Flags and classification -------------------------------------------------------------
    flags_raw = raw.get("flags") if isinstance(raw.get("flags"), dict) else {}
    flags: dict[str, FlagState] = {}
    stated_time: Optional[Claim] = None
    for name in ("self_serve_registration", "review_required_for_production", "partner_program_required",
                 "business_verification_required", "security_assessment_required", "paid_plan_required",
                 "tenant_or_admin_consent_involved", "stated_review_time"):
        value, quote, src = _fact(flags_raw.get(name))
        if name == "stated_review_time":
            txt = _s(value)
            if txt:
                stated_time = Claim(text=f"The provider states: {txt}", evidence=ver(quote, src))
            continue
        v = _review_value(value) if name == "review_required_for_production" else _tri(value)
        if v is not None:
            flags[name] = FlagState(v, ver(quote, src))

    s_value, s_quote, s_src = _fact(raw.get("supports_oauth"))
    supports = _tri(s_value)
    result = classify(flags)

    # ---- Steps, blockers and the rest ---------------------------------------------------------
    steps = []
    for it in _items(raw.get("registration_steps")):
        title = _s(it.get("title"))
        if not title:
            continue
        actor = it.get("actor") if it.get("actor") in ("agent", "human", "either") else "human"
        steps.append(Step(title=title, detail=_s(it.get("detail")) or "", url=_s(it.get("url")), actor=actor,
                          evidence=ver(_s(it.get("quote")), _s(it.get("source_url")))))
    # ---- Status (decided from VALIDATED facts, never from raw model output) -------------------
    has_any_oauth_fact = bool(oauth.authorization_url or oauth.token_url or oauth.registration_url or steps)
    status, status_note = "ok", ""
    if supports is False:
        status = "no_oauth"
        status_note = "The docs we read say this app does not support OAuth 2.0 (or only supports other auth methods)."
    elif supports is None and not has_any_oauth_fact:
        status = "insufficient_info"
        status_note = "We could not find enough in the docs to describe an OAuth setup for this app."

    blockers = []
    for it in _items(raw.get("blockers")):
        problem = _s(it.get("problem"))
        if problem:
            blockers.append(Blocker(problem=problem, what_to_do=_s(it.get("what_to_do")) or "",
                                    evidence=ver(_s(it.get("quote")), _s(it.get("source_url")))))

    synth = Evidence(level="inferred", note="Model suggestion based on the fetched docs.")
    done = Evidence(level="tested", note="Done in this run.")
    agent_can = [Claim(text="Found and read the provider's developer docs (see Sources).", evidence=done)]
    if any(p.evidence.level == "tested" and p.ok for p in inp.probes.results):
        agent_can.append(Claim(text="Sent test requests to the OAuth endpoints with an invalid client to confirm they are live.", evidence=done))
    agent_can += [Claim(text=t, evidence=synth) for t in _strs(raw.get("agent_can"))[:5]]
    human_must = [Claim(text="Sign in to the provider's developer console and accept its terms. Agents should not hold your login.", evidence=synth)]
    human_must += [Claim(text=t, evidence=synth) for t in _strs(raw.get("human_must"))[:6]]

    drafts = []
    for it in _items(raw.get("drafts"))[:2]:
        title, body = _s(it.get("title")), _s(it.get("body"))
        if title and body:
            drafts.append(Draft(title=title, body=body))

    uncertainties = _strs(raw.get("uncertainties"))
    unconfirmed = sum(1 for e in _all_evidence_of_claims(oauth, steps, blockers) if e.level == "inferred")
    if unconfirmed:
        uncertainties.append(f"{unconfirmed} statements below are labelled 'Inferred': the model's quote could not be found in the pages we read.")
    if result.setup:
        uncertainties.append("Time ranges are rule-of-thumb estimates for this difficulty tier. Providers rarely publish approval times, and we did not measure any.")
    uncertainties += inp.notes

    need_own, own_reasons = _own_app_claims(inp.managed, app_name)
    if status != "ok":
        need_own = Claim(text="Not determined. The docs we read do not describe an OAuth app setup for this app.", evidence=Evidence(level="inferred"))
        own_reasons = None
    approval_reqs = _claims(_items(raw.get("review_requirements")), pages_norm)
    rejection = _claims(_items(raw.get("rejection_reasons")), pages_norm)
    prereqs = _claims(_items(raw.get("prerequisites")), pages_norm)

    guide = Guide(
        slug=inp.slug,
        app_name=app_name,
        provider_name=_s(raw.get("provider_name")) or app_name,
        website=inp.website,
        status=status,  # type: ignore[arg-type]
        status_note=status_note,
        summary=_s(raw.get("summary")) or "",
        tier=result.tier if status == "ok" else 0,
        tier_label=result.label if status == "ok" else TIERS[0][0],
        tier_confidence=result.confidence if status == "ok" else "low",  # type: ignore[arg-type]
        tier_reasons=result.reasons if status == "ok" else [],
        modifiers=result.modifiers if status == "ok" else [],
        setup_effort=result.setup if status == "ok" else None,
        approval_wait=result.wait if status == "ok" else None,
        approval_wait_stated=stated_time,
        managed_auth=inp.managed,
        need_own_app=need_own,
        own_app_reasons=own_reasons,
        oauth=oauth,
        prerequisites=prereqs,
        steps=steps,
        approval_requirements=approval_reqs,
        rejection_reasons=rejection,
        blockers=blockers,
        agent_can=agent_can,
        human_must=human_must,
        drafts=drafts,
        next_actions=_strs(raw.get("next_actions")),
        probes=inp.probes.results,
        sources=inp.sources,
        uncertainties=uncertainties[:12],
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        generator=f"{inp.model} + rule-based tiering v1",
    )
    return guide


def _all_evidence_of_claims(oauth: OAuthDetails, steps: list[Step], blockers: list[Blocker]) -> list[Evidence]:
    evs = [c.evidence for c in oauth.scopes + oauth.redirect_uri_rules] + [s.evidence for s in steps] + [b.evidence for b in blockers]
    return evs
