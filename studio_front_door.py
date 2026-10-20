#!/usr/bin/env python3
"""Front Door entry-path routing (ADR 0004): Operator -> Front Door ->
DIRECT | DELEGATE | DIRECTOR.

The Front Door IS the plain, unaddressed native Claude Code session. It is
NOT a sixth agent identity, NOT a new agent file, NOT a new skill, and NOT
a new orchestration framework -- it comes into existence by activation no
longer composing a forced `"agent": "agency-director"` default into a
project's settings.json (see studio_settings_merge.py, studio_activation.py)
plus this one small pure routing module. A plain session that reaches this
module already exists; nothing here spawns, installs, or renders anything.

Clarification/planning on-ramps (CLEAR / MATERIAL_AMBIGUITY /
GRILL_WITH_DOCS / WAYFINDER) live in studio_planning_onramp.py — a shared
pre-router contract that selects studio-grill, grill-with-docs procedure,
or wayfinder when needed, then always hands back to route_entry() below.
This module imports that on-ramp so the installed Front Door runtime can
classify and emit executable next_action contracts (`--signals-json`)
before route_entry() runs on a clarified decomposition.

The Autonomous Execution Contract (studio_autonomous_execution.py) is the
provider-neutral task lifecycle around that on-ramp + frozen work order +
route_entry path: INTAKE → CLARIFYING → READY → EXECUTING → VERIFYING →
DONE | BLOCKED | GATED. It is imported here so the installed runtime
closure ships it (same packaging precedent as the planning on-ramp).

`select_entry_path()` and `route_entry()` themselves are pure: no filesystem
writes, no subprocess, no network. They import only studio_director_bridge
(the single existing execution-path classifier) and
studio_capability_router (the single existing per-work-order executor/
tool-scope resolver) -- duplicating neither.

`execute_entry()` is the canonical governed driver: it runs the Autonomous
Execution Contract through INTAKE → clarification → freeze → route_entry →
execute/verify (Factory) with DIRECT inline or DELEGATE/DIRECTOR via the
ParallelCoordinator, then Critic + required Operator gate → DONE/BLOCKED/GATED.
It does not invent a second planner; it consumes route_entry() results.

`main()` is a thin CLI adapter only, mirroring studio_director_bridge.py's
own --decomposition/stdin shape exactly, added because a live Claude Code
session cannot call route_entry() as a Python API -- it can only shell out
via Bash (see the studio-run-engine skill contract). It reads a
decomposition (from --decomposition or stdin), calls route_entry()
unchanged, and prints its result as sorted JSON; it contains no routing
logic of its own -- read a file, call the pure function, print the result.

`select_entry_path()` delegates entirely to
studio_director_bridge.select_execution_path(): there must remain exactly
ONE implementation of the classification rule (operator_gate_required
presence) in this codebase. This function only relabels that rule's three
possible outputs ("direct"/"delegate"/"governed_engine") into Front Door
vocabulary (DIRECT/DELEGATE/DIRECTOR) via an explicit lookup table -- an
unexpected value from the bridge raises rather than silently defaulting,
and a malformed decomposition still surfaces the bridge's own
DirectorBridgeError unchanged (never swallowed, never wrapped in a new
exception type).

`route_entry()` turns a selected path into concrete delegation records:

- DIRECT (empty decomposition): no delegations at all. This is the whole
  point of requirement C -- an empty decomposition means the Front Door (a
  plain session) does the work itself; nothing is created, not even a
  worktree.
- DELEGATE: one delegation per work order, routed through
  CapabilityRouter.route() -- the same manifest-driven, per-role tool-scope
  resolution M3 already implements, reused unmodified. No Director is
  created or delegated to on this path. Isolation is derived from the
  routing decision's own existing `tools` string (which itself comes from
  studio_renderer.ROLE_TOOLS): a work order is worktree-isolated if and
  only if its resolved role's tool grant includes a write-class tool
  (`Edit`, `Write`, or `NotebookEdit`). agency-coder/agency-resolver carry
  "Read, Edit, Write, Bash"; agency-researcher/agency-critic do not carry
  any write-class tool, so a read-only specialist needs no worktree at
  all -- isolation only where mutation actually requires it. This is not a
  new capability taxonomy, schema field, or manifest key; it is a read of
  an existing field's existing value.
- DIRECTOR: the decomposition is split into weakly-connected components of
  its dependency graph (dependency edges treated as undirected), and one
  `agency-director` delegation is emitted per independent component. This
  is requirement F: N independent governed work orders produce N separate
  Director delegations the Front Door can issue concurrently, rather than
  routing every governed decomposition through one persistent Director
  bottleneck. The Front Door itself is stateless and holds no Director --
  each independent component gets its own Agent(agency-director, ...)
  spawn with its own worktree isolation (see ADR 0004 for the full
  concurrency mechanism). Director isolation is always "worktree":
  agency-director's only mutation path is Bash, and
  studio_worktree_gate.py blocks un-isolated Bash for a director-rooted
  session (see _studio/kit/adapters/claude-code/README.md's "Concurrency
  isolation" section), so a Director delegation that skipped isolation
  would simply deadlock on its own first Bash call.

Front Door output grants nothing: no tool grant, no MCP grant, no
governance/authorization field is ever present in a route_entry() result.
This module imports nothing from the Director MCP grant-resolution module --
MCP least-privilege scoping for a Director remains that other module's sole
concern, entirely separate from entry-path routing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from studio_capability_router import DEFAULT_MANIFEST_PATH, CapabilityRouter, CapabilityRouterError
from studio_director_bridge import (
    DirectorBridgeError,
    _normalize_decomposition,
    build_plan_from_decomposition,
    select_execution_path,
)
from studio_planning_onramp import (
    resolve_planning_onramp,
    result_as_dict as planning_onramp_result_as_dict,
)
# Autonomous execution lifecycle — imported so FRONT_DOOR_RUNTIME_MODULES
# ships it with the installed Front Door (planning-onramp packaging precedent).
import studio_autonomous_execution  # noqa: F401
# Foundation modules — packaging imports only. They do not classify
# DIRECT/DELEGATE/DIRECTOR and must never let JEV choose the entry path.
import studio_decision_provider  # noqa: F401
import studio_jev_decision_provider  # noqa: F401
import studio_execution_routing  # noqa: F401
import studio_runtime_observability  # noqa: F401
import studio_workflow_profiles  # noqa: F401

DIRECT = "direct"
DELEGATE = "delegate"
DIRECTOR = "director"

# Explicit lookup table, never a reimplementation of the classification
# rule itself -- select_execution_path() remains the single source of
# truth for what "direct"/"delegate"/"governed_engine" means.
_BRIDGE_PATH_TO_ENTRY_PATH = {
    "direct": DIRECT,
    "delegate": DELEGATE,
    "governed_engine": DIRECTOR,
}

# Write-class tool tokens as they appear in studio_renderer.ROLE_TOOLS'
# comma-separated tool strings. Not a new capability taxonomy -- just the
# tokens whose presence in an existing tools string means "this role can
# mutate the filesystem." Kept in sync with
# _studio/kit/adapters/claude-code/studio_worktree_gate.py's
# WRITE_CLASS_AGENTS pin (tests/test_worktree_gate.py's
# WriteClassAgentsDriftPinTests and this module's own
# WriteClassToolsPinTests both check against the same
# {"Edit", "Write", "NotebookEdit"} ground truth) -- a role granted only
# NotebookEdit must never end up gated by WRITE_CLASS_AGENTS while
# route_entry() still emits isolation: None for it.
_WRITE_CLASS_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def select_entry_path(decomposition) -> str:
    """Relabel select_execution_path()'s classification into Front Door
    vocabulary. Fails loud (raises) on any bridge output this module does
    not explicitly know how to map -- never silently defaults."""
    bridge_path = select_execution_path(decomposition)
    try:
        return _BRIDGE_PATH_TO_ENTRY_PATH[bridge_path]
    except KeyError:
        raise DirectorBridgeError(
            f"unexpected execution path from studio_director_bridge: {bridge_path!r}"
        )


def _is_write_class(tools: str) -> bool:
    tokens = {token.strip() for token in tools.split(",")}
    return not tokens.isdisjoint(_WRITE_CLASS_TOOLS)


def _weakly_connected_components(plan) -> list[list[str]]:
    """Union-find over WorkOrderPlan.dependencies_of(), treating every
    dependency edge as undirected. WorkOrderPlan already validates and
    rejects cycles/unknown dependencies at construction time -- this only
    groups its already-validated work_ids into connected components."""
    parent = {work_id: work_id for work_id in plan.work_ids}

    def find(work_id: str) -> str:
        root = work_id
        while parent[root] != root:
            root = parent[root]
        while parent[work_id] != root:
            parent[work_id], work_id = root, parent[work_id]
        return root

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for work_id in plan.work_ids:
        for dependency in plan.dependencies_of(work_id):
            union(work_id, dependency)

    components: dict[str, list[str]] = {}
    for work_id in plan.work_ids:
        components.setdefault(find(work_id), []).append(work_id)

    return [sorted(members) for members in components.values()]


def _admission_items(delegations, work_orders) -> list[dict]:
    """One admission identity per governed work id, including inside a Director component."""
    by_id = {entry["work_id"]: entry for entry in work_orders}
    items = []
    for delegation in delegations:
        for work_id in delegation["work_ids"]:
            work_order = by_id.get(work_id, {})
            dependencies = list(work_order.get("dependencies") or [])
            if not dependencies:
                dependencies = list(delegation.get("dependencies") or [])
            providers = list(work_order.get("providers") or [])
            items.append({
                "work_id": work_id,
                "owned_paths": list(work_order.get("owned_paths") or []),
                "dependencies": dependencies,
                "gated": bool(work_order.get("operator_gate_required")),
                "providers": providers or ["default"],
                "targets": list(work_order.get("targets") or []) or None,
            })
    return items


def _with_admission(
    result: dict,
    work_orders,
    *,
    admission_capacity: int | None,
    provider_health,
    completed,
    already_admitted,
) -> dict:
    if result["path"] == DIRECT:
        result["admission"] = {"capacity": 0, "admitted_count": 0, "results": []}
        return result
    from studio_parallel_coordinator import admit_workstreams
    items = _admission_items(result["delegations"], work_orders)
    capacity = len(items) if admission_capacity is None else admission_capacity
    result["admission"] = admit_workstreams(
        items,
        capacity=capacity,
        provider_health=provider_health,
        completed=completed,
        already_admitted=already_admitted,
    )
    return result


def route_entry(
    decomposition,
    *,
    router: CapabilityRouter | None = None,
    admission_capacity: int | None = None,
    provider_health=None,
    completed=None,
    already_admitted=None,
) -> dict:
    """Return {"path": DIRECT|DELEGATE|DIRECTOR, "delegations": [...]}.

    Every delegation carries {"agent": str, "work_ids": [str, ...],
    "isolation": "worktree" | None}, sorted deterministically by its first
    work_id. Carries no tool grant, no MCP grant, and no governance field --
    PlanGovernanceGate remains the sole authority for release readiness.

    DELEGATE delegations additionally carry "dependencies": [str, ...] --
    that single work order's own dependency edges, read from the same
    WorkOrderPlan.dependencies_of() studio_director_bridge._plan_summary()
    already exposes under this exact field name/shape. This is a
    consistency restoration, not a new mechanism: one delegation per work
    order otherwise erases ordering (a chain and independent work orders
    would emit byte-identical output), while DIRECTOR delegations already
    preserve ordering structurally by collapsing a whole connected
    component into one delegation. DIRECT (no delegations) and DIRECTOR
    intentionally omit "dependencies": DIRECTOR delegations cover whole
    weakly-connected components, so cross-delegation dependency edges are
    always empty by construction (a component is maximal) -- adding a
    field that is always [] would misleadingly suggest no internal
    ordering constraint exists, when the receiving Director itself already
    has the full work-order set to re-derive that ordering."""
    path = select_entry_path(decomposition)

    if path == DIRECT:
        return _with_admission({"path": DIRECT, "delegations": []}, [], admission_capacity=admission_capacity, provider_health=provider_health, completed=completed, already_admitted=already_admitted)

    work_orders = _normalize_decomposition(decomposition)
    work_orders_by_id = {entry["work_id"]: entry for entry in work_orders}

    if path == DELEGATE:
        plan = build_plan_from_decomposition(work_orders)
        active_router = router if router is not None else CapabilityRouter(manifest_path=DEFAULT_MANIFEST_PATH)
        delegations = []
        for work_id in work_orders_by_id:
            decision = active_router.route(work_orders_by_id[work_id])
            isolation = "worktree" if _is_write_class(decision["tools"]) else None
            delegations.append({
                "agent": decision["executor"],
                "work_ids": [work_id],
                "isolation": isolation,
                "dependencies": list(plan.dependencies_of(work_id)),
            })
        delegations.sort(key=lambda delegation: delegation["work_ids"][0])
        return _with_admission({"path": DELEGATE, "delegations": delegations}, work_orders, admission_capacity=admission_capacity, provider_health=provider_health, completed=completed, already_admitted=already_admitted)

    # DIRECTOR: split into weakly-connected components; one Director
    # delegation per independent component, always worktree-isolated.
    plan = build_plan_from_decomposition(work_orders)
    delegations = [
        {"agent": "agency-director", "work_ids": members, "isolation": "worktree"}
        for members in _weakly_connected_components(plan)
    ]
    delegations.sort(key=lambda delegation: delegation["work_ids"][0])
    return _with_admission({"path": DIRECTOR, "delegations": delegations}, work_orders, admission_capacity=admission_capacity, provider_health=provider_health, completed=completed, already_admitted=already_admitted)



def _prepare_next_operator_gate(task):
    """Prepare the next still-gated work id. One authorization does not carry forward."""
    task.operator_authorization = None
    task.prepared_gate_action = None
    gated = next(
        (entry for entry in task.frozen_work_order or [] if entry.get("operator_gate_required")),
        None,
    )
    if gated is None:
        return None
    task.prepare_operator_gate(
        gate_kind="operator_gate_required",
        prepared_action={"action": "authorize_release", "work_id": gated["work_id"]},
        required_decision_or_action=f"Operator authorize release for {gated['work_id']}",
    )
    return gated


def _release_authorized_gate(task, *, execute_phases, verify_phases):
    """Admit previously gated work only after Operator authorization succeeds."""
    from studio_parallel_coordinator import ADMISSION_ADMITTED, ADMISSION_ALREADY

    action = ((task.operator_authorization or {}).get("action") or {})
    released_work_id = action.get("work_id")
    released = []
    for entry in task.frozen_work_order or []:
        item = dict(entry)
        if item.get("operator_gate_required") and item.get("work_id") == released_work_id:
            item["operator_gate_required"] = False
        released.append(item)
    task.frozen_work_order = released
    prior = {}
    completed = list(getattr(task, "_admission_completed", None) or [])
    for row in ((task.entry_route or {}).get("admission") or {}).get("results") or []:
        if row.get("disposition") in {ADMISSION_ADMITTED, ADMISSION_ALREADY}:
            prior[row["work_id"]] = row
    for run in task.workstream_runs:
        if run.get("passed") and run["evidence"]["work_id"] not in completed:
            completed.append(run["evidence"]["work_id"])
    for work_id in getattr(task, "_durable_completions", None) or {}:
        if work_id not in completed:
            completed.append(work_id)
    task.capture_durable_completions()
    task.entry_route = route_entry(
        released,
        admission_capacity=getattr(task, "_admission_capacity", None),
        provider_health=getattr(task, "_provider_health", None),
        completed=completed,
        already_admitted=prior,
    )
    task.dispatch_route(worktrees=getattr(task, "_dispatch_worktrees", None))
    kept = list(task.workstream_runs)
    if task.coordinator_dispatch.get("coordinator_used"):
        task.execute_admitted_workstreams(execute_phases, verify_phases)
        task.workstream_runs = kept + list(task.workstream_runs)
    return task.join_required_work()


def _complete_admitted_entry(
    task,
    *,
    execute_phases,
    verify_phases,
    done_supplemental,
) -> dict:
    """Execute each admitted workstream, then join the full required set.

    Deferred, rate-limited, waiting, gated, and refused required work stay
    in the join. The parent becomes DONE only when that join is JOINED.
    """
    from studio_autonomous_execution import (
        MANDATORY_OPERATOR_GATE,
        UNRESOLVABLE_BLOCKER,
        AutonomousExecutionError,
    )
    from studio_parallel_coordinator import ADMISSION_GATED, JOIN_BLOCKED, JOIN_JOINED

    if task.coordinator_dispatch.get("coordinator_used"):
        task.execute_admitted_workstreams(execute_phases, verify_phases)
    join = task.join_required_work()
    supplemental = dict(done_supplemental or {})
    authorization_error = None

    rows = ((task.entry_route or {}).get("admission") or {}).get("results") or []
    gated_required = any(row.get("disposition") == ADMISSION_GATED for row in rows)
    if gated_required or (task._operator_gate_required() and join.get("verdict") != JOIN_JOINED):
        gated = next(
            entry for entry in task.frozen_work_order or []
            if entry.get("operator_gate_required")
        )
        if task.prepared_gate_action is None:
            task.prepare_operator_gate(
                gate_kind="operator_gate_required",
                prepared_action={
                    "action": "authorize_release",
                    "work_id": gated["work_id"],
                },
                required_decision_or_action=f"Operator authorize release for {gated['work_id']}",
            )
        auth = supplemental.get("operator_authorization")
        if auth:
            try:
                task.authorize_operator_gate(auth)
            except AutonomousExecutionError as exc:
                authorization_error = str(exc)
        if task.operator_authorization is None:
            reason = "operator authorization required before DONE"
            if authorization_error:
                reason = f"{reason}; rejected authorization: {authorization_error}"
            task.interrupt(
                interruption_class=MANDATORY_OPERATOR_GATE,
                reason=reason,
                required_decision_or_action=task.prepared_gate_action["decision"],
                gate_kind=task.prepared_gate_action["gate_kind"],
                prepared_action=task.prepared_gate_action["action"],
            )
            task.finish_delegations(passed=False)
            result = _admitted_payload(task, join, error=None)
            if authorization_error:
                result["authorization_error"] = authorization_error
            return result
        join = _release_authorized_gate(
            task, execute_phases=execute_phases, verify_phases=verify_phases,
        )
        still_gated = any(
            row.get("disposition") == ADMISSION_GATED
            for row in ((task.entry_route or {}).get("admission") or {}).get("results") or []
        )
        if still_gated and join.get("verdict") != JOIN_JOINED:
            _prepare_next_operator_gate(task)
            task.interrupt(
                interruption_class=MANDATORY_OPERATOR_GATE,
                reason="operator authorization does not release every gated workstream",
                required_decision_or_action=task.prepared_gate_action["decision"],
                gate_kind=task.prepared_gate_action["gate_kind"],
                prepared_action=task.prepared_gate_action["action"],
            )
            task.finish_delegations(passed=False)
            return _admitted_payload(task, join)

    task.finish_delegations(passed=join.get("verdict") == JOIN_JOINED)
    if join.get("verdict") != JOIN_JOINED:
        if join.get("verdict") == JOIN_BLOCKED:
            task.interrupt(
                interruption_class=UNRESOLVABLE_BLOCKER,
                reason=_failed_work_reason(task, join),
                required_decision_or_action=(
                    "resolve failed or refused required work before the parent can be DONE"
                ),
            )
            return _admitted_payload(task, join, error="execution_halted")
        return _admitted_payload(task, join, error="admission_incomplete")

    try:
        task.adopt_workstream_evidence()
    except AutonomousExecutionError as exc:
        return _admitted_payload(task, join, error="complete_done_rejected", completion_error=exc)
    task.begin_verification()
    fresh, stale_work_id = task.contributing_workstreams_fresh()
    if not fresh:
        task.interrupt(
            interruption_class=UNRESOLVABLE_BLOCKER,
            reason=f"workstream evidence is stale: {stale_work_id}",
            required_decision_or_action="re-verify the stale workstream before DONE",
        )
        return _admitted_payload(task, join, error="verification_stale")
    if not task.critic_required and "critic_not_required" not in supplemental:
        supplemental["critic_not_required"] = True
    try:
        task.complete_done(supplemental)
    except AutonomousExecutionError as exc:
        return _admitted_payload(task, join, error="complete_done_rejected", completion_error=exc)
    return _admitted_payload(task, join)


_COMPLETION_ERROR_MAX_CHARS = 500
# Concrete secret shapes from studio_readiness.ReadinessGate.SECRET_VALUE_PATTERNS,
# which is not part of the installed runtime.
_SECRET_SHAPED = re.compile(
    r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9_])gh[pos]_[A-Za-z0-9]{20,}"
    r"|(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}"
    r"|(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"
    r"|(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)


def _completion_error(exc: BaseException) -> str:
    """The existing rejection message, bounded; withheld whole if it looks secret-bearing."""
    message = str(exc)
    if _SECRET_SHAPED.search(message):
        return "completion rejected; reason withheld (secret-shaped content)"
    if len(message) > _COMPLETION_ERROR_MAX_CHARS:
        message = message[: _COMPLETION_ERROR_MAX_CHARS - 3] + "..."
    return message


_FAILED_WORK_REASON = "required work failed, was refused, or did not complete"
_FAILED_WORK_LIST_MAX = 5


def _bounded_list(items: Sequence[str]) -> str:
    shown = [str(item) for item in items[:_FAILED_WORK_LIST_MAX]]
    if len(items) > _FAILED_WORK_LIST_MAX:
        shown.append(f"+{len(items) - _FAILED_WORK_LIST_MAX} more")
    return "[" + ", ".join(shown) + "]"


def _failed_work_reason(task, join) -> str:
    """The fixed BLOCKED reason plus each failed work id's recorded causes, bounded (#144)."""
    from studio_factory_run import STATUS_PASSED

    evidence = {item["work_id"]: item for item in task.workstream_evidence()}
    details = []
    for work_id in join.get("failed") or []:
        item = evidence.get(work_id) or {}
        parts = []
        if item.get("scope_violations"):
            parts.append(f"scope_violations={_bounded_list(item['scope_violations'])}")
        # Last envelope per phase wins, so a repaired phase is not reported as failed.
        last_status: dict[str, Any] = {}
        for key in ("factory_run", "verification"):
            for envelope in (item.get(key) or {}).get("envelopes") or []:
                if envelope.get("phase_id"):
                    last_status[envelope["phase_id"]] = envelope.get("status")
        failed_phases = [phase for phase, status in last_status.items() if status != STATUS_PASSED]
        if failed_phases:
            parts.append(f"failed_phases={_bounded_list(failed_phases)}")
        if parts:
            details.append(f"{work_id}: " + " ".join(parts))
    if not details:
        return _FAILED_WORK_REASON
    detail = "; ".join(details)
    if _SECRET_SHAPED.search(detail):
        return f"{_FAILED_WORK_REASON}: cause detail withheld (secret-shaped content)"
    reason = f"{_FAILED_WORK_REASON}: {detail}"
    if len(reason) > _COMPLETION_ERROR_MAX_CHARS:
        reason = reason[: _COMPLETION_ERROR_MAX_CHARS - 3] + "..."
    return reason


def _admitted_payload(task, join, error=None, completion_error=None) -> dict:
    result = {
        "task_id": task.task_id,
        "state": task.state,
        "entry_route": task.entry_route,
        "coordinator_dispatch": task.coordinator_dispatch,
        "workstreams": task.workstream_evidence(),
        "admission_join": join,
        "factory_run": task.factory_run,
        "task": task.as_dict(),
    }
    if task.prepared_gate_action is not None:
        result["prepared_gate_action"] = task.prepared_gate_action
    if error:
        result["error"] = error
    if completion_error is not None:
        result["completion_error"] = _completion_error(completion_error)
    if task.blocker:
        result["blocker"] = task.blocker
    if task.evidence is not None:
        result["evidence"] = task.evidence
    emitted = {
        work_id: record.get("evidence_ref")
        for work_id, record in (getattr(task, "_durable_completions", None) or {}).items()
        if isinstance(record, dict) and record.get("evidence_ref")
    }
    if emitted:
        result["emitted_evidence"] = emitted
    return result



def _release_evidenced_gates(decomposition, resolved: Mapping[str, Any]):
    """A work id with governed completion evidence is no longer waiting on its gate."""
    if not resolved or not isinstance(decomposition, list):
        return decomposition
    released = []
    for entry in decomposition:
        if isinstance(entry, dict) and entry.get("work_id") in resolved:
            item = dict(entry)
            item["operator_gate_required"] = False
            released.append(item)
        else:
            released.append(entry)
    return released


def _seed_already_from_evidence(already, resolved: Mapping[str, Any]):
    seeded = dict(already or {})
    for work_id, record in resolved.items():
        if work_id in seeded:
            continue
        seeded[work_id] = {
            "work_id": work_id,
            "provider": record.get("provider"),
            "model": record.get("model"),
            "task_id": record.get("admit_task_id") or f"admit:{work_id}",
            "owned_paths": list(record.get("owned_paths") or []),
            "attempts": list(record.get("attempts") or []),
        }
    return seeded



def _completed_with_evidence(admission_completed, resolved):
    completed = [item for item in (admission_completed or []) if isinstance(item, str)]
    for work_id in resolved:
        if work_id not in completed:
            completed.append(work_id)
    return completed


def execute_entry(
    task_id: str,
    signals: Mapping[str, Any],
    *,
    cwd: str,
    proposed_decomposition: Any = None,
    discoverable_facts: Sequence[str] | None = None,
    availability: Mapping[str, Any] | None = None,
    profile_id: str | None = None,
    operator_answers: Mapping[str, Any] | None = None,
    execute_phases: Sequence[Any] | None = None,
    verify_phases: Sequence[Any] | None = None,
    agent_producers: Mapping[str, Any] | None = None,
    worktrees: Mapping[str, Any] | None = None,
    done_supplemental: Mapping[str, Any] | None = None,
    router: CapabilityRouter | None = None,
    admission_capacity: int | None = None,
    provider_health: Mapping[str, Any] | None = None,
    admission_completed: Sequence[str] | None = None,
    already_admitted: Mapping[str, Any] | None = None,
    provider_executors: Mapping[str, Any] | None = None,
    durable_completions: Mapping[str, Any] | None = None,
    durable_evidence_refs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Drive a complete governed task from Front Door entry.

    Lifecycle: INTAKE → (CLARIFYING) → READY → EXECUTING → VERIFYING →
    DONE | BLOCKED | GATED. Routing uses route_entry(); multi-delegation
    uses ParallelCoordinator; DIRECT invents no coordinator work.

    ``profile_id`` is not a supported v1.2 contract: Workflow Profiles are
    deferred (ADR 0010), so any value fails closed instead of being accepted.
    """
    if profile_id is not None:
        raise ValueError(
            "profile_id is not a supported v1.2 Front Door contract: "
            "Workflow Profiles are deferred (ADR 0010)"
        )
    from studio_autonomous_execution import (
        CLARIFYING,
        EXECUTING,
        MANDATORY_OPERATOR_GATE,
        AutonomousExecutionError,
        start_task,
    )

    from studio_autonomous_execution import resolve_factory_evidence
    from studio_runtime_observability import attach_default_ledger
    from studio_authorization_gate import evaluate_authorization
    owned_paths = []
    if isinstance(proposed_decomposition, list):
        for entry in proposed_decomposition:
            if isinstance(entry, dict):
                owned_paths.extend(entry.get("owned_paths") or [])
    supplied = {}
    if isinstance(done_supplemental, dict):
        raw_action = done_supplemental.get("authorization_action")
        if isinstance(raw_action, dict):
            supplied = raw_action
    action = dict(supplied)
    authorization_decision = evaluate_authorization(
        {
            "authorized": True,
            "work_order_id": task_id,
            "owned_paths": owned_paths,
            "evidence_refs": [f"work-order:{task_id}"],
        },
        action,
    )
    if authorization_decision.human_required or authorization_decision.decision == "DENY":
        return {
            "task_id": task_id,
            "state": "GATED" if authorization_decision.human_required else "BLOCKED",
            "authorization_decision": authorization_decision.as_dict(),
            "entry_route": None,
        }

    resolved = resolve_factory_evidence(durable_evidence_refs, worktrees)
    proposed_decomposition = _release_evidenced_gates(proposed_decomposition, resolved)
    already_admitted = _seed_already_from_evidence(already_admitted, resolved)
    admission_completed = _completed_with_evidence(admission_completed, resolved)
    ledger = attach_default_ledger(task_id)
    task = start_task(
        task_id,
        signals,
        discoverable_facts=discoverable_facts,
        proposed_decomposition=proposed_decomposition,
        availability=availability,
        observability_ledger=ledger,
        admission_capacity=admission_capacity,
        provider_health=provider_health,
        admission_completed=admission_completed,
        already_admitted=already_admitted,
        provider_executors=provider_executors,
        authoritative_completions=resolved,
    )

    if task.state == CLARIFYING:
        if not operator_answers:
            return {
                "task_id": task_id,
                "state": task.state,
                "entry_route": task.entry_route,
                "clarification_questions": list(task.clarification_questions),
                "task": task.as_dict(),
            }
        if proposed_decomposition is None:
            raise AutonomousExecutionError(
                "execute_entry clarification completion requires proposed_decomposition"
            )
        task.complete_clarification(
            operator_answers=operator_answers,
            proposed_decomposition=proposed_decomposition,
        )

    # Re-bind router for route consistency when caller supplies one.
    if router is not None and task.frozen_work_order is not None:
        task.entry_route = route_entry(
            task.frozen_work_order,
            router=router,
            admission_capacity=admission_capacity,
            provider_health=provider_health,
            completed=admission_completed,
            already_admitted=already_admitted,
        )

    task.begin_execution(cwd=cwd, agent_producers=agent_producers)

    # Apply DIRECT / DELEGATE / DIRECTOR result for coordinator claims.
    task.dispatch_route(worktrees=worktrees)

    if (task.coordinator_dispatch or {}).get("path") in {DELEGATE, DIRECTOR}:
        return _complete_admitted_entry(
            task,
            execute_phases=list(execute_phases or []),
            verify_phases=list(verify_phases or []),
            done_supplemental=done_supplemental,
        )

    execution_ok = True
    if execute_phases:
        execution_ok = bool(task.execute_phases(execute_phases).all_passed)
    if task.state != EXECUTING:
        # Execution already left EXECUTING (e.g. UNRESOLVABLE_BLOCKER → BLOCKED):
        # do not attempt verification; fail delegations and return the outcome.
        if task.coordinator is not None:
            task.finish_delegations(passed=False)
        return {
            "task_id": task_id,
            "state": task.state,
            "entry_route": task.entry_route,
            "coordinator_dispatch": task.coordinator_dispatch,
            "blocker": task.blocker,
            "factory_run": task.factory_run,
            "task": task.as_dict(),
            "error": "execution_halted",
        }
    task.begin_verification()
    if verify_phases:
        execution_ok = execution_ok and bool(task.verify_phases(verify_phases).all_passed)
    if task.coordinator is not None and not execution_ok:
        task.finish_delegations(passed=False)
        return {
            "task_id": task_id,
            "state": task.state,
            "entry_route": task.entry_route,
            "coordinator_dispatch": task.coordinator_dispatch,
            "task": task.as_dict(),
            "error": "execution_incomplete",
        }

    supplemental = dict(done_supplemental or {})
    if task.critic_required and "critic_evidence" not in supplemental and "critic_verdict" in supplemental:
        # Fail closed via complete_done — bare verdict alone is insufficient.
        pass
    if not task.critic_required and "critic_not_required" not in supplemental:
        supplemental["critic_not_required"] = True

    if task._operator_gate_required() and task.prepared_gate_action is None:
        # Auto-prepare from first gated work order when caller did not.
        gated = next(
            e for e in task.frozen_work_order or []
            if e.get("operator_gate_required")
        )
        task.prepare_operator_gate(
            gate_kind="operator_gate_required",
            prepared_action={
                "action": "authorize_release",
                "work_id": gated["work_id"],
            },
            required_decision_or_action=f"Operator authorize release for {gated['work_id']}",
        )
    if task._operator_gate_required() and task.operator_authorization is None:
        auth = supplemental.get("operator_authorization")
        authorization_error = None
        if auth:
            try:
                task.authorize_operator_gate(auth)
            except AutonomousExecutionError as exc:
                authorization_error = str(exc)
        if task.operator_authorization is None:
            # Missing or invalid authorization: the gate stays unsatisfied → GATED.
            reason = "operator authorization required before DONE"
            if authorization_error:
                reason = f"{reason}; rejected authorization: {authorization_error}"
            task.interrupt(
                interruption_class=MANDATORY_OPERATOR_GATE,
                reason=reason,
                required_decision_or_action=task.prepared_gate_action["decision"],
                gate_kind=task.prepared_gate_action["gate_kind"],
                prepared_action=task.prepared_gate_action["action"],
            )
            result = {
                "task_id": task_id,
                "state": task.state,
                "entry_route": task.entry_route,
                "coordinator_dispatch": task.coordinator_dispatch,
                "prepared_gate_action": task.prepared_gate_action,
                "task": task.as_dict(),
            }
            if authorization_error:
                result["authorization_error"] = authorization_error
            return result

    try:
        task.complete_done(supplemental)
    except AutonomousExecutionError as exc:
        return {
            "task_id": task_id,
            "state": task.state,
            "entry_route": task.entry_route,
            "coordinator_dispatch": task.coordinator_dispatch,
            "task": task.as_dict(),
            "error": "complete_done_rejected",
            "completion_error": _completion_error(exc),
        }
    if task.coordinator is not None:
        task.finish_delegations(passed=True)

    return {
        "task_id": task_id,
        "state": task.state,
        "entry_route": task.entry_route,
        "coordinator_dispatch": task.coordinator_dispatch,
        "evidence": task.evidence,
        "task": task.as_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decomposition", type=Path, default=None,
                         help="path to a decomposition JSON file; reads stdin if omitted "
                              "and --signals-json is not set")
    parser.add_argument(
        "--signals-json",
        default=None,
        help="when set, classify/resolve a planning on-ramp (CLEAR / "
             "MATERIAL_AMBIGUITY / GRILL_WITH_DOCS / WAYFINDER) and print that "
             "contract JSON; optional --decomposition then includes entry_route",
    )
    parser.add_argument(
        "--execute-json",
        default=None,
        help="JSON object or path to a file that drives execute_entry "
             "(route, then the canonical governed lifecycle)",
    )
    parser.add_argument(
        "--availability-json",
        default=None,
        help="optional provider_id -> LifecycleState name map for on-ramp "
             "provider selection (kit-repo only; ignored when providers absent)",
    )
    args = parser.parse_args()

    if args.execute_json is not None:
        try:
            raw = args.execute_json
            payload_path = Path(raw)
            if payload_path.is_file():
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            else:
                payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("--execute-json must be a JSON object")
            from studio_factory_run import PhaseSpec

            def _specs(items):
                return [PhaseSpec(**item) for item in (items or [])]

            worktrees = None
            if payload.get("worktrees"):
                from studio_parallel_coordinator import WorktreeIdentity
                worktrees = {
                    key: WorktreeIdentity(**value)
                    for key, value in payload["worktrees"].items()
                }
            result = execute_entry(
                payload["task_id"],
                payload.get("signals") or {},
                cwd=payload["cwd"],
                proposed_decomposition=payload.get("proposed_decomposition"),
                execute_phases=_specs(payload.get("execute_phases")),
                verify_phases=_specs(payload.get("verify_phases")),
                done_supplemental=payload.get("done_supplemental"),
                profile_id=payload.get("profile_id"),
                worktrees=worktrees,
                admission_capacity=payload.get("admission_capacity"),
                provider_health=payload.get("provider_health"),
                admission_completed=payload.get("admission_completed"),
                already_admitted=payload.get("already_admitted"),
                durable_evidence_refs=payload.get("durable_evidence_refs"),
            )
        except (ValueError, json.JSONDecodeError, OSError, KeyError, TypeError, DirectorBridgeError, CapabilityRouterError) as exc:
            print(f"front-door error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            from studio_autonomous_execution import AutonomousExecutionError
            if not isinstance(exc, AutonomousExecutionError):
                raise
            print(f"front-door error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True, default=str))
        return 0

    if args.signals_json is not None:
        try:
            signals = json.loads(args.signals_json)
            if not isinstance(signals, dict):
                raise ValueError("--signals-json must be a JSON object")
            availability = {}
            if args.availability_json is not None:
                availability = json.loads(args.availability_json)
                if not isinstance(availability, dict):
                    raise ValueError("--availability-json must be a JSON object")
            decomposition = None
            include_entry_route = False
            if args.decomposition is not None:
                include_entry_route = True
                decomposition = args.decomposition.read_text(encoding="utf-8")
            result = resolve_planning_onramp(
                signals,
                availability=availability,
                decomposition=decomposition,
                include_entry_route=include_entry_route,
            )
        except (ValueError, json.JSONDecodeError, OSError, DirectorBridgeError, CapabilityRouterError) as exc:
            print(f"front-door error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(planning_onramp_result_as_dict(result), sort_keys=True))
        return 0

    text = args.decomposition.read_text(encoding="utf-8") if args.decomposition else sys.stdin.read()

    try:
        output = route_entry(text)
    except (DirectorBridgeError, CapabilityRouterError) as exc:
        print(f"front-door error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
