#!/usr/bin/env python3
"""JEV-backed DecisionProvider behind the provider-neutral contract.

Implements studio_decision_provider.DecisionProvider for TypeSafe JEV
(System One). The authenticated network call is never made from this
module's unit-test path: callers MUST inject a transport. A missing
transport is an explicit GAP with passed=False -- never a pretend success.

Standing orders:
  * Deterministic-first: never invoke transport when existing rules decide.
  * Only bounded decision classes from the neutral contract.
  * Timeout / unavailable / uncertain / malformed → fallback or escalate
    with passed=False; never silent success.
  * JEV must never act as Planner, Director, Critic, Operator, security
    gate, permission authority, deployment authority, or release authority.
  * Unsupported / unavailable JEV is GAP with passed=False.

Front Door calls this module for advisory decisions. This module does
not import Front Door or Factory. JEV remains advisory evidence and is
not Planner, Director, Critic, Operator, or release authority.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from studio_decision_provider import (
    DEFAULT_MIN_CONFIDENCE,
    DecisionEvidence,
    DecisionProviderError,
    DecisionRequest,
    STATUS_DECIDED,
    STATUS_ESCALATED,
    STATUS_FALLBACK,
    STATUS_GAP,
    STATUS_MALFORMED,
    STATUS_REFUSED,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    STATUS_UNCERTAIN,
    apply_fallback,
    build_evidence,
    gap_evidence,
    refuse_forbidden_authority,
    try_deterministic_decide,
    validate_decision_request,
)


PROVIDER_ID_JEV = "jev"
DEFAULT_TIMEOUT_S = 5.0


class JevTransportError(RuntimeError):
    """Transport-level failure (unavailable, protocol error, etc.)."""


class JevTransport(Protocol):
    """Injected callable surface for JEV. Unit tests supply a fake.

    Implementations MUST NOT be required for import of this module.
    ``invoke`` raises TimeoutError on timeout and JevTransportError (or
    ConnectionError / OSError) when the service is unavailable.
    """

    def invoke(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_s: float,
    ) -> Mapping[str, Any]: ...


def _candidate_ids(request: DecisionRequest) -> list[str]:
    return [c.id for c in request.candidates]


def _build_transport_payload(request: DecisionRequest) -> dict[str, Any]:
    """Structured, secret-free payload for the injected transport."""
    return {
        "kind": "bounded_decision",
        "decision_class": request.decision_class,
        "question": request.question,
        "candidates": [c.as_dict() for c in request.candidates],
        "hard_requirements": list(request.hard_requirements),
        "request_id": request.request_id,
        # Explicitly declare that JEV is decision evidence only.
        "authority": "decision_evidence_only",
        "forbidden_roles": sorted({
            "planner",
            "director",
            "critic",
            "operator",
            "security_gate",
            "permission_authority",
            "deployment_authority",
            "release_authority",
        }),
    }


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _normalize_transport_result(
    raw: Any,
    *,
    request: DecisionRequest,
) -> dict[str, Any]:
    """Normalize a transport mapping into a structured internal result.

    Raises DecisionProviderError when the result is malformed.
    """
    if not isinstance(raw, Mapping):
        raise DecisionProviderError(
            f"JEV transport returned non-mapping result: {type(raw).__name__}"
        )

    known = set(_candidate_ids(request))
    action = raw.get("action")
    if action is not None:
        if not isinstance(action, str) or not action.strip():
            raise DecisionProviderError("JEV result action must be a non-empty string")
        action = action.strip().lower()

    selected = raw.get("selected_candidate_id")
    if selected is None:
        selected = raw.get("choice")
    if selected is not None:
        if not isinstance(selected, str) or not selected.strip():
            raise DecisionProviderError(
                "JEV result selected_candidate_id/choice must be a non-empty string"
            )
        selected = selected.strip()
        if selected not in known:
            raise DecisionProviderError(
                f"JEV selected unknown candidate_id {selected!r}"
            )

    ranking = raw.get("ranking")
    if ranking is not None:
        if not isinstance(ranking, Sequence) or isinstance(ranking, (str, bytes)):
            raise DecisionProviderError("JEV result ranking must be an array of ids")
        ranking_list = []
        for item in ranking:
            if not isinstance(item, str) or not item.strip():
                raise DecisionProviderError("JEV ranking entries must be non-empty strings")
            cid = item.strip()
            if cid not in known:
                raise DecisionProviderError(f"JEV ranking references unknown id {cid!r}")
            ranking_list.append(cid)
        if len(ranking_list) != len(set(ranking_list)):
            raise DecisionProviderError("JEV ranking contains duplicate ids")
        ranking = ranking_list

    confidence = _as_float(raw.get("confidence"))
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise DecisionProviderError("JEV confidence must be in [0, 1]")

    uncertainty = _as_float(raw.get("uncertainty"))
    if uncertainty is None and confidence is not None:
        uncertainty = max(0.0, 1.0 - confidence)
    if uncertainty is not None and not 0.0 <= uncertainty <= 1.0:
        raise DecisionProviderError("JEV uncertainty must be in [0, 1]")

    reason = raw.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise DecisionProviderError("JEV reason must be a non-empty string when present")
    if isinstance(reason, str):
        reason = reason.strip()

    # Require a decidable signal, escalate/uncertain action, or explicit error.
    if action is None and selected is None and ranking is None:
        raise DecisionProviderError(
            "JEV result missing selected_candidate_id/choice, ranking, and action"
        )

    # Ranking/selection consistency: when both are present, selection must
    # equal ranking[0]. Divergence is malformed, never a silent DECIDED.
    if selected is not None and ranking is not None and ranking and selected != ranking[0]:
        raise DecisionProviderError(
            f"JEV selected_candidate_id {selected!r} inconsistent with "
            f"ranking[0]={ranking[0]!r}"
        )

    # Normalize forbidden authority intents/tokens if the transport smuggled them.
    for key in ("authority_role", "intent", "authority"):
        token = raw.get(key)
        if isinstance(token, str) and token.strip():
            lowered = token.strip().lower()
            if lowered in {
                "planner",
                "director",
                "critic",
                "operator",
                "security_gate",
                "permission_authority",
                "deployment_authority",
                "release_authority",
                "permission",
                "safety",
                "security",
                "production",
                "destructive_action",
                "critic_verdict",
                "operator_authority",
                "factual_verification",
                "source_verification",
            }:
                raise DecisionProviderError(
                    f"JEV result smuggled forbidden authority token {key}={token!r}"
                )

    return {
        "action": action,
        "selected_candidate_id": selected,
        "ranking": ranking,
        "confidence": confidence,
        "uncertainty": uncertainty,
        "reason": reason,
        "raw_keys": sorted(str(k) for k in raw.keys()),
    }


class JevDecisionProvider:
    """JEV DecisionProvider. Transport is required for any live call."""

    def __init__(
        self,
        transport: JevTransport | None = None,
        *,
        default_timeout_s: float = DEFAULT_TIMEOUT_S,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        if default_timeout_s <= 0:
            raise DecisionProviderError("default_timeout_s must be positive")
        if not 0.0 <= min_confidence <= 1.0:
            raise DecisionProviderError("min_confidence must be in [0, 1]")
        self._transport = transport
        self._default_timeout_s = float(default_timeout_s)
        self._min_confidence = float(min_confidence)

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID_JEV

    @property
    def transport(self) -> JevTransport | None:
        return self._transport

    def decide(self, request: DecisionRequest) -> DecisionEvidence:
        """Resolve a bounded DecisionRequest via JEV, or gap/fallback/refuse."""
        validate_decision_request(request)

        refused = refuse_forbidden_authority(request)
        if refused is not None:
            # Re-stamp provider_id so provenance shows which provider refused.
            refused.provider_id = PROVIDER_ID_JEV
            refused.provenance = {
                **refused.provenance,
                "provider_id": PROVIDER_ID_JEV,
            }
            return refused

        deterministic = try_deterministic_decide(request)
        if deterministic is not None:
            return deterministic

        if self._transport is None:
            return gap_evidence(
                request,
                reason=(
                    "JEV transport not configured; explicit GAP with passed=False "
                    "(never a pretend success)."
                ),
                provider_id=PROVIDER_ID_JEV,
                status=STATUS_GAP,
                notes=[
                    "Missing transport is a capability gap; inject a fake in tests "
                    "or a real transport at INTEGRATION time."
                ],
                provenance_extra={"transport": None, "capability": "GAP"},
            )

        timeout_s = (
            float(request.timeout_s)
            if request.timeout_s is not None
            else self._default_timeout_s
        )
        min_confidence = (
            float(request.min_confidence)
            if request.min_confidence is not None
            else self._min_confidence
        )

        payload = _build_transport_payload(request)
        try:
            raw_result = self._transport.invoke(payload, timeout_s=timeout_s)
        except TimeoutError as exc:
            return apply_fallback(
                request,
                status=STATUS_TIMEOUT,
                provider_id=PROVIDER_ID_JEV,
                reason=f"JEV transport timed out after {timeout_s}s: {exc}",
                notes=["Timeout — apply declared fallback; do not invent a winner."],
                provenance_extra={
                    "timeout_s": timeout_s,
                    "transport_error": "TimeoutError",
                },
            )
        except (JevTransportError, ConnectionError, OSError) as exc:
            return apply_fallback(
                request,
                status=STATUS_UNAVAILABLE,
                provider_id=PROVIDER_ID_JEV,
                reason=f"JEV transport unavailable: {exc}",
                notes=["Unavailable — apply declared fallback; do not invent a winner."],
                provenance_extra={
                    "transport_error": type(exc).__name__,
                    "capability": "GAP",
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            return gap_evidence(
                request,
                reason=f"JEV transport raised unexpected {type(exc).__name__}: {exc}",
                provider_id=PROVIDER_ID_JEV,
                status=STATUS_GAP,
                notes=["Unexpected transport failure; explicit gap, not a pass."],
                provenance_extra={"transport_error": type(exc).__name__},
            )

        try:
            normalized = _normalize_transport_result(raw_result, request=request)
        except DecisionProviderError as exc:
            return apply_fallback(
                request,
                status=STATUS_MALFORMED,
                provider_id=PROVIDER_ID_JEV,
                reason=f"Malformed JEV result: {exc}",
                notes=[
                    "Malformed JEV result — no silent decision; apply fallback."
                ],
                provenance_extra={
                    "parse_error": str(exc),
                    "raw_type": type(raw_result).__name__,
                },
            )

        action = normalized["action"]
        if action in {"escalate", "escalated", "refuse", "refused"}:
            return build_evidence(
                status=STATUS_ESCALATED if action.startswith("escalat") else STATUS_REFUSED,
                passed=False,
                request=request,
                provider_id=PROVIDER_ID_JEV,
                decided_by="jev-transport",
                confidence=normalized["confidence"],
                uncertainty=normalized["uncertainty"],
                escalation_reason=normalized["reason"]
                or f"JEV action={action!r}",
                notes=[
                    "JEV requested escalation/refusal; output remains evidence only."
                ],
                provenance_extra={
                    "action": action,
                    "transport_keys": normalized["raw_keys"],
                },
            )

        if action in {"uncertain", "unknown", "gap"}:
            return apply_fallback(
                request,
                status=STATUS_UNCERTAIN if action != "gap" else STATUS_GAP,
                provider_id=PROVIDER_ID_JEV,
                reason=normalized["reason"] or f"JEV reported action={action!r}",
                notes=["JEV uncertain/gap — apply fallback; not a pass."],
                provenance_extra={
                    "action": action,
                    "confidence": normalized["confidence"],
                    "uncertainty": normalized["uncertainty"],
                    "transport_keys": normalized["raw_keys"],
                },
            )

        confidence = normalized["confidence"]
        if confidence is not None and confidence < min_confidence:
            return build_evidence(
                status=STATUS_ESCALATED,
                passed=False,
                request=request,
                provider_id=PROVIDER_ID_JEV,
                decided_by="jev-confidence-gate",
                selected_candidate_id=normalized["selected_candidate_id"],
                ranking=normalized["ranking"],
                confidence=confidence,
                uncertainty=normalized["uncertainty"],
                escalation_reason=(
                    f"JEV confidence {confidence} below min_confidence "
                    f"{min_confidence}; escalate rather than accept silently."
                ),
                notes=[
                    "Low confidence is escalated; JEV does not silently decide."
                ],
                provenance_extra={
                    "min_confidence": min_confidence,
                    "action": action,
                    "transport_keys": normalized["raw_keys"],
                },
            )

        selected = normalized["selected_candidate_id"]
        ranking = normalized["ranking"]
        if selected is None and ranking:
            selected = ranking[0]

        if selected is None and ranking is None:
            return apply_fallback(
                request,
                status=STATUS_UNCERTAIN,
                provider_id=PROVIDER_ID_JEV,
                reason="JEV returned no selectable candidate after normalization",
                notes=["No selectable candidate — fallback, not a silent decision."],
                provenance_extra={"transport_keys": normalized["raw_keys"]},
            )

        return build_evidence(
            status=STATUS_DECIDED,
            passed=True,
            request=request,
            provider_id=PROVIDER_ID_JEV,
            decided_by="jev-transport",
            selected_candidate_id=selected,
            ranking=ranking,
            confidence=confidence,
            uncertainty=normalized["uncertainty"],
            notes=[
                "JEV decision evidence only — not Critic, Operator, or release authority."
            ],
            provenance_extra={
                "action": action or "decide",
                "timeout_s": timeout_s,
                "min_confidence": min_confidence,
                "transport_keys": normalized["raw_keys"],
                "authority": "decision_evidence_only",
            },
        )


def decide_with_jev(
    raw_request: Mapping[str, Any] | DecisionRequest,
    *,
    transport: JevTransport | None = None,
    default_timeout_s: float = DEFAULT_TIMEOUT_S,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> DecisionEvidence:
    """Convenience: parse via neutral resolve path semantics using JEV provider.

    Prefer studio_decision_provider.resolve_decision(provider=...) at call
    sites; this helper exists for direct JEV-focused callers/tests.
    """
    from studio_decision_provider import resolve_decision

    provider = JevDecisionProvider(
        transport,
        default_timeout_s=default_timeout_s,
        min_confidence=min_confidence,
    )
    return resolve_decision(raw_request, provider=provider)
