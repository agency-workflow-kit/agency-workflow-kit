#!/usr/bin/env python3
"""Declarative workflow profiles for Studio OS.

Profiles define phase templates, capability requirements, and evidence
expectations for a work domain. They do not route, store tasks, run Critic,
exercise Operator authority, own project source of truth, or execute work.

Contract per phase step:
  PHASE -> CODE | AGENT | TOOL -> EVIDENCE -> GATE -> NEXT

Phase templates are consumable as PhaseSpec-compatible plain dicts via
phase_dicts_for_factory() for the common Factory Run Engine. This module
still does not route, store tasks, run Critic, or execute work itself.
"""

from __future__ import annotations

from typing import Any, Mapping


class WorkflowProfileError(RuntimeError):
    """Fail-closed profile lookup or validation error."""


# Canonical step kinds in order. Execution modalities are alternatives at
# the second position; a phase template picks exactly one.
STEP_PHASE = "PHASE"
STEP_CODE = "CODE"
STEP_AGENT = "AGENT"
STEP_TOOL = "TOOL"
STEP_EVIDENCE = "EVIDENCE"
STEP_GATE = "GATE"
STEP_NEXT = "NEXT"

EXECUTION_MODALITIES = frozenset({STEP_CODE, STEP_AGENT, STEP_TOOL})
CONTRACT_ORDER = (STEP_PHASE, "CODE|AGENT|TOOL", STEP_EVIDENCE, STEP_GATE, STEP_NEXT)

PROFILE_SOFTWARE = "software"
PROFILE_RESEARCH = "research"
PROFILE_SEO_CONTENT = "seo_content"
PROFILE_ANALYTICS = "analytics"
PROFILE_OPERATIONS = "operations"

PROFILE_IDS = (
    PROFILE_SOFTWARE,
    PROFILE_RESEARCH,
    PROFILE_SEO_CONTENT,
    PROFILE_ANALYTICS,
    PROFILE_OPERATIONS,
)


def _phase(
    phase_id: str,
    *,
    title: str,
    modality: str,
    capabilities: tuple[str, ...],
    evidence: tuple[str, ...],
    gate: str,
    next_phase: str | None,
) -> dict[str, Any]:
    if modality not in EXECUTION_MODALITIES:
        raise WorkflowProfileError(f"invalid execution modality: {modality}")
    return {
        "phase_id": phase_id,
        "title": title,
        "steps": (STEP_PHASE, modality, STEP_EVIDENCE, STEP_GATE, STEP_NEXT),
        "capability_requirements": capabilities,
        "evidence_expectations": evidence,
        "gate": gate,
        "next": next_phase,
    }


def _profile(
    profile_id: str,
    *,
    title: str,
    summary: str,
    phases: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    phase_ids = [p["phase_id"] for p in phases]
    if len(phase_ids) != len(set(phase_ids)):
        raise WorkflowProfileError(f"duplicate phase_id in profile {profile_id}")
    return {
        "profile_id": profile_id,
        "title": title,
        "summary": summary,
        "owns": (
            "phase_templates",
            "capability_requirements",
            "evidence_expectations",
        ),
        "does_not_own": (
            "routing",
            "task_storage",
            "critic",
            "operator_authority",
            "project_source_of_truth",
            "execution",
        ),
        "contract": " -> ".join(CONTRACT_ORDER),
        "phases": phases,
    }


_PROFILES: dict[str, dict[str, Any]] = {
    PROFILE_SOFTWARE: _profile(
        PROFILE_SOFTWARE,
        title="Software",
        summary="Bounded implementation work with verification evidence.",
        phases=(
            _phase(
                "clarify",
                title="Clarify acceptance",
                modality=STEP_AGENT,
                capabilities=("requirements_interrogation",),
                evidence=("acceptance_criteria", "scope_boundary"),
                gate="acceptance_clear",
                next_phase="implement",
            ),
            _phase(
                "implement",
                title="Implement change",
                modality=STEP_CODE,
                capabilities=("implementation",),
                evidence=("diff", "unit_or_integration_result"),
                gate="implementation_complete",
                next_phase="verify",
            ),
            _phase(
                "verify",
                title="Verify against acceptance",
                modality=STEP_TOOL,
                capabilities=("verification",),
                evidence=("verification_report", "exact_revision"),
                gate="acceptance_met",
                next_phase=None,
            ),
        ),
    ),
    PROFILE_RESEARCH: _profile(
        PROFILE_RESEARCH,
        title="Research",
        summary="Evidence gathering and synthesis without implementation mutation.",
        phases=(
            _phase(
                "frame",
                title="Frame question",
                modality=STEP_AGENT,
                capabilities=("research_framing",),
                evidence=("question_statement", "source_plan"),
                gate="question_framed",
                next_phase="gather",
            ),
            _phase(
                "gather",
                title="Gather sources",
                modality=STEP_TOOL,
                capabilities=("source_retrieval",),
                evidence=("source_citations", "raw_notes"),
                gate="sources_sufficient",
                next_phase="synthesize",
            ),
            _phase(
                "synthesize",
                title="Synthesize findings",
                modality=STEP_AGENT,
                capabilities=("research_synthesis",),
                evidence=("findings_summary", "open_questions"),
                gate="synthesis_complete",
                next_phase=None,
            ),
        ),
    ),
    PROFILE_SEO_CONTENT: _profile(
        PROFILE_SEO_CONTENT,
        title="SEO / Content",
        summary="Content and SEO work with measurable publish-ready evidence.",
        phases=(
            _phase(
                "brief",
                title="Content brief",
                modality=STEP_AGENT,
                capabilities=("content_briefing", "keyword_research"),
                evidence=("brief", "target_queries"),
                gate="brief_approved",
                next_phase="draft",
            ),
            _phase(
                "draft",
                title="Draft content",
                modality=STEP_AGENT,
                capabilities=("content_authoring",),
                evidence=("draft_artifact", "on_page_checklist"),
                gate="draft_ready_for_review",
                next_phase="optimize",
            ),
            _phase(
                "optimize",
                title="Optimize and package",
                modality=STEP_TOOL,
                capabilities=("seo_audit", "schema_check"),
                evidence=("seo_findings", "publish_package"),
                gate="publish_ready",
                next_phase=None,
            ),
        ),
    ),
    PROFILE_ANALYTICS: _profile(
        PROFILE_ANALYTICS,
        title="Analytics",
        summary="Measurement definition, analysis, and decision-ready reporting.",
        phases=(
            _phase(
                "define",
                title="Define metrics",
                modality=STEP_AGENT,
                capabilities=("metric_definition",),
                evidence=("metric_spec", "data_sources"),
                gate="metrics_defined",
                next_phase="analyze",
            ),
            _phase(
                "analyze",
                title="Analyze data",
                modality=STEP_TOOL,
                capabilities=("data_query", "analysis"),
                evidence=("query_results", "analysis_notes"),
                gate="analysis_complete",
                next_phase="report",
            ),
            _phase(
                "report",
                title="Report insights",
                modality=STEP_AGENT,
                capabilities=("analytics_reporting",),
                evidence=("insight_report", "recommended_actions"),
                gate="report_ready",
                next_phase=None,
            ),
        ),
    ),
    PROFILE_OPERATIONS: _profile(
        PROFILE_OPERATIONS,
        title="Operations",
        summary="Operational runbooks, checks, and change evidence.",
        phases=(
            _phase(
                "assess",
                title="Assess state",
                modality=STEP_TOOL,
                capabilities=("ops_inspection",),
                evidence=("current_state", "risk_notes"),
                gate="state_known",
                next_phase="act",
            ),
            _phase(
                "act",
                title="Execute runbook step",
                modality=STEP_TOOL,
                capabilities=("ops_execution",),
                evidence=("action_log", "before_after"),
                gate="action_complete",
                next_phase="confirm",
            ),
            _phase(
                "confirm",
                title="Confirm outcome",
                modality=STEP_AGENT,
                capabilities=("ops_confirmation",),
                evidence=("confirmation_record", "followups"),
                gate="outcome_confirmed",
                next_phase=None,
            ),
        ),
    ),
}


def list_profiles() -> tuple[str, ...]:
    """Return stable profile ids in registry order."""
    return PROFILE_IDS


def get_profile(profile_id: str) -> Mapping[str, Any]:
    """Return one profile definition. Fail-closed on unknown ids."""
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise WorkflowProfileError(f"unknown workflow profile: {profile_id}") from exc


def phase_template(profile_id: str) -> tuple[dict[str, Any], ...]:
    """Return ordered phase templates for a profile."""
    return tuple(get_profile(profile_id)["phases"])


def capability_requirements(profile_id: str, phase_id: str | None = None) -> tuple[str, ...]:
    """Return capability requirements for a profile or one of its phases."""
    phases = phase_template(profile_id)
    if phase_id is None:
        seen: list[str] = []
        for phase in phases:
            for cap in phase["capability_requirements"]:
                if cap not in seen:
                    seen.append(cap)
        return tuple(seen)
    for phase in phases:
        if phase["phase_id"] == phase_id:
            return phase["capability_requirements"]
    raise WorkflowProfileError(f"unknown phase {phase_id!r} in profile {profile_id!r}")


def evidence_expectations(profile_id: str, phase_id: str | None = None) -> tuple[str, ...]:
    """Return evidence expectations for a profile or one of its phases."""
    phases = phase_template(profile_id)
    if phase_id is None:
        seen: list[str] = []
        for phase in phases:
            for item in phase["evidence_expectations"]:
                if item not in seen:
                    seen.append(item)
        return tuple(seen)
    for phase in phases:
        if phase["phase_id"] == phase_id:
            return phase["evidence_expectations"]
    raise WorkflowProfileError(f"unknown phase {phase_id!r} in profile {profile_id!r}")


def validate_phase_contract(phase: Mapping[str, Any]) -> None:
    """Assert a phase dict follows PHASE -> modality -> EVIDENCE -> GATE -> NEXT."""
    steps = tuple(phase.get("steps") or ())
    if len(steps) != 5:
        raise WorkflowProfileError("phase must declare exactly five contract steps")
    if steps[0] != STEP_PHASE:
        raise WorkflowProfileError("phase must start with PHASE")
    if steps[1] not in EXECUTION_MODALITIES:
        raise WorkflowProfileError("second step must be CODE, AGENT, or TOOL")
    if steps[2] != STEP_EVIDENCE:
        raise WorkflowProfileError("third step must be EVIDENCE")
    if steps[3] != STEP_GATE:
        raise WorkflowProfileError("fourth step must be GATE")
    if steps[4] != STEP_NEXT:
        raise WorkflowProfileError("fifth step must be NEXT")


def profile_as_dict(profile_id: str) -> dict[str, Any]:
    """JSON-serializable copy of a profile (tuples -> lists)."""
    profile = get_profile(profile_id)
    return {
        "profile_id": profile["profile_id"],
        "title": profile["title"],
        "summary": profile["summary"],
        "owns": list(profile["owns"]),
        "does_not_own": list(profile["does_not_own"]),
        "contract": profile["contract"],
        "phases": [
            {
                "phase_id": phase["phase_id"],
                "title": phase["title"],
                "steps": list(phase["steps"]),
                "capability_requirements": list(phase["capability_requirements"]),
                "evidence_expectations": list(phase["evidence_expectations"]),
                "gate": phase["gate"],
                "next": phase["next"],
            }
            for phase in profile["phases"]
        ],
    }


def phase_dicts_for_factory(
    profile_id: str,
    *,
    work_order_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return PhaseSpec-compatible plain dicts for the common Factory engine.

    Profiles stay declarative: this does not execute work, invent a domain
    orchestrator, or fork a second engine. Callers (or the autonomous
    lifecycle) supply CODE operation/command fields and AGENT/TOOL producers
    when running through FactoryRunEngine.
    """
    out: list[dict[str, Any]] = []
    for phase in phase_template(profile_id):
        validate_phase_contract(phase)
        modality = phase["steps"][1]
        entry: dict[str, Any] = {
            "phase_id": phase["phase_id"],
            "phase_kind": modality,
            "producer": f"workflow_profile.{profile_id}.{phase['phase_id']}",
            "work_order_id": work_order_id,
            "notes": list(phase["evidence_expectations"]),
            "gate": phase["gate"],
            "capability_requirements": list(phase["capability_requirements"]),
        }
        out.append(entry)
    return tuple(out)
