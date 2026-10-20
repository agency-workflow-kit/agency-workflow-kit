#!/usr/bin/env python3
"""Evidence-based authorization gate for one already-authorized work order.

Deterministic policy decides first. An injected JEV evaluator may classify
only the open remainder. High confidence never overrides policy. This gate
does not grant host permissions and does not bypass Cursor's own approval hook.

Unavoidable host prompts this gate cannot remove:
- a host shell or process boundary the host has not already allowed;
- host credential or secret prompts;
- host-enforced destructive git prompts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"
DECISION_ESCALATE = "ESCALATE"

POLICY_AUTONOMOUS = "autonomous_within_authorized_work_order"
RISK_READ_ONLY = "read_only"
RISK_REVERSIBLE = "reversible"
RISK_DESTRUCTIVE = "destructive"
RISK_PRODUCTION = "production"
RISK_SECRETS = "secrets"
RISK_RELEASE = "public_release"
RISK_SCOPE = "scope_expansion"

_TRUST_BOUNDARY_RISKS = frozenset({
    RISK_DESTRUCTIVE, RISK_PRODUCTION, RISK_SECRETS, RISK_RELEASE,
})
_AUTONOMOUS_CLASSES = frozenset({
    "read_only_inspection",
    "tests",
    "ci_inspect",
    "ci_dispatch",
    "review_freeze",
    "safe_retry",
    "verification",
    "critic_evidence",
    "renderer_golden",
    "execute_admitted",
    "implementation",
})
_UNAVOIDABLE_HOST = frozenset({
    "host_shell_boundary",
    "host_credential_prompt",
    "host_destructive_git",
})


class AuthorizationGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorizationDecision:
    decision: str
    confidence: float
    reason: str
    evidence_refs: tuple[str, ...]
    policy: str
    risk: str
    autonomous_execution: bool
    human_required: bool
    resolution: str | None = None
    host_enforced: bool = False
    host_permission_granted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "policy": self.policy,
            "risk": self.risk,
            "autonomous_execution": self.autonomous_execution,
            "human_required": self.human_required,
            "resolution": self.resolution,
            "host_enforced": self.host_enforced,
            "host_permission_granted": False,
        }


def _refs(authorization: Mapping[str, Any], action: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = ["policy:" + POLICY_AUTONOMOUS]
    for source in (authorization.get("evidence_refs"), action.get("evidence_refs")):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            refs.extend(str(item) for item in source if isinstance(item, str) and item)
    return tuple(dict.fromkeys(refs))


def _paths(action: Mapping[str, Any]) -> list[str]:
    raw = action.get("paths") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [str(item) for item in raw]


def _owned(authorization: Mapping[str, Any]) -> list[str]:
    raw = authorization.get("owned_paths") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [str(item).strip().lstrip("./") for item in raw if str(item).strip() not in {"", ".", "/"}]


def _normalize_scope_path(path: str) -> str | None:
    name = path.strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or name.startswith("~"):
        return None
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)


def _inside_owned(path: str, owned: list[str]) -> bool:
    name = _normalize_scope_path(path)
    if name is None:
        return False
    for owner in owned:
        root_path = _normalize_scope_path(owner)
        if root_path is None:
            continue
        if name == root_path or name.startswith(root_path.rstrip("/") + "/"):
            return True
    return False


def _host_enforced(action: Mapping[str, Any]) -> bool:
    return action.get("action_class") in _UNAVOIDABLE_HOST or bool(action.get("host_enforced"))


def _decide(
    *,
    decision: str,
    confidence: float,
    reason: str,
    refs: tuple[str, ...],
    risk: str,
    human_required: bool,
    resolution: str | None,
    host_enforced: bool,
) -> AuthorizationDecision:
    autonomous = decision == DECISION_ALLOW and not human_required
    return AuthorizationDecision(
        decision=decision,
        confidence=confidence,
        reason=reason,
        evidence_refs=refs,
        policy=POLICY_AUTONOMOUS,
        risk=risk,
        autonomous_execution=autonomous,
        human_required=human_required,
        resolution=resolution,
        host_enforced=host_enforced,
        host_permission_granted=False,
    )


MIN_AUTONOMOUS_CONFIDENCE = 0.55


def evaluate_authorization(
    authorization: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    jev: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> AuthorizationDecision:
    """Return ALLOW, DENY, or ESCALATE. Deterministic policy beats JEV."""
    if not isinstance(authorization, Mapping) or not authorization.get("authorized"):
        raise AuthorizationGateError("authorization must be an already-granted work order")
    if not isinstance(action, Mapping):
        raise AuthorizationGateError("action must be a mapping")
    raw_risk = action.get("risk")
    risk = raw_risk.strip() if isinstance(raw_risk, str) and raw_risk.strip() else "unspecified"
    refs = _refs(authorization, action)
    host = _host_enforced(action)
    action_class = str(action.get("action_class") or "")
    owned = _owned(authorization)
    paths = _paths(action)
    outside = [path for path in paths if not _inside_owned(path, owned)]
    expands = bool(action.get("expands_scope")) or action_class == "expand_owned_paths"

    def finish(decision: str, confidence: float, reason: str, *, human: bool, resolution: str | None) -> AuthorizationDecision:
        return _decide(
            decision=decision,
            confidence=confidence,
            reason=reason,
            refs=refs,
            risk=risk,
            human_required=human,
            resolution=resolution,
            host_enforced=host,
        )

    if expands or outside or risk == RISK_SCOPE:
        return finish(DECISION_DENY, 1.0, "action leaves the authorized owned paths", human=False, resolution=None)
    if risk in _TRUST_BOUNDARY_RISKS or action_class in {"public_release", "production_write", "secret_change"}:
        return finish(DECISION_ESCALATE, 1.0, "policy requires a human decision at this trust boundary", human=True, resolution="operator")
    if action.get("evidence_conflict"):
        return finish(DECISION_ESCALATE, 0.4, "conflicting evidence requires a human decision", human=True, resolution="operator")
    if action.get("evidence_valid") is not True:
        return finish(DECISION_ESCALATE, 0.4, "insufficient evidence requires a human decision", human=True, resolution="operator")

    jev_confidence = 1.0
    if jev is not None:
        raw = jev({"action_class": action_class, "risk": risk, "evidence_refs": list(refs)})
        if not isinstance(raw, Mapping):
            return finish(DECISION_ESCALATE, 0.0, "JEV returned no usable decision", human=True, resolution="operator")
        jev_decision = raw.get("decision")
        if isinstance(jev_decision, str):
            jev_decision = jev_decision.strip().upper()
        else:
            jev_decision = None
        confidence = raw.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return finish(DECISION_ESCALATE, 0.0, "JEV confidence is missing", human=True, resolution="operator")
        jev_confidence = float(confidence)
        refs = refs + ("jev:authorization",)
        if not math.isfinite(jev_confidence):
            return finish(DECISION_ESCALATE, 0.0, "JEV confidence is not finite", human=True, resolution="operator")
        if jev_confidence < MIN_AUTONOMOUS_CONFIDENCE or jev_decision not in {DECISION_ALLOW, DECISION_DENY, DECISION_ESCALATE}:
            return finish(DECISION_ESCALATE, jev_confidence, "JEV confidence is below the autonomous threshold", human=True, resolution="operator")
        if jev_decision == DECISION_DENY:
            return finish(DECISION_DENY, jev_confidence, "JEV denied the action", human=False, resolution=None)
        if jev_decision == DECISION_ESCALATE:
            return finish(DECISION_ESCALATE, jev_confidence, "JEV escalated the action", human=True, resolution="operator")

    if action_class not in _AUTONOMOUS_CLASSES:
        return finish(DECISION_ESCALATE, 0.5, "unclassified action requires a human decision", human=True, resolution="operator")
    if risk not in {RISK_READ_ONLY, RISK_REVERSIBLE} or action.get("reversible") is not True:
        return finish(DECISION_ESCALATE, 1.0, "only reversible low-risk actions may proceed autonomously", human=True, resolution="operator")
    return finish(DECISION_ALLOW, jev_confidence, "policy allows this reversible action inside the authorized work order", human=False, resolution=None)


def run_authorized_phases(
    authorization: Mapping[str, Any],
    phases: Sequence[Mapping[str, Any]],
    *,
    jev: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every safe phase under one authorization. One denial does not stop siblings."""
    decisions = []
    executed = []
    denied = []
    human = None
    for phase in phases:
        action = phase.get("action") if isinstance(phase, Mapping) else None
        if not isinstance(action, Mapping):
            raise AuthorizationGateError("each phase needs an action")
        decision = evaluate_authorization(authorization, action, jev=jev)
        decisions.append(decision.as_dict())
        if decision.human_required:
            human = decision.as_dict()
            break
        if decision.decision == DECISION_DENY:
            denied.append(action.get("action_id"))
            continue
        if decision.decision == DECISION_ALLOW:
            execute = phase.get("execute")
            if callable(execute):
                execute()
            executed.append(action.get("action_id"))
        elif decision.resolution in {"critic", "director"}:
            resolve = phase.get("resolve")
            if callable(resolve):
                resolve()
            executed.append(action.get("action_id"))
    return {
        "decisions": decisions,
        "executed": executed,
        "denied": denied,
        "human_escalation": human,
        "consolidated": human is None,
    }
