"""Rule-based difficulty tier. The model supplies sourced facts; THESE rules decide the tier, so
the same facts always give the same answer and every reason is traceable to a fact + its evidence.

The time ranges below are rule-of-thumb heuristics by tier. Providers rarely publish approval
SLAs, so they are labelled 'unverified' in every guide unless the provider states a time itself."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from schema.models import Claim, Duration, Evidence

TIERS = {
    0: ("Not enough evidence", "The docs we could read did not settle the key questions. Treat everything here as a starting point."),
    1: ("Instant self-serve", "Create the app, copy the credentials, done. No review is described."),
    2: ("Self-serve with a gate", "You can start right away, but something (a review, a verification or a paid plan) gates wider use."),
    3: ("Review-gated", "A formal review or security assessment is needed before real users can connect."),
    4: ("Approval-gated", "You need the provider's approval or a partner program to get access. Plan for weeks and a possible rejection."),
}

MIN, H, D = 1, 60, 1440
# tier -> (setup_min, setup_max, wait_min, wait_max, basis)
HEURISTICS = {
    1: (15 * MIN, 1 * H, 0, 0, "Rule of thumb for self-serve providers."),
    2: (1 * H, 4 * H, 0, 7 * D, "Rule of thumb: quick to start, then a gate whose duration varies widely."),
    3: (4 * H, 3 * D, 3 * D, 6 * 7 * D, "Rule of thumb: preparing the submission (policy, demo, justification) takes most of the effort."),
    4: (1 * D, 5 * D, 14 * D, 90 * D, "Rule of thumb: application effort plus a long, uncertain wait. Rejection is possible."),
}
HEURISTIC_EVIDENCE = Evidence(level="unverified", note="Heuristic by tier, not a provider-published figure.")


@dataclass
class FlagState:
    value: object = None  # bool | str | None
    evidence: Evidence = field(default_factory=lambda: Evidence(level="inferred"))


@dataclass
class TierResult:
    tier: int
    label: str
    confidence: str
    reasons: list[Claim]
    modifiers: list[Claim]
    setup: Optional[Duration]
    wait: Optional[Duration]


def _is(flags: dict[str, FlagState], name: str, expected: object) -> bool:
    f = flags.get(name)
    return f is not None and f.value == expected


def _ev(flags: dict[str, FlagState], name: str) -> Evidence:
    f = flags.get(name)
    return f.evidence if f else Evidence(level="inferred")


def classify(flags: dict[str, FlagState]) -> TierResult:
    review = flags.get("review_required_for_production", FlagState()).value
    partner = _is(flags, "partner_program_required", True)
    biz = _is(flags, "business_verification_required", True)
    sec = _is(flags, "security_assessment_required", True)
    paid = _is(flags, "paid_plan_required", True)
    tenant = _is(flags, "tenant_or_admin_consent_involved", True)

    reasons: list[Claim] = []

    def add(text: str, flag: str) -> None:
        reasons.append(Claim(text=text, evidence=_ev(flags, flag)))

    if partner or (review == "required" and biz):
        tier = 4
        if partner:
            add("A partner or special-access program is required.", "partner_program_required")
        if review == "required":
            add("A review is required before production use.", "review_required_for_production")
        if biz:
            add("Business verification is required.", "business_verification_required")
    elif sec or review == "required":
        tier = 3
        if review == "required":
            add("A review is required before production use.", "review_required_for_production")
        if sec:
            add("A security assessment is required.", "security_assessment_required")
    elif review == "optional" or biz or paid:
        tier = 2
        if review == "optional":
            add("A review is optional or only needed for wider distribution.", "review_required_for_production")
        if biz:
            add("Business verification is required.", "business_verification_required")
        if paid:
            add("A paid plan or account tier may be required.", "paid_plan_required")
    elif any(f.value is not None for f in flags.values()):
        tier = 1
        if _is(flags, "self_serve_registration", True):
            add("Registration is self-serve.", "self_serve_registration")
        if review == "none":
            add("No review is described for production use.", "review_required_for_production")
        if not reasons:
            reasons.append(Claim(text="Nothing in the docs we read describes a review, partner program or paid requirement.", evidence=Evidence(level="inferred")))
    else:
        tier = 0

    modifiers: list[Claim] = []
    if tenant:
        modifiers.append(Claim(text="Admin or tenant consent is involved: each customer organisation may need to approve your app.", evidence=_ev(flags, "tenant_or_admin_consent_involved")))
    if paid and tier != 2:
        modifiers.append(Claim(text="A paid plan or account tier may be required.", evidence=_ev(flags, "paid_plan_required")))

    solid = sum(1 for r in reasons if r.evidence.level in ("docs", "tested"))
    confidence = "high" if solid >= 2 else "medium" if solid == 1 else "low"
    if tier == 0:
        confidence = "low"

    setup = wait = None
    if tier in HEURISTICS:
        s_lo, s_hi, w_lo, w_hi, basis = HEURISTICS[tier]
        setup = Duration(min_minutes=s_lo, max_minutes=s_hi, basis=basis, evidence=HEURISTIC_EVIDENCE)
        wait = Duration(min_minutes=w_lo, max_minutes=w_hi, basis=basis, evidence=HEURISTIC_EVIDENCE)
    return TierResult(tier, TIERS[tier][0], confidence, reasons, modifiers, setup, wait)
