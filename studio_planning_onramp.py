#!/usr/bin/env python3
"""Shared clarification/planning on-ramp contract for the Front Door.

States (exact literals):
  CLEAR                 — no questions; proceed to Front Door route_entry
  MATERIAL_AMBIGUITY    — studio-grill / requirements_interrogation
  GRILL_WITH_DOCS       — durable one-session decisions (repo + durable)
  WAYFINDER             — huge / foggy / multi-session decision wayfinding

This module is NOT a second execution engine, planner, Master Plan, or
architecture document. After any on-ramp completes, every path returns to
the existing Front Door router (`studio_front_door.route_entry` →
DIRECT | DELEGATE | DIRECTOR). The on-ramp result is an input to that
router, not a replacement for it.

Shipped as part of the Front Door runtime closure
(`studio_renderer.FRONT_DOOR_RUNTIME_MODULES`) so an activated external
project can run on-ramps via the installed `studio_front_door.py`.
Does not import `studio_capability_providers` / `studio_provider_detection`
at module load — those stay kit-repo-only; provider selection is optional
when those modules are present on sys.path.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

# Exact on-ramp state set. User-facing phrase map (docs / ADR 0004):
#   CLEAR                 ← clear tasks (zero questions)
#   MATERIAL_AMBIGUITY    ← material ambiguity → studio-grill
#   GRILL_WITH_DOCS       ← REPO + DURABLE_DECISIONS + ONE_SESSION
#   WAYFINDER             ← HUGE / FOGGY / MULTI_SESSION
ONRAMP_STATES = frozenset({
    "CLEAR",
    "MATERIAL_AMBIGUITY",
    "GRILL_WITH_DOCS",
    "WAYFINDER",
})

CLEAR = "CLEAR"
MATERIAL_AMBIGUITY = "MATERIAL_AMBIGUITY"
GRILL_WITH_DOCS = "GRILL_WITH_DOCS"
WAYFINDER = "WAYFINDER"

# Procedure labels owned by existing contracts / providers — NOT new
# generated core skills. Catalog lock stays studio-gate, studio-verify,
# studio-run-engine, studio-grill.
PROCEDURE_STUDIO_GRILL = "studio-grill"
PROCEDURE_STUDIO_GRILL_WITH_DOCS = "studio-grill-with-docs"
PROCEDURE_STUDIO_WAYFINDER = "studio-wayfinder"

CAPABILITY_BY_STATE: dict[str, str | None] = {
    CLEAR: None,
    MATERIAL_AMBIGUITY: "requirements_interrogation",
    GRILL_WITH_DOCS: "durable_decision_documentation",
    WAYFINDER: "decision_wayfinding",
}

PROCEDURE_BY_STATE: dict[str, str | None] = {
    CLEAR: None,
    MATERIAL_AMBIGUITY: PROCEDURE_STUDIO_GRILL,
    GRILL_WITH_DOCS: PROCEDURE_STUDIO_GRILL_WITH_DOCS,
    WAYFINDER: PROCEDURE_STUDIO_WAYFINDER,
}

RETURN_TO_ROUTER = "studio_front_door.route_entry"

# Provenance: canonical pin is the upstream git revision below. Host plugin
# cache 1.2.3 is local availability evidence only — not the pin. Paths were
# verified via GitHub raw at this revision (HTTP 200); do not invent paths.
UPSTREAM_PROVENANCE = {
    "repository": "mattpocock/skills",
    "evaluated_revision": "c55ee46073ed923f86ce59a5eb3b6d895095d1b7",
    "license": "MIT",
    "source_paths": {
        "grill-me": "skills/productivity/grill-me/SKILL.md",
        "grilling": "skills/productivity/grilling/SKILL.md",
        "grill-with-docs": "skills/engineering/grill-with-docs/SKILL.md",
        "wayfinder": "skills/engineering/wayfinder/SKILL.md",
    },
    "adapted_behavior": (
        "Workflow Kit owned bounded wrappers: MATERIAL_AMBIGUITY enters "
        "existing studio-grill (grill-me/grilling adaptation) with "
        "operator-owned asks vs agent-researched facts; GRILL_WITH_DOCS "
        "reconciles durable intent to the canonical Notion project plan and "
        "engineering evidence to GitHub/ADRs without a Master Plan or "
        "CONTEXT.md second source of truth; WAYFINDER planning state is "
        "selected automatically by classify_planning_onramp when signals "
        "show multi-session/foggy effort (upstream skill is "
        "disable-model-invocation / user-invoked — this classifier does not "
        "bypass Operator gates or auto-select the capability provider as "
        "automated); use the owning project's configured work/decision "
        "tracker and leave the destination unresolved until that binding "
        "is verified; "
        "executes_work=false; every path returns to studio_front_door.route_entry."
    ),
    "referenced_behavior": (
        "grilling design-tree rounds and research-vs-ask split; "
        "grill-with-docs durable ADR/glossary recording pattern; "
        "wayfinder decision-map / destination / plan-don't-do posture."
    ),
    "local_host_cache_availability_evidence_only": "mattpocock-skills@1.2.3",
    "vendored_upstream_repo": False,
    "in_repo_provider_ids": (
        "matt-pocock-grill-me",
        "matt-pocock-grill-with-docs",
        "matt-pocock-wayfinder",
    ),
}

# Executable next actions for non-CLEAR states — procedure name alone is
# insufficient; the default agent must be able to act from these fields.
NEXT_ACTION_BY_STATE: dict[str, dict[str, Any]] = {
    MATERIAL_AMBIGUITY: {
        "action": "enter_studio_grill",
        "invoke": "studio-grill",
        "adaptation": (
            "Use existing Workflow Kit studio-grill / grill-me adaptation "
            "(rounds of operator-owned decisions; agent researches facts)."
        ),
        "ask_operator": [
            "scope boundary and non-goals",
            "acceptance criteria / done definition",
            "commitments that require Operator authority",
            "any preference among materially different approaches",
        ],
        "research_yourself": [
            "repository facts (existing code, docs, ADRs, tickets)",
            "already-stated acceptance criteria or constraints in the request",
            "current owning-project tracker binding and durable context if available",
        ],
        "stop_when": (
            "objective is unambiguous and executable as a Front Door "
            "decomposition (CLEAR enough for route_entry)"
        ),
        "then": RETURN_TO_ROUTER,
        "executes_work": False,
    },
    GRILL_WITH_DOCS: {
        "action": "enter_grill_with_docs",
        "invoke": "studio-grill-with-docs",
        "adaptation": (
            "Stateful one-session clarification that records durable "
            "decisions. Do not create a competing Master Plan. Do not create "
            "CONTEXT.md as a second source of truth by default."
        ),
        "durable_sinks": {
            "intent_roadmap_architecture": "canonical Notion project plan",
            "engineering_decisions_evidence": "GitHub / ADRs (existing README truth model)",
        },
        "ask_operator": [
            "product intent / roadmap decisions that belong in Notion",
            "engineering decisions that should become ADRs or GitHub records",
            "whether an external write would cross an existing Operator gate",
        ],
        "research_yourself": [
            "existing Notion project plan fields and prior ADRs",
            "repo structure and related engineering records",
            "whether a proposed write is operator-gated",
        ],
        "operator_gate_rule": (
            "If an external write would cross an existing Operator gate, "
            "prepare the exact action payload and stop — do not perform the "
            "write; do not invent a new approval category."
        ),
        "stop_when": (
            "durable decisions are reconciled to Notion/GitHub sinks (or "
            "gated write payloads are prepared) and the task is executable"
        ),
        "then": RETURN_TO_ROUTER,
        "executes_work": False,
    },
    WAYFINDER: {
        "action": "enter_wayfinder_wrapper",
        "invoke": "studio-wayfinder",
        "adaptation": (
            "Workflow Kit-owned bounded wayfinder wrapper. Planning state is "
            "selected automatically by the classifier when evidence shows "
            "multi-session/foggy effort — do not require the operator to "
            "manually choose Wayfinder because upstream marks the skill "
            "user-invoked. Do not bypass Operator gates. Do not create a "
            "second local ticket system."
        ),
        "tracker": "unresolved-owning-project-binding",
        "output": "decisions_and_dependencies_map",
        "ask_operator": [
            "destination naming for the effort",
            "owning-project tracker binding and authorization to update it when gated",
        ],
        "research_yourself": [
            "whether the effort is genuinely multi-session / foggy",
            "existing owning-project tracker binding and open decision surface",
            "dependencies already visible in repo/docs",
        ],
        "stop_when": (
            "decision/dependency map is sufficiently resolved for a normal "
            "Front Door decomposition"
        ),
        "then": RETURN_TO_ROUTER,
        "executes_work": False,
        "provider_selection_note": (
            "Automatic selection is the planning-state classifier only. "
            "A PRESENT wayfinder provider stays recommended + "
            "operator_approval_required — never a silent automated "
            "capability-provider override."
        ),
    },
}


@dataclass(frozen=True)
class PlanningSignals:
    """Caller-supplied facts for on-ramp classification.

    Explicit booleans only — no complexity_score / file_count /
    feature_count / prompt_length heuristic. Clear tasks leave every
    ambiguity/escalation flag false.
    """

    material_ambiguity: bool = False
    repo_context: bool = False
    durable_decisions: bool = False
    one_session: bool = True
    multi_session: bool = False
    huge_or_foggy: bool = False


@dataclass(frozen=True)
class PlanningOnrampResult:
    """On-ramp decision plus optional post-on-ramp Front Door route.

    `entry_route` is populated only when `include_entry_route` is true and
    a post-clarification `decomposition` is supplied — the normal
    DIRECT/DELEGATE/DIRECTOR handoff. Wayfinder never executes work; its
    `decision_map` shape is the durable hand-back for that path.
    `next_action` is the executable contract for non-CLEAR states.
    """

    onramp_state: str
    procedure: str | None
    capability_id: str | None
    provider_selection: dict[str, Any] | None
    asks_questions: bool
    next_action: dict[str, Any] | None
    decision_map: dict[str, Any] | None
    entry_route: dict[str, Any] | None
    return_to_router: str
    provenance: dict[str, Any]
    # Optional declarative profile context — never owns routing/classification.
    profile_context: dict[str, Any] | None = None


def classify_planning_onramp(signals: PlanningSignals | Mapping[str, Any]) -> str:
    """Classify clarification/planning need into an ONRAMP_STATES literal.

    Precedence (first match wins):
      1. huge_or_foggy OR multi_session → WAYFINDER
      2. repo_context AND durable_decisions AND one_session (and not
         multi_session) → GRILL_WITH_DOCS
      3. material_ambiguity → MATERIAL_AMBIGUITY
      4. else → CLEAR
    """
    if isinstance(signals, PlanningSignals):
        s = signals
    else:
        s = PlanningSignals(
            material_ambiguity=bool(signals.get("material_ambiguity", False)),
            repo_context=bool(signals.get("repo_context", False)),
            durable_decisions=bool(signals.get("durable_decisions", False)),
            one_session=bool(signals.get("one_session", True)),
            multi_session=bool(signals.get("multi_session", False)),
            huge_or_foggy=bool(signals.get("huge_or_foggy", False)),
        )

    if s.huge_or_foggy or s.multi_session:
        return WAYFINDER
    if s.repo_context and s.durable_decisions and s.one_session and not s.multi_session:
        return GRILL_WITH_DOCS
    if s.material_ambiguity:
        return MATERIAL_AMBIGUITY
    return CLEAR


def _selection_as_dict(selection: Any) -> dict[str, Any] | None:
    if selection is None:
        return None
    if isinstance(selection, dict):
        return selection
    return asdict(selection)


def _optional_resolve_provider(
    capability_id: str,
    availability: Mapping[str, Any],
) -> Any | None:
    """Resolve provider when kit-repo modules are importable; else None.

    Activated external projects do not ship studio_capability_providers /
    studio_capability_detection. On-ramp contracts remain executable via
    next_action without provider selection.
    """
    try:
        from studio_capability_detection import LifecycleState
        from studio_capability_providers import resolve_provider
    except ImportError:
        return None

    normalized: dict[str, Any] = {}
    for provider_id, state in dict(availability or {}).items():
        if isinstance(state, LifecycleState):
            normalized[provider_id] = state
        elif isinstance(state, str):
            try:
                normalized[provider_id] = LifecycleState[state]
            except KeyError as exc:
                raise ValueError(f"unknown LifecycleState {state!r}") from exc
        else:
            raise ValueError(
                f"availability for {provider_id!r} must be LifecycleState or name"
            )
    return resolve_provider(capability_id, availability=normalized)


def _wayfinder_decision_map(
    *,
    selection: Any | None,
    next_path: str | None,
) -> dict[str, Any]:
    """Small structured hand-back: decisions + next Front Door route.

    Does not execute work. The owning project's verified tracker remains
    work/dependency truth; this map does not invent or assume a tracker.
    """
    provider_block: dict[str, Any] | None = None
    if selection is not None:
        if isinstance(selection, dict):
            provider_block = {
                "recommended_provider_id": selection.get("recommended_provider_id"),
                "selected_provider_id": selection.get("selected_provider_id"),
                "execution_mode": selection.get("execution_mode"),
                "reason": selection.get("reason"),
            }
        else:
            provider_block = {
                "recommended_provider_id": selection.recommended_provider_id,
                "selected_provider_id": selection.selected_provider_id,
                "execution_mode": selection.execution_mode,
                "reason": selection.reason,
            }
    return {
        "kind": "wayfinder_decision_map",
        "decisions": [],
        "dependencies": [],
        "tracker": "unresolved-owning-project-binding",
        "provider": provider_block,
        "next_route": next_path,  # DIRECT | DELEGATE | DIRECTOR once clarified
        "executes_work": False,
    }


def resolve_planning_onramp(
    signals: PlanningSignals | Mapping[str, Any],
    *,
    availability: Mapping[str, Any] | None = None,
    decomposition: Any = None,
    include_entry_route: bool = False,
    router=None,
    profile_id: str | None = None,
) -> PlanningOnrampResult:
    """Resolve on-ramp state, optional provider selection, and router handoff.

    CLEAR asks zero questions and, when `include_entry_route` is set,
    returns `route_entry(decomposition)` unchanged.

    MATERIAL_AMBIGUITY / GRILL_WITH_DOCS use existing resolve_provider
    selection when available (PRESENT → provider; ABSENT/UNKNOWN →
    documented fallback).

    WAYFINDER consults decision_wayfinding only: present Wayfinder stays
    RECOMMENDED / operator_approval_required (never auto-selected as
    automated) unless and until Operator authorization proceeds outside
    this module. Produces a decision_map hand-back; does not execute.
    """
    state = classify_planning_onramp(signals)
    if state not in ONRAMP_STATES:
        raise ValueError(f"unknown on-ramp state: {state!r}")

    # Profile context is optional metadata only. It must not hijack on-ramp
    # classification or replace return_to_router / route_entry.
    profile_context = None
    if profile_id is not None:
        from studio_workflow_profiles import profile_as_dict

        profile_context = profile_as_dict(profile_id)

    capability_id = CAPABILITY_BY_STATE[state]
    procedure = PROCEDURE_BY_STATE[state]
    selection = None
    if capability_id is not None:
        selection = _optional_resolve_provider(capability_id, availability or {})

    entry_route = None
    next_path = None
    if include_entry_route and decomposition is not None:
        # Lazy import avoids a circular import with studio_front_door.
        from studio_front_door import DELEGATE, DIRECT, DIRECTOR, route_entry

        entry_route = route_entry(decomposition, router=router)
        next_path = entry_route["path"]
        if next_path not in {DIRECT, DELEGATE, DIRECTOR}:
            raise ValueError(f"unexpected Front Door path: {next_path!r}")

    decision_map = None
    if state == WAYFINDER:
        decision_map = _wayfinder_decision_map(selection=selection, next_path=next_path)

    next_action = None if state == CLEAR else dict(NEXT_ACTION_BY_STATE[state])

    provenance = dict(UPSTREAM_PROVENANCE)
    if profile_context is not None:
        provenance["workflow_profile_id"] = profile_context["profile_id"]

    return PlanningOnrampResult(
        onramp_state=state,
        procedure=procedure,
        capability_id=capability_id,
        provider_selection=_selection_as_dict(selection),
        asks_questions=state != CLEAR,
        next_action=next_action,
        decision_map=decision_map,
        entry_route=entry_route,
        return_to_router=RETURN_TO_ROUTER,
        provenance=provenance,
        profile_context=profile_context,
    )


def result_as_dict(result: PlanningOnrampResult) -> dict[str, Any]:
    return {
        "onramp_state": result.onramp_state,
        "procedure": result.procedure,
        "capability_id": result.capability_id,
        "provider_selection": result.provider_selection,
        "asks_questions": result.asks_questions,
        "next_action": result.next_action,
        "decision_map": result.decision_map,
        "entry_route": result.entry_route,
        "return_to_router": result.return_to_router,
        "provenance": result.provenance,
        "profile_context": result.profile_context,
    }


def _parse_availability(raw: str | None) -> dict[str, str]:
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --availability-json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--availability-json must be a JSON object")
    out: dict[str, str] = {}
    for provider_id, state_name in parsed.items():
        if not isinstance(state_name, str):
            raise ValueError(f"availability for {provider_id!r} must be a string state name")
        out[provider_id] = state_name
    return out


def _parse_signals(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --signals-json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--signals-json must be a JSON object")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signals-json",
        required=True,
        help="JSON object of PlanningSignals fields",
    )
    parser.add_argument(
        "--availability-json",
        default=None,
        help="optional provider_id -> LifecycleState name map",
    )
    parser.add_argument(
        "--decomposition",
        default=None,
        help="optional post-clarification decomposition JSON (file path or stdin '-'); "
             "when set, includes entry_route from studio_front_door.route_entry",
    )
    args = parser.parse_args()

    try:
        signals = _parse_signals(args.signals_json)
        availability = _parse_availability(args.availability_json)
        decomposition = None
        include_entry_route = False
        if args.decomposition is not None:
            include_entry_route = True
            if args.decomposition == "-":
                decomposition = sys.stdin.read()
            else:
                decomposition = Path(args.decomposition).read_text(encoding="utf-8")
        result = resolve_planning_onramp(
            signals,
            availability=availability,
            decomposition=decomposition,
            include_entry_route=include_entry_route,
        )
    except (ValueError, OSError) as exc:
        print(f"planning-onramp error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result_as_dict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
