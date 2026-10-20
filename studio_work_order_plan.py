#!/usr/bin/env python3
"""Validated dependency graph composing WorkOrderRun nodes by work_id.

M2 of the Director-orchestration milestone: one Director-decomposed work
order represented as a DAG of work-order nodes, plus deterministic
ready-set computation. This is data + validation + ready-set logic, not
an orchestration framework -- no Capability Router, agent/skill/tool
selection, parallel execution, join gates, or retries/recovery. Those
remain later milestones.

Node shape reuses work-order.schema.json's own "work_id"/"dependencies"
fields (dependencies default to [] there too), so a real Director-
produced decomposition -- an array of work-order dicts -- can be handed
to WorkOrderPlan directly; any other schema fields present are ignored,
never required or redefined here.

WorkOrderRun (M1) is unmodified. This module composes WorkOrderRun nodes
by work_id only -- it does not persist run state itself (that remains
each node's own _studio/.local/runs/<work_id>/ file) and does not
duplicate WorkOrderRun's own step/status model; ready_set() takes each
node's already-derived status ("DONE" or anything else) as input.
"""

from __future__ import annotations


class WorkOrderPlanError(RuntimeError):
    pass


class WorkOrderPlan:
    def __init__(self, work_orders):
        if not work_orders:
            raise WorkOrderPlanError("a plan requires at least one work order")

        dependencies = {}
        for entry in work_orders:
            work_id = entry.get("work_id")
            if not work_id:
                raise WorkOrderPlanError("every work order requires a work_id")
            if work_id in dependencies:
                raise WorkOrderPlanError(f"duplicate work_id: {work_id}")
            dependencies[work_id] = tuple(entry.get("dependencies") or ())

        for work_id, deps in dependencies.items():
            for dep in deps:
                if dep == work_id:
                    raise WorkOrderPlanError(f"self-dependency is forbidden: {work_id}")
                if dep not in dependencies:
                    raise WorkOrderPlanError(f"{work_id} depends on unknown work_id: {dep}")

        self._dependencies = dependencies
        self.work_ids = tuple(dependencies)
        self._reject_cycles()

    def _reject_cycles(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {work_id: WHITE for work_id in self.work_ids}

        def visit(work_id, stack):
            color[work_id] = GRAY
            for dep in self._dependencies[work_id]:
                if color[dep] == GRAY:
                    cycle = " -> ".join(stack + [dep])
                    raise WorkOrderPlanError(f"dependency cycle detected: {cycle}")
                if color[dep] == WHITE:
                    visit(dep, stack + [dep])
            color[work_id] = BLACK

        for work_id in self.work_ids:
            if color[work_id] == WHITE:
                visit(work_id, [work_id])

    def dependencies_of(self, work_id) -> tuple:
        return self._dependencies[work_id]

    def ready_set(self, statuses: dict) -> frozenset:
        """Unfinished nodes whose dependencies are all complete.

        `statuses` maps work_id -> its current WorkOrderRun.state()["status"]
        (or any status vocabulary a caller uses); a work_id absent from
        `statuses` is treated as not started. Only the literal "DONE"
        counts as complete -- deterministic, no other status is inferred.
        """
        done = {work_id for work_id, status in statuses.items() if status == "DONE"}
        return frozenset(
            work_id
            for work_id in self.work_ids
            if work_id not in done and all(dep in done for dep in self._dependencies[work_id])
        )
