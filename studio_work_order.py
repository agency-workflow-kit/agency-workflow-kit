#!/usr/bin/env python3
"""Resumable, non-secret persisted state machine for one Workflow Kit
work order's Director/specialist/Critic/Operator execution lifecycle.

M1 of the Director-orchestration milestone: reuses SetupWizard's exact
proven pattern (STEPS tuple, _expected_step, fail-closed
_validate_state/_reject_secrets, atomic write_state/read_state) applied
to a work order's four-stage lifecycle instead of the wizard's five
setup steps. Does not import from or modify studio_wizard.py --
readiness's ReadinessGate-backed gate logic is wizard-specific and
doesn't generalize to a work order's PASS/FAIL/BLOCKED_INCONCLUSIVE
critic verdict, whose policy interpretation belongs to a later
milestone (join/Operator gates), not this persisted record.

Only two mutation primitives exist here, not SetupWizard's four:
complete_step and skip_step. fail_step/block_step are unnecessary --
specialist-result.schema.json's own "status" (completed/failed/blocked)
and critic-verdict.schema.json's own "verdict"
(PASS/FAIL/BLOCKED_INCONCLUSIVE) already carry the outcome inside a
completed step's result; a work order that got a real FAIL verdict is
a completed observation of reality, not a step-level failure. Adding a
redundant outer fail/blocked marker would duplicate state the schemas
already own.

Compatible result shapes:
- "delegated": RuntimeProbe.parse_stream_json's output shape
  ({"director_delegation": bool, "delegated_agents": [...]}).
- "specialist_result": a specialist-result.schema.json-shaped dict.
- "critic_review": a critic-verdict.schema.json-shaped dict.
- "operator_gate": {"approved": bool, ...}; skippable only when the
  work order's own operator_gate_required is False.

No DAG, no dependencies, no Capability Router, no parallel execution,
no join gates -- this models exactly one work order's own linear
lifecycle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class WorkOrderError(RuntimeError):
    pass


class WorkOrderRun:
    STEPS = ("delegated", "specialist_result", "critic_review", "operator_gate")
    FORBIDDEN_KEYS = {"token", "password", "secret", "credential", "credentials", "api_key", "apikey"}

    def __init__(self, *, work_id: str):
        if not work_id:
            raise WorkOrderError("work_id is required")
        self.work_id = work_id

    def new_state(self, *, operator_gate_required: bool = False) -> dict:
        return {
            "schema_version": 1,
            "work_id": self.work_id,
            "status": "IN_PROGRESS",
            "operator_gate_required": operator_gate_required,
            "steps": {step: {"completed": False, "result": None} for step in self.STEPS},
        }

    def _validate_state(self, state: dict) -> None:
        if state.get("schema_version") != 1:
            raise WorkOrderError("unsupported work-order state schema")
        if state.get("work_id") != self.work_id:
            raise WorkOrderError("work-order state work_id mismatch")
        if not isinstance(state.get("operator_gate_required"), bool):
            raise WorkOrderError("operator_gate_required must be a boolean")
        steps = state.get("steps")
        if not isinstance(steps, dict) or set(steps) != set(self.STEPS):
            raise WorkOrderError("invalid work-order step set")
        self._reject_secrets(state)

    def _reject_secrets(self, value, path="state") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in self.FORBIDDEN_KEYS:
                    raise WorkOrderError(f"secret-bearing field is forbidden: {path}.{key}")
                self._reject_secrets(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._reject_secrets(child, f"{path}[{index}]")

    def _expected_step(self, state: dict) -> str | None:
        return next((name for name in self.STEPS if not state["steps"][name]["completed"]), None)

    def complete_step(self, state: dict, step: str, result: dict) -> dict:
        self._validate_state(state)
        if state.get("status") == "DONE":
            raise WorkOrderError("work order is already complete")
        if step not in self.STEPS:
            raise WorkOrderError(f"unknown work-order step: {step}")
        expected = self._expected_step(state)
        if step != expected:
            raise WorkOrderError(f"cannot complete {step}; expected {expected}")
        self._reject_secrets(result, f"result.{step}")

        updated = json.loads(json.dumps(state))
        updated["steps"][step] = {"completed": True, "result": result}
        updated["status"] = "DONE" if self._expected_step(updated) is None else "IN_PROGRESS"
        return updated

    def skip_step(self, state: dict, step: str, *, reason) -> dict:
        self._validate_state(state)
        if state.get("status") == "DONE":
            raise WorkOrderError("work order is already complete")
        if step != "operator_gate":
            raise WorkOrderError(f"only operator_gate can be skipped, not {step}")
        if state["operator_gate_required"]:
            raise WorkOrderError("operator_gate is required for this work order and cannot be skipped")
        expected = self._expected_step(state)
        if step != expected:
            raise WorkOrderError(f"cannot skip {step}; expected {expected}")
        self._reject_secrets(reason, f"skip.{step}")

        updated = json.loads(json.dumps(state))
        updated["steps"][step] = {"completed": True, "result": None, "skipped": {"reason": reason}}
        updated["status"] = "DONE" if self._expected_step(updated) is None else "IN_PROGRESS"
        return updated

    def _classify_step_state(self, step_entry: dict) -> dict:
        if not step_entry.get("completed"):
            return {"status": "pending"}
        if "skipped" in step_entry:
            return {"status": "skipped-not-applicable", "reason": step_entry["skipped"]["reason"]}
        return {"status": "complete"}

    def state(self, state: dict) -> dict:
        """Canonical read-only view, derived entirely from the persisted
        representation -- never mutates or persists `state`."""
        self._validate_state(state)
        return {
            "status": state["status"],
            "current_step": self._expected_step(state),
            "steps": {step: self._classify_step_state(state["steps"][step]) for step in self.STEPS},
        }

    def write_state(self, path: Path, state: dict) -> None:
        self._validate_state(state)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)

    def read_state(self, path: Path) -> dict:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        self._validate_state(state)
        return state


def default_state_path(work_id: str) -> Path:
    """The contracts.md-speced _studio/.local/runs/<work-id>/ layout,
    matching M0's studio_delegation_pilot.write_run_record convention
    (that module has since been removed as dead code; this layout
    convention it established is still current)."""
    return Path("_studio/.local/runs") / work_id / "work-order-state.json"
