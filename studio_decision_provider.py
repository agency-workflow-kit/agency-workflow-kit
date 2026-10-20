#!/usr/bin/env python3
"""Provider-neutral bounded decision_request contract.

Answers one narrow question -- given a well-formed bounded DecisionRequest,
produce provenance-bearing DecisionEvidence -- without binding to any
vendor. External providers (e.g. JEV) plug in behind DecisionProvider.

Honesty rules:
  * Deterministic rules decide first; a provider is never consulted when
    existing evidence already establishes a clear winner or the request is
    malformed / forbidden.
  * Only the closed allow-list of decision classes is accepted.
  * Missing / unsupported / unavailable providers are explicit GAP with
    passed=False -- never a silent success.
  * Malformed or missing evidence never yields a silent decision.
  * Output is decision evidence, not authority: it cannot act as Planner,
    Director, Critic, Operator, security gate, permission authority,
    deployment authority, or release authority.

This module does not perform network I/O and does not import vendor SDKs.
Front Door calls it for bounded decision evidence. It does not import
Front Door. Output remains evidence, not authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence


class DecisionProviderError(RuntimeError):
    """Fail-closed decision contract violation."""


# ---------------------------------------------------------------------------
# Bounded decision classes (mirrors specialist-result.schema.json)
# ---------------------------------------------------------------------------

DECISION_CLASS_CANDIDATE_SELECTION = "candidate_selection"
DECISION_CLASS_RANKING = "ranking"
DECISION_CLASS_CLASSIFICATION = "classification"

DECISION_CLASSES = frozenset({
    DECISION_CLASS_CANDIDATE_SELECTION,
    DECISION_CLASS_RANKING,
    DECISION_CLASS_CLASSIFICATION,
})

# Structurally excluded intents -- never representable as decision_class,
# and also refused when smuggled via authority_role / intent fields.
FORBIDDEN_DECISION_INTENTS = frozenset({
    "factual_verification",
    "source_verification",
    "permission",
    "safety",
    "security",
    "production",
    "destructive_action",
    "critic_verdict",
    "operator_authority",
})

# Roles / authorities a decision provider must never assume.
FORBIDDEN_AUTHORITIES = frozenset({
    "planner",
    "director",
    "critic",
    "operator",
    "security_gate",
    "permission_authority",
    "deployment_authority",
    "release_authority",
})

# Outcome status vocabulary.
STATUS_DECIDED = "DECIDED"
STATUS_FALLBACK = "FALLBACK"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_UNCERTAIN = "UNCERTAIN"
STATUS_ESCALATED = "ESCALATED"
STATUS_GAP = "GAP"
STATUS_REFUSED = "REFUSED"
STATUS_MALFORMED = "MALFORMED"

OUTCOME_STATUSES = frozenset({
    STATUS_DECIDED,
    STATUS_FALLBACK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    STATUS_UNCERTAIN,
    STATUS_ESCALATED,
    STATUS_GAP,
    STATUS_REFUSED,
    STATUS_MALFORMED,
})

PROVIDER_NEUTRAL = "provider-neutral"
PROVIDER_DETERMINISTIC = "deterministic-rules"

DEFAULT_MIN_CONFIDENCE = 0.55


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionProviderError(f"{field_name} must be a non-empty string")
    return value.strip()


# ---------------------------------------------------------------------------
# Structured request / evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionCandidate:
    """One viable candidate in a bounded decision_request."""

    id: str
    summary: str
    # Optional deterministic evaluation hint -- True means hard requirements
    # are already known to be satisfied for this candidate.
    satisfies_hard_requirements: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "summary": self.summary}
        if self.satisfies_hard_requirements is not None:
            payload["satisfies_hard_requirements"] = self.satisfies_hard_requirements
        return payload


@dataclass(frozen=True)
class DecisionRequest:
    """Provider-neutral bounded decision_request.

    Field names align with specialist-result.schema.json's decision_request
    object. Extra optional fields support deterministic-first routing and
    forbidden-authority refusal without widening the wire schema.
    """

    question: str
    decision_class: str
    candidates: tuple[DecisionCandidate, ...]
    hard_requirements: tuple[str, ...]
    fallback: str
    request_id: str | None = None
    # If set to a FORBIDDEN_AUTHORITIES value, the request is refused
    # without consulting any provider.
    authority_role: str | None = None
    # Optional smuggled intent (also checked against forbidden list).
    intent: str | None = None
    # When set and valid, deterministic rules decide without a provider.
    deterministic_winner_id: str | None = None
    timeout_s: float | None = None
    min_confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question": self.question,
            "decision_class": self.decision_class,
            "candidates": [c.as_dict() for c in self.candidates],
            "hard_requirements": list(self.hard_requirements),
            "fallback": self.fallback,
        }
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        if self.authority_role is not None:
            payload["authority_role"] = self.authority_role
        if self.intent is not None:
            payload["intent"] = self.intent
        if self.deterministic_winner_id is not None:
            payload["deterministic_winner_id"] = self.deterministic_winner_id
        if self.timeout_s is not None:
            payload["timeout_s"] = self.timeout_s
        if self.min_confidence is not None:
            payload["min_confidence"] = self.min_confidence
        return payload


@dataclass
class DecisionEvidence:
    """Provenance-bearing decision evidence -- never silent authority."""

    passed: bool
    status: str
    selected_candidate_id: str | None
    ranking: list[str] | None
    confidence: float | None
    uncertainty: float | None
    provenance: dict[str, Any]
    fallback_applied: bool = False
    fallback_description: str | None = None
    escalation_reason: str | None = None
    gap_reason: str | None = None
    provider_id: str | None = None
    notes: list[str] = field(default_factory=list)
    timestamps: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status,
            "selected_candidate_id": self.selected_candidate_id,
            "ranking": list(self.ranking) if self.ranking is not None else None,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "provenance": dict(self.provenance),
            "fallback_applied": self.fallback_applied,
            "fallback_description": self.fallback_description,
            "escalation_reason": self.escalation_reason,
            "gap_reason": self.gap_reason,
            "provider_id": self.provider_id,
            "notes": list(self.notes),
            "timestamps": dict(self.timestamps),
        }


class DecisionProvider(Protocol):
    """Provider plug-in behind the neutral contract."""

    @property
    def provider_id(self) -> str: ...

    def decide(self, request: DecisionRequest) -> DecisionEvidence: ...


# ---------------------------------------------------------------------------
# Builders / parsers
# ---------------------------------------------------------------------------


def _parse_candidate(raw: Mapping[str, Any], index: int) -> DecisionCandidate:
    if not isinstance(raw, Mapping):
        raise DecisionProviderError(f"candidates[{index}] must be an object")
    cid = _require_nonempty_str(raw.get("id"), f"candidates[{index}].id")
    summary = _require_nonempty_str(raw.get("summary"), f"candidates[{index}].summary")
    satisfies = raw.get("satisfies_hard_requirements")
    if satisfies is not None and not isinstance(satisfies, bool):
        raise DecisionProviderError(
            f"candidates[{index}].satisfies_hard_requirements must be bool or absent"
        )
    return DecisionCandidate(id=cid, summary=summary, satisfies_hard_requirements=satisfies)


def parse_decision_request(raw: Mapping[str, Any] | DecisionRequest) -> DecisionRequest:
    """Parse a mapping into DecisionRequest. Raises on malformed input."""
    if isinstance(raw, DecisionRequest):
        return raw
    if not isinstance(raw, Mapping):
        raise DecisionProviderError("decision_request must be a mapping")

    question = _require_nonempty_str(raw.get("question"), "question")
    decision_class = _require_nonempty_str(raw.get("decision_class"), "decision_class")
    fallback = _require_nonempty_str(raw.get("fallback"), "fallback")

    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, Sequence) or isinstance(candidates_raw, (str, bytes)):
        raise DecisionProviderError("candidates must be a non-empty array")
    if len(candidates_raw) < 2:
        raise DecisionProviderError("candidates must contain at least two viable entries")
    candidates = tuple(_parse_candidate(item, i) for i, item in enumerate(candidates_raw))
    ids = [c.id for c in candidates]
    if len(ids) != len(set(ids)):
        raise DecisionProviderError("candidate ids must be unique")

    requirements_raw = raw.get("hard_requirements")
    if not isinstance(requirements_raw, Sequence) or isinstance(requirements_raw, (str, bytes)):
        raise DecisionProviderError("hard_requirements must be a non-empty array")
    if len(requirements_raw) < 1:
        raise DecisionProviderError("hard_requirements must contain at least one requirement")
    hard_requirements = tuple(
        _require_nonempty_str(item, f"hard_requirements[{i}]")
        for i, item in enumerate(requirements_raw)
    )

    request_id = raw.get("request_id")
    if request_id is not None:
        request_id = _require_nonempty_str(request_id, "request_id")

    authority_role = raw.get("authority_role")
    if authority_role is not None:
        authority_role = _require_nonempty_str(authority_role, "authority_role").lower()

    intent = raw.get("intent")
    if intent is not None:
        intent = _require_nonempty_str(intent, "intent").lower()

    deterministic_winner_id = raw.get("deterministic_winner_id")
    if deterministic_winner_id is not None:
        deterministic_winner_id = _require_nonempty_str(
            deterministic_winner_id, "deterministic_winner_id"
        )

    timeout_s = raw.get("timeout_s")
    if timeout_s is not None:
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            raise DecisionProviderError("timeout_s must be a positive number")
        if float(timeout_s) <= 0:
            raise DecisionProviderError("timeout_s must be a positive number")
        timeout_s = float(timeout_s)

    min_confidence = raw.get("min_confidence")
    if min_confidence is not None:
        if not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool):
            raise DecisionProviderError("min_confidence must be a number in [0, 1]")
        min_confidence = float(min_confidence)
        if not 0.0 <= min_confidence <= 1.0:
            raise DecisionProviderError("min_confidence must be a number in [0, 1]")

    return DecisionRequest(
        question=question,
        decision_class=decision_class,
        candidates=candidates,
        hard_requirements=hard_requirements,
        fallback=fallback,
        request_id=request_id,
        authority_role=authority_role,
        intent=intent,
        deterministic_winner_id=deterministic_winner_id,
        timeout_s=timeout_s,
        min_confidence=min_confidence,
    )


def validate_decision_request(request: DecisionRequest) -> None:
    """Validate bounded-decision preconditions. Raises DecisionProviderError."""
    if request.decision_class not in DECISION_CLASSES:
        raise DecisionProviderError(
            f"decision_class must be one of {sorted(DECISION_CLASSES)}, "
            f"got {request.decision_class!r}"
        )
    if len(request.candidates) < 2:
        raise DecisionProviderError("candidates must contain at least two viable entries")
    if len(request.hard_requirements) < 1:
        raise DecisionProviderError("hard_requirements must contain at least one requirement")
    if not request.question.strip():
        raise DecisionProviderError("question must be a non-empty string")
    if not request.fallback.strip():
        raise DecisionProviderError("fallback must be a non-empty string")
    if request.deterministic_winner_id is not None:
        known = {c.id for c in request.candidates}
        if request.deterministic_winner_id not in known:
            raise DecisionProviderError(
                f"deterministic_winner_id {request.deterministic_winner_id!r} "
                "is not among candidates"
            )


def _base_provenance(
    request: DecisionRequest,
    *,
    provider_id: str,
    decided_by: str,
    **extra: Any,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "provider_id": provider_id,
        "decided_by": decided_by,
        "decision_class": request.decision_class,
        "request_id": request.request_id,
        "candidate_ids": [c.id for c in request.candidates],
        "hard_requirements": list(request.hard_requirements),
        "question": request.question,
    }
    provenance.update(extra)
    return provenance


def build_evidence(
    *,
    status: str,
    passed: bool,
    request: DecisionRequest,
    provider_id: str,
    decided_by: str,
    selected_candidate_id: str | None = None,
    ranking: Sequence[str] | None = None,
    confidence: float | None = None,
    uncertainty: float | None = None,
    fallback_applied: bool = False,
    escalation_reason: str | None = None,
    gap_reason: str | None = None,
    notes: Sequence[str] | None = None,
    provenance_extra: Mapping[str, Any] | None = None,
) -> DecisionEvidence:
    """Construct DecisionEvidence. Never invents passed=True for non-DECIDED."""
    status_text = _require_nonempty_str(status, "status")
    if status_text not in OUTCOME_STATUSES:
        raise DecisionProviderError(
            f"status must be one of {sorted(OUTCOME_STATUSES)}, got {status!r}"
        )
    if passed and status_text != STATUS_DECIDED:
        raise DecisionProviderError(
            "passed=True is only allowed for STATUS_DECIDED evidence"
        )
    if status_text == STATUS_DECIDED and selected_candidate_id is None and ranking is None:
        raise DecisionProviderError(
            "DECIDED evidence requires selected_candidate_id or ranking"
        )

    now = _utc_now()
    provenance = _base_provenance(
        request,
        provider_id=provider_id,
        decided_by=decided_by,
        **dict(provenance_extra or {}),
    )
    if gap_reason:
        provenance["explicit_gap"] = gap_reason

    fallback_description = request.fallback if fallback_applied else None
    return DecisionEvidence(
        passed=bool(passed),
        status=status_text,
        selected_candidate_id=selected_candidate_id,
        ranking=list(ranking) if ranking is not None else None,
        confidence=confidence,
        uncertainty=uncertainty,
        provenance=provenance,
        fallback_applied=bool(fallback_applied),
        fallback_description=fallback_description,
        escalation_reason=escalation_reason,
        gap_reason=gap_reason,
        provider_id=provider_id,
        notes=[str(n) for n in (notes or ())],
        timestamps={"started_at": now, "ended_at": now},
    )


def refuse_forbidden_authority(request: DecisionRequest) -> DecisionEvidence | None:
    """Refuse when the request asks the provider to act as a forbidden authority."""
    role = (request.authority_role or "").strip().lower()
    intent = (request.intent or "").strip().lower()
    decision_class = request.decision_class.strip().lower()

    reasons: list[str] = []
    if role in FORBIDDEN_AUTHORITIES:
        reasons.append(f"authority_role={role!r} is forbidden")
    if intent in FORBIDDEN_AUTHORITIES or intent in FORBIDDEN_DECISION_INTENTS:
        reasons.append(f"intent={intent!r} is forbidden")
    if decision_class in FORBIDDEN_DECISION_INTENTS:
        reasons.append(f"decision_class={decision_class!r} is forbidden")

    if not reasons:
        return None

    reason = "; ".join(reasons)
    return build_evidence(
        status=STATUS_REFUSED,
        passed=False,
        request=request,
        provider_id=PROVIDER_NEUTRAL,
        decided_by="forbidden-authority-guard",
        gap_reason=reason,
        notes=[
            "Decision provider refused: must never act as Planner, Director, "
            "Critic, Operator, security gate, permission, deployment, or "
            "release authority."
        ],
        provenance_extra={"refusal": reason, "policy": "forbidden_authority"},
    )


def try_deterministic_decide(request: DecisionRequest) -> DecisionEvidence | None:
    """Decide without an external provider when existing rules suffice.

    Returns DecisionEvidence when a clear deterministic winner exists;
    returns None when the question remains genuinely ambiguous (provider
    may be consulted). Never returns a silent success on missing evidence.
    """
    validate_decision_request(request)

    # Explicit deterministic winner already established upstream.
    if request.deterministic_winner_id is not None:
        return build_evidence(
            status=STATUS_DECIDED,
            passed=True,
            request=request,
            provider_id=PROVIDER_DETERMINISTIC,
            decided_by="deterministic-winner",
            selected_candidate_id=request.deterministic_winner_id,
            confidence=1.0,
            uncertainty=0.0,
            notes=["Deterministic winner already established; provider not consulted."],
            provenance_extra={"rule": "deterministic_winner_id"},
        )

    # Exactly one candidate known to satisfy all hard requirements.
    satisfied = [
        c for c in request.candidates if c.satisfies_hard_requirements is True
    ]
    unsatisfied = [
        c for c in request.candidates if c.satisfies_hard_requirements is False
    ]
    unknown = [
        c for c in request.candidates if c.satisfies_hard_requirements is None
    ]

    if satisfied and not unknown and len(satisfied) == 1:
        winner = satisfied[0]
        return build_evidence(
            status=STATUS_DECIDED,
            passed=True,
            request=request,
            provider_id=PROVIDER_DETERMINISTIC,
            decided_by="hard-requirement-filter",
            selected_candidate_id=winner.id,
            confidence=1.0,
            uncertainty=0.0,
            notes=[
                "Exactly one candidate satisfies hard requirements; "
                "provider not consulted."
            ],
            provenance_extra={
                "rule": "single_satisfies_hard_requirements",
                "eliminated": [c.id for c in unsatisfied],
            },
        )

    if not unknown and len(satisfied) == 0 and unsatisfied:
        # All candidates known to fail -- escalate; do not invent a winner.
        return build_evidence(
            status=STATUS_ESCALATED,
            passed=False,
            request=request,
            provider_id=PROVIDER_DETERMINISTIC,
            decided_by="hard-requirement-filter",
            escalation_reason=(
                "No candidate satisfies the stated hard requirements; "
                "escalate rather than inventing a decision."
            ),
            notes=["Deterministic evaluation exhausted with zero viable candidates."],
            provenance_extra={"rule": "zero_satisfies_hard_requirements"},
        )

    # Ambiguous or incomplete deterministic evidence -- provider may run.
    return None


def apply_fallback(
    request: DecisionRequest,
    *,
    status: str,
    provider_id: str,
    reason: str,
    notes: Sequence[str] | None = None,
    provenance_extra: Mapping[str, Any] | None = None,
) -> DecisionEvidence:
    """Apply the request's declared safe fallback. Always passed=False."""
    if status not in {
        STATUS_FALLBACK,
        STATUS_TIMEOUT,
        STATUS_UNAVAILABLE,
        STATUS_GAP,
        STATUS_UNCERTAIN,
        STATUS_MALFORMED,
    }:
        raise DecisionProviderError(
            f"apply_fallback status must be a non-success outcome, got {status!r}"
        )
    note_list = list(notes or ())
    note_list.append(f"Fallback applied: {request.fallback}")
    return build_evidence(
        status=status,
        passed=False,
        request=request,
        provider_id=provider_id,
        decided_by="fallback",
        fallback_applied=True,
        gap_reason=reason,
        notes=note_list,
        provenance_extra={
            "fallback": request.fallback,
            "reason": reason,
            **dict(provenance_extra or {}),
        },
    )


def gap_evidence(
    request: DecisionRequest,
    *,
    reason: str,
    provider_id: str = PROVIDER_NEUTRAL,
    status: str = STATUS_GAP,
    notes: Sequence[str] | None = None,
    provenance_extra: Mapping[str, Any] | None = None,
) -> DecisionEvidence:
    """Explicit GAP with passed=False -- never a pretend success."""
    return build_evidence(
        status=status,
        passed=False,
        request=request,
        provider_id=provider_id,
        decided_by="gap",
        gap_reason=reason,
        notes=list(notes or ("Explicit capability gap; not a pass.",)),
        provenance_extra=provenance_extra,
    )


def resolve_decision(
    raw_request: Mapping[str, Any] | DecisionRequest,
    *,
    provider: DecisionProvider | None = None,
) -> DecisionEvidence:
    """Provider-neutral entry: validate → refuse → deterministic → provider/GAP.

    Order is fixed:
      1. Parse / validate (malformed → MALFORMED, passed=False)
      2. Forbidden-authority refusal (REFUSED, passed=False)
      3. Deterministic rules (DECIDED / ESCALATED without provider call)
      4. Provider decide(), or GAP if no provider is supplied
    """
    try:
        request = parse_decision_request(raw_request)
        validate_decision_request(request)
    except DecisionProviderError as exc:
        # Build a minimal request shell so evidence remains structured.
        shell = DecisionRequest(
            question="(malformed)",
            decision_class=DECISION_CLASS_CANDIDATE_SELECTION,
            candidates=(
                DecisionCandidate(id="__invalid_a__", summary="invalid"),
                DecisionCandidate(id="__invalid_b__", summary="invalid"),
            ),
            hard_requirements=("malformed-input",),
            fallback="continue with normal reasoning, no degradation",
        )
        return gap_evidence(
            shell,
            reason=str(exc),
            status=STATUS_MALFORMED,
            notes=["Malformed or missing decision_request; no silent decision."],
            provenance_extra={"parse_error": str(exc)},
        )

    refused = refuse_forbidden_authority(request)
    if refused is not None:
        if provider is not None:
            # Stamp the active provider identity onto the refusal so
            # provenance shows which provider path declined the request.
            refused.provider_id = provider.provider_id
            refused.provenance = {
                **refused.provenance,
                "provider_id": provider.provider_id,
            }
        return refused

    deterministic = try_deterministic_decide(request)
    if deterministic is not None:
        return deterministic

    if provider is None:
        return apply_fallback(
            request,
            status=STATUS_GAP,
            provider_id=PROVIDER_NEUTRAL,
            reason="No decision provider supplied; explicit gap, not a pass.",
            notes=["Provider absent — apply declared fallback; do not invent a winner."],
        )

    return provider.decide(request)
