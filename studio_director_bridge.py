#!/usr/bin/env python3
"""Director-Engine Bridge (M8): convert a Director-authored
decomposition into a real WorkOrderPlan, and a thin CLI so an
installed Director can reach it.

Per ADR 0003, this is the one concrete, reachable hand-off between an
installed Director (which only ever produces its decomposition as a
JSON string, never as in-process Python objects) and this repository's
existing M2 `WorkOrderPlan`. It duplicates none of WorkOrderPlan's own
DAG/validation logic -- it accepts a Director's raw decomposition (a
JSON string, or an already-parsed list, for direct/programmatic
callers) and hands it to WorkOrderPlan unmodified, translating only
shape-level rejections (not JSON, not a list, not objects) and
WorkOrderPlan's own WorkOrderPlanError into one DirectorBridgeError so
a calling Director tool has a single exception surface for a bad
decomposition. `bytes` is deliberately not a supported input: no real
caller produces it, and supporting it would require its own
decoding-error handling this bridge has no need for.

`main()` (slice 2) is a thin adapter only: read a decomposition (from
--decomposition or stdin) -> build_plan_from_decomposition() -> print
a minimal JSON plan summary. It contains no orchestration logic of its
own -- no specialist invocation, no installer/runtime glue, no
provider adapter -- those remain later M8 slices per ADR 0003.

`route_ready_from_decomposition()` (slice 3) connects the plan's ready
set to the existing M3 `CapabilityRouter` unmodified. `WorkOrderPlan`
deliberately never retains a work order's `executor`/`owned_paths`/
`permitted_side_effects` (M2's own boundary), so routing needs the
decomposition's full records, not the plan or its CLI summary alone --
this mirrors M4's `dispatch_ready_nodes(plan=..., work_orders_by_id=...)`,
which already takes the DAG and the full records as two separate
things. Still no specialist invocation, no `dispatch_ready_nodes()`/
parallel execution: those remain later M8 slices.

`select_execution_path()` (slice 5, recalibrated in slice 7 after a real
production pilot) is the deterministic entry-path decision the
Director's own prose contract has always described ("select the
minimum capable execution path") but never had a testable
implementation of. A real native-CLI acceptance pilot proved that node
count and dependency edges (the slice-5 rule) are the wrong signal:
native Claude Agent delegation already handles ordinary sequential
chains and fan-out-with-join correctly on its own, with no orchestration
engine involved -- so a bare dependency edge, or any number of edges,
must never by itself select "governed_engine". The rule now classifies
on exactly one existing, already-optional work-order field --
`operator_gate_required` (`_studio/kit/schemas/work-order.schema.json`,
default `false`) -- via `_classify_plan(plan, work_orders_by_id)`: an
empty decomposition is "direct" (the Director does the work itself);
any decomposition where no node declares `operator_gate_required: true`
is "delegate" (native Agent(...) delegation, sequential or parallel,
regardless of dependency shape); any decomposition with at least one
node declaring `operator_gate_required: true` is "governed_engine" --
the one thing the engine uniquely provides that native delegation
cannot (a fail-closed, plan-wide Critic+Operator release verdict via
`PlanGovernanceGate`). `owned_paths`/`permitted_side_effects` are
deliberately NOT used for this decision: both are schema-*required* on
every well-formed work order, so their mere presence can never
discriminate ordinary work from governance-worthy work. This is not a
new capability taxonomy, classifier abstraction, or schema field -- it
is one pure function reusing an existing field's own already-precise
semantics, with no NLP/keyword inspection of task content and no new
exception type. `WorkOrderPlan` still never retains `operator_gate_required`
(same M2 boundary as `executor`/`owned_paths`), so `_classify_plan()`
takes the raw `work_orders_by_id` map alongside the plan, exactly the
way M4's `dispatch_ready_nodes(plan=..., work_orders_by_id=...)` already
takes the DAG and the full records as two separate things.

Per ADR 0004 (Front Door entry routing), `select_execution_path()` remains
the single, unduplicated implementation of the classification rule;
`studio_front_door.select_entry_path()` only relabels its three outputs
into Front Door vocabulary (DIRECT/DELEGATE/DIRECTOR) for the plain,
unaddressed pre-Director entry point -- it never reimplements this rule.

Wiring (M8 slice 6): `_plan_summary()` -- and therefore every `main()`
output, with or without `--route` -- now includes `execution_path`
(via the same `_classify_plan()` helper `select_execution_path()`
itself calls, so the rule has exactly one implementation). This makes
the CLI a real Director invokes for its own decomposition the single
source of truth for the delegate/governed_engine choice once a
decomposition already exists; deciding whether a decomposition is
warranted at all, and what its nodes/dependencies are, remains the
Director's own semantic judgment -- this module never sees task
content, only the structure the Director already authored.

The legacy M1-M8 plan_orchestrator / parallel_executor stack remains a
separate safety surface; this module is the minimum bridge so existing
contracts still import. It does not add a second planner.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from studio_capability_router import CapabilityRouter, DEFAULT_MANIFEST_PATH
from studio_work_order_plan import WorkOrderPlan, WorkOrderPlanError


class DirectorBridgeError(RuntimeError):
    pass


def _normalize_decomposition(decomposition) -> list:
    if isinstance(decomposition, str):
        try:
            decomposition = json.loads(decomposition)
        except json.JSONDecodeError as exc:
            raise DirectorBridgeError(f"invalid decomposition JSON: {exc}") from exc

    if not isinstance(decomposition, list) or not all(isinstance(item, dict) for item in decomposition):
        raise DirectorBridgeError("a decomposition must be a JSON array of work-order objects")

    return decomposition


def build_plan_from_decomposition(decomposition) -> WorkOrderPlan:
    decomposition = _normalize_decomposition(decomposition)

    try:
        return WorkOrderPlan(decomposition)
    except WorkOrderPlanError as exc:
        raise DirectorBridgeError(str(exc)) from exc


def _classify_plan(plan: WorkOrderPlan, work_orders_by_id: dict) -> str:
    requires_operator_gate = any(
        work_orders_by_id[work_id].get("operator_gate_required") for work_id in plan.work_ids
    )
    return "governed_engine" if requires_operator_gate else "delegate"


def select_execution_path(decomposition) -> str:
    work_orders = _normalize_decomposition(decomposition)
    if not work_orders:
        return "direct"

    plan = build_plan_from_decomposition(work_orders)
    work_orders_by_id = {entry["work_id"]: entry for entry in work_orders}
    return _classify_plan(plan, work_orders_by_id)


def route_ready_from_decomposition(decomposition, *, router: CapabilityRouter) -> dict:
    plan = build_plan_from_decomposition(decomposition)
    work_orders = _normalize_decomposition(decomposition)
    work_orders_by_id = {entry["work_id"]: entry for entry in work_orders}

    routes = router.route_ready(work_orders_by_id, plan.ready_set({}))

    return {**_plan_summary(plan, work_orders_by_id), "routes": routes}


def _plan_summary(plan: WorkOrderPlan, work_orders_by_id: dict) -> dict:
    ready = plan.ready_set({})
    return {
        "work_ids": list(plan.work_ids),
        "dependencies": {work_id: list(plan.dependencies_of(work_id)) for work_id in plan.work_ids},
        "ready": [work_id for work_id in plan.work_ids if work_id in ready],
        "execution_path": _classify_plan(plan, work_orders_by_id),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decomposition", type=Path, default=None,
                         help="path to a Director-authored decomposition JSON file; reads stdin if omitted")
    parser.add_argument("--route", action="store_true",
                         help="also route the plan's ready work orders through CapabilityRouter")
    args = parser.parse_args()

    text = args.decomposition.read_text(encoding="utf-8") if args.decomposition else sys.stdin.read()

    try:
        if args.route:
            output = route_ready_from_decomposition(text, router=CapabilityRouter(manifest_path=DEFAULT_MANIFEST_PATH))
        else:
            work_orders = _normalize_decomposition(text)
            plan = build_plan_from_decomposition(work_orders)
            output = _plan_summary(plan, {entry["work_id"]: entry for entry in work_orders})
    except DirectorBridgeError as exc:
        print(f"director-bridge error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
