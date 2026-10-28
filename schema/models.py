"""The guide schema. The pipeline produces these; the UI only renders them.

Every factual statement carries an Evidence object saying how well it is supported:
  tested     - we observed it ourselves (live request, API call)
  docs       - the model quoted it and we confirmed the quote appears in a page we fetched
  inferred   - model synthesis or an unconfirmed quote; plausible but not confirmed
  unverified - a heuristic or placeholder; do not rely on it
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Iterator, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, computed_field

EvidenceLevel = Literal["tested", "docs", "inferred", "unverified"]
Actor = Literal["agent", "human", "either"]
GuideStatus = Literal["ok", "no_oauth", "insufficient_info"]
Confidence = Literal["high", "medium", "low"]


def clean_url(value: Any) -> Optional[str]:
    """Only absolute http(s) URLs survive; anything else (javascript:, data:, junk) becomes None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 2000:
        return None
    try:
        p = urlparse(v)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return v


def _one_line(limit: int):
    def f(value: Any) -> str:
        if value is None:
            return ""
        s = value if isinstance(value, str) else str(value)
        return " ".join(s.split())[:limit]

    return f


def _multi_line(limit: int):
    def f(value: Any) -> str:
        if value is None:
            return ""
        s = value if isinstance(value, str) else str(value)
        return s.strip()[:limit]

    return f


def _opt_line(limit: int):
    inner = _one_line(limit)

    def f(value: Any) -> Optional[str]:
        s = inner(value)
        return s or None

    return f


Url = Annotated[Optional[str], BeforeValidator(clean_url)]
Short = Annotated[str, BeforeValidator(_one_line(300))]
Medium = Annotated[str, BeforeValidator(_one_line(900))]
Body = Annotated[str, BeforeValidator(_multi_line(4000))]
OptShort = Annotated[Optional[str], BeforeValidator(_opt_line(320))]


class Evidence(BaseModel):
    level: EvidenceLevel = "unverified"
    source_url: Url = None
    quote: OptShort = None
    note: OptShort = None


class Claim(BaseModel):
    text: Medium
    evidence: Evidence = Field(default_factory=Evidence)


def _plural(n: float, unit: str) -> str:
    return f"{n:g} {unit}" if n == 1 else f"{n:g} {unit}s"


def fmt_minutes(m: int) -> str:
    if m < 60:
        return f"{m} min"
    if m < 1440:
        return f"{round(m / 60, 1):g} h"
    d = m / 1440
    if d < 14:
        return _plural(round(d, 1), "day")
    if d < 70:
        return _plural(round(d / 7), "week")
    return _plural(round(d / 30), "month")


class Duration(BaseModel):
    min_minutes: int = Field(0, ge=0)
    max_minutes: int = Field(0, ge=0)
    basis: Medium = ""
    evidence: Evidence = Field(default_factory=Evidence)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def label(self) -> str:
        lo, hi = self.min_minutes, max(self.max_minutes, self.min_minutes)
        if hi == 0:
            return "None expected"
        if lo == hi:
            return fmt_minutes(lo)
        return f"{fmt_minutes(lo)} to {fmt_minutes(hi)}"


class Fact(BaseModel):
    value: Short
    evidence: Evidence = Field(default_factory=Evidence)


class ManagedAuth(BaseModel):
    available: Optional[bool] = None  # None = unknown
    note: Medium = ""
    evidence: Evidence = Field(default_factory=Evidence)


class Step(BaseModel):
    title: Short
    detail: Medium = ""
    url: Url = None
    actor: Actor = "human"
    evidence: Evidence = Field(default_factory=Evidence)


class Blocker(BaseModel):
    problem: Medium
    what_to_do: Medium = ""
    evidence: Evidence = Field(default_factory=Evidence)


class Draft(BaseModel):
    title: Short
    body: Body
    evidence: Evidence = Field(default_factory=lambda: Evidence(level="inferred", note="AI-drafted template. Review and edit before using."))


class ProbeResult(BaseModel):
    name: Short
    url: Url = None
    ok: bool = False
    observed: Medium = ""
    evidence: Evidence = Field(default_factory=lambda: Evidence(level="tested"))


class Source(BaseModel):
    url: Url = None
    title: Short = ""
    fetched_ok: bool = False
    likely_official: bool = False
    note: Short = ""


class OAuthDetails(BaseModel):
    authorization_url: Optional[Fact] = None
    token_url: Optional[Fact] = None
    registration_url: Optional[Fact] = None
    pkce_supported: Optional[Fact] = None
    redirect_uri_rules: list[Claim] = Field(default_factory=list)
    scopes: list[Claim] = Field(default_factory=list)
    scope_advice: Optional[Claim] = None


class Guide(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    slug: str
    app_name: Short
    provider_name: Short = ""
    website: Url = None
    status: GuideStatus = "ok"
    status_note: Medium = ""
    summary: Medium = ""

    tier: int = Field(0, ge=0, le=4)
    tier_label: Short = "Not enough evidence"
    tier_confidence: Confidence = "low"
    tier_reasons: list[Claim] = Field(default_factory=list)
    modifiers: list[Claim] = Field(default_factory=list)
    setup_effort: Optional[Duration] = None
    approval_wait: Optional[Duration] = None
    approval_wait_stated: Optional[Claim] = None

    managed_auth: ManagedAuth = Field(default_factory=ManagedAuth)
    need_own_app: Optional[Claim] = None
    own_app_reasons: Optional[Claim] = None

    oauth: OAuthDetails = Field(default_factory=OAuthDetails)
    prerequisites: list[Claim] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    approval_requirements: list[Claim] = Field(default_factory=list)
    rejection_reasons: list[Claim] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)

    agent_can: list[Claim] = Field(default_factory=list)
    human_must: list[Claim] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    next_actions: list[Short] = Field(default_factory=list)

    probes: list[ProbeResult] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    uncertainties: list[Medium] = Field(default_factory=list)

    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    generator: Short = ""
    hand_verified: bool = False  # set by a human after checking against real sources

    def evidence_counts(self) -> dict[str, int]:
        counts = {"tested": 0, "docs": 0, "inferred": 0, "unverified": 0}
        for ev in _walk(self):
            counts[ev.level] += 1
        return counts

    def evidence_coverage(self) -> float:
        """Share of evidence-bearing statements that are tested or confirmed by docs (0..1)."""
        c = self.evidence_counts()
        total = sum(c.values())
        return (c["tested"] + c["docs"]) / total if total else 0.0


def _walk(obj: Any) -> Iterator[Evidence]:
    if isinstance(obj, Evidence):
        yield obj
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from _walk(getattr(obj, name))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk(item)
