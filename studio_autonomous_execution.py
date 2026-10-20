#!/usr/bin/env python3
"""Autonomous Execution Contract — provider-neutral task lifecycle.

States (exact literals):
  INTAKE → CLARIFYING → READY → EXECUTING → VERIFYING → DONE | BLOCKED | GATED

This is NOT a second planner, task manager, ticket database, Master Plan,
or source of truth. Clarification reuses studio_planning_onramp
(CLEAR / MATERIAL_AMBIGUITY / GRILL_WITH_DOCS / WAYFINDER → return_to_router).
Execution input is a frozen work order; Front Door route_entry remains the
router after clarification. Critic and Operator gates reuse existing
contracts (operator_gate_required / studio-gate OPERATOR_APPROVAL_REQUIRED);
no new approval category is invented.

Install/update recovery stays in studio_update / installer — not folded
into this lifecycle.

Shipped in FRONT_DOOR_RUNTIME_MODULES because studio_front_door imports it
(same packaging precedent as studio_planning_onramp).

The Front Door drives this lifecycle as the single canonical governed
execution path: INTAKE → planning/clarification → freeze → route_entry →
execute (DIRECT inline / DELEGATE|DIRECTOR via ParallelCoordinator) →
verify → Critic → required Operator gate → DONE | BLOCKED | GATED.
Material clarification is bounded (MAX_MATERIAL_CLARIFICATIONS); a repeated
material clarification becomes UNRESOLVABLE_BLOCKER. Bare critic_verdict
strings are not Critic evidence — verdicts must bind to task/work-order/node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from studio_planning_onramp import (
    CLEAR,
    resolve_planning_onramp,
    result_as_dict as planning_onramp_result_as_dict,
)
from studio_factory_run import (
    FactoryRunEngine,
    FactoryRunError,
    FactoryRunResult,
    PhaseSpec,
    boolean_only_done_attempt,
    build_done_evidence_from_factory,
)
from studio_decision_provider import DecisionEvidence, resolve_decision
from studio_runtime_observability import RuntimeObservabilityLedger
from studio_parallel_coordinator import (
    JOIN_JOINED,
    OVERLAP_REFUSE,
    ParallelCoordinator,
    ParallelCoordinatorError,
    STATUS_DONE,
    STATUS_RUNNING,
    WorktreeIdentity,
    normalize_owned_path,
    overlapping_paths,
)

# Lifecycle states
INTAKE = "INTAKE"
CLARIFYING = "CLARIFYING"
READY = "READY"
EXECUTING = "EXECUTING"
VERIFYING = "VERIFYING"
DONE = "DONE"
BLOCKED = "BLOCKED"
GATED = "GATED"

LIFECYCLE_STATES = frozenset({
    INTAKE, CLARIFYING, READY, EXECUTING, VERIFYING, DONE, BLOCKED, GATED,
})

TERMINAL_STATES = frozenset({DONE, BLOCKED, GATED})

# Legal transitions (source → frozenset of targets)
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    INTAKE: frozenset({CLARIFYING, READY}),
    CLARIFYING: frozenset({READY}),
    READY: frozenset({EXECUTING}),
    EXECUTING: frozenset({VERIFYING, BLOCKED, GATED, CLARIFYING}),
    VERIFYING: frozenset({DONE, BLOCKED, GATED, CLARIFYING}),
    DONE: frozenset(),
    BLOCKED: frozenset(),
    GATED: frozenset(),
}

# Interruption classes — only these may interrupt EXECUTING / VERIFYING
MANDATORY_OPERATOR_GATE = "MANDATORY_OPERATOR_GATE"
UNRESOLVABLE_BLOCKER = "UNRESOLVABLE_BLOCKER"
NEW_MATERIAL_PRODUCT_DECISION = "NEW_MATERIAL_PRODUCT_DECISION"

INTERRUPTION_CLASSES = frozenset({
    MANDATORY_OPERATOR_GATE,
    UNRESOLVABLE_BLOCKER,
    NEW_MATERIAL_PRODUCT_DECISION,
})

# Existing Operator gate kinds — do not invent a new approval category.
# Mirrors work-order.schema.json operator_gate_required and studio-gate
# OPERATOR_APPROVAL_REQUIRED output.
EXISTING_OPERATOR_GATE_KINDS = frozenset({
    "operator_gate_required",
    "OPERATOR_APPROVAL_REQUIRED",
})

# Critic accepted vocabulary (same literals as studio_scm_governance;
# that module is kit-repo-only and must not enter the Front Door closure).
ACCEPTED_CRITIC_VERDICTS = frozenset({"PASS", "APPROVE"})

# Agreed clarification bound: one material clarification phase per task.
# A repeated material clarification becomes UNRESOLVABLE_BLOCKER (no loop).
MAX_MATERIAL_CLARIFICATIONS = 1

# Phases where an unclassified clarification request is rejected fail-closed
_EXECUTION_PHASES = frozenset({EXECUTING, VERIFYING})

_PHASESPEC_FIELDS = frozenset(PhaseSpec.__dataclass_fields__)


class AutonomousExecutionError(RuntimeError):
    """Fail-closed contract violation (illegal transition, unclassified ask, …)."""


@dataclass
class InterruptionRecord:
    interruption_class: str
    phase: str
    reason: str
    required_decision_or_action: str
    required: bool  # False ⇒ avoidable (should never be emitted by API)

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": self.interruption_class,
            "phase": self.phase,
            "reason": self.reason,
            "required_decision_or_action": self.required_decision_or_action,
            "required": self.required,
        }


class _AdmittedProviderBinding:
    """Runs only the provider admission selected for this workstream."""

    def __init__(self, provider_id: str, execute_fn: Any, model: str | None = None) -> None:
        self._provider_id = provider_id
        self._execute_fn = execute_fn
        self._model = model

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def runtime_id(self) -> str:
        return "admitted-provider"

    @property
    def continuation_supported(self) -> bool:
        return False

    def execute(self, request: Any) -> Any:
        from dataclasses import replace
        if self._model and getattr(request, "selected_model", None) is None:
            request = replace(request, selected_model=self._model)
        result = self._execute_fn(request)
        if self._model and getattr(result, "model", None) in (None, ""):
            try:
                result.model = self._model
            except Exception:
                pass
        if getattr(result, "provider", None) in (None, ""):
            try:
                result.provider = self._provider_id
            except Exception:
                pass
        return result


def _rolling_completed(task) -> list[str]:
    """Current passes plus durable and previously admitted completions."""
    completed: list[str] = []
    for run in task.workstream_runs:
        if run.get("passed"):
            work_id = (run.get("evidence") or {}).get("work_id")
            if isinstance(work_id, str) and work_id not in completed:
                completed.append(work_id)
    for work_id in getattr(task, "_admission_completed", None) or []:
        if isinstance(work_id, str) and work_id not in completed:
            completed.append(work_id)
    for work_id, record in (getattr(task, "_durable_completions", None) or {}).items():
        if isinstance(work_id, str) and work_id not in completed and isinstance(record, Mapping) and record.get("passed", True):
            completed.append(work_id)
    return completed



@dataclass
class AutonomousExecutionTask:
    """Smallest coherent executable lifecycle for one Operator task.

    Callers supply planning signals and (when ready) a decomposition to
    freeze. The contract refuses avoidable mid-execution questions and
    refuses DONE without provenance-bearing factory-run evidence (boolean-only self-reports are rejected).
    """

    task_id: str
    state: str = INTAKE
    planning_onramp: dict[str, Any] | None = None
    clarification_questions: list[str] = field(default_factory=list)
    discovered_facts: list[str] = field(default_factory=list)
    frozen_work_order: list[dict[str, Any]] | None = None
    entry_route: dict[str, Any] | None = None
    critic_required: bool | None = None
    interruptions: list[InterruptionRecord] = field(default_factory=list)
    residual_uncertainty: list[str] = field(default_factory=list)
    evidence: dict[str, Any] | None = None
    prepared_gate_action: dict[str, Any] | None = None
    blocker: dict[str, Any] | None = None
    factory_engine: FactoryRunEngine | None = None
    factory_run: dict[str, Any] | None = None
    worktree_cwd: str | None = None
    profile_id: str | None = None
    profile_context: dict[str, Any] | None = None
    observability_ledger: RuntimeObservabilityLedger | None = None
    decision_evidence: list[dict[str, Any]] = field(default_factory=list)
    material_clarification_rounds: int = 0
    coordinator: ParallelCoordinator | None = None
    coordinator_dispatch: dict[str, Any] | None = None
    workstream_runs: list[dict[str, Any]] = field(default_factory=list)
    operator_authorization: dict[str, Any] | None = None

    def intake(
        self,
        signals: Mapping[str, Any],
        *,
        discoverable_facts: Sequence[str] | None = None,
        proposed_decomposition: Any = None,
        availability: Mapping[str, Any] | None = None,
        profile_id: str | None = None,
        observability_ledger: RuntimeObservabilityLedger | None = None,
        admission_capacity: int | None = None,
        provider_health: Mapping[str, Any] | None = None,
        admission_completed: Sequence[str] | None = None,
        already_admitted: Mapping[str, Any] | None = None,
        provider_executors: Mapping[str, Any] | None = None,
        durable_completions: Mapping[str, Any] | None = None,
        authoritative_completions: Mapping[str, Any] | None = None,
    ) -> "AutonomousExecutionTask":
        """INTAKE: classify via planning on-ramp; batch Operator questions.

        Discoverable facts are recorded and never become Operator questions.
        CLEAR (or ambiguity fully covered by discoverable facts) freezes
        when a decomposition is supplied and moves to READY; otherwise
        CLARIFYING.
        """
        if self.state != INTAKE:
            raise AutonomousExecutionError(
                f"intake only from INTAKE, not {self.state}"
            )

        self._admission_capacity = admission_capacity
        self._provider_health = provider_health
        self._admission_completed = admission_completed
        self._already_admitted = already_admitted
        self._provider_executors = dict(provider_executors or {})
        # Caller-submitted completion JSON is not authority. Only records this
        # runtime resolved from previously emitted Factory evidence are.
        self._durable_completions = dict(authoritative_completions or {})
        if observability_ledger is not None:
            self.observability_ledger = observability_ledger
        if profile_id is not None:
            self.profile_id = profile_id

        facts = [str(f) for f in (discoverable_facts or ())]
        self.discovered_facts = list(facts)

        onramp = resolve_planning_onramp(
            signals,
            availability=availability,
            decomposition=None,
            include_entry_route=False,
            profile_id=self.profile_id,
        )
        self.planning_onramp = planning_onramp_result_as_dict(onramp)
        self.profile_context = onramp.profile_context

        raw_asks: list[str] = []
        if onramp.asks_questions and onramp.next_action:
            raw_asks = list(onramp.next_action.get("ask_operator") or [])

        fact_set = {f.casefold() for f in facts}
        operator_asks = [
            q for q in raw_asks
            if not _question_covered_by_discoverable(q, fact_set)
        ]

        needs_operator = onramp.onramp_state != CLEAR and bool(operator_asks)

        self.clarification_questions = operator_asks if needs_operator else []

        if needs_operator:
            self._enter_clarifying("intake material clarification")
            return self

        if proposed_decomposition is None:
            raise AutonomousExecutionError(
                "CLEAR / no-Operator-ask intake requires proposed_decomposition to freeze"
            )
        self._freeze(proposed_decomposition)
        self._transition(READY)
        return self

    def complete_clarification(
        self,
        *,
        operator_answers: Mapping[str, Any],
        proposed_decomposition: Any,
    ) -> "AutonomousExecutionTask":
        """CLARIFYING → READY: answers collected; freeze work order."""
        if self.state != CLARIFYING:
            raise AutonomousExecutionError(
                f"complete_clarification only from CLARIFYING, not {self.state}"
            )
        if not isinstance(operator_answers, Mapping) or not operator_answers:
            raise AutonomousExecutionError(
                "operator_answers must be a non-empty mapping of Operator-owned decisions"
            )
        self.clarification_questions = []
        self._freeze(proposed_decomposition)
        self._transition(READY)
        return self

    def _freeze(self, decomposition: Any) -> None:
        from studio_director_bridge import (
            DirectorBridgeError,
            _normalize_decomposition,
            build_plan_from_decomposition,
        )
        from studio_front_door import route_entry

        try:
            work_orders = _normalize_decomposition(decomposition)
            if not work_orders:
                self.frozen_work_order = []
                self.critic_required = False
                self.entry_route = route_entry(
                    [],
                    admission_capacity=getattr(self, "_admission_capacity", None),
                    provider_health=getattr(self, "_provider_health", None),
                    completed=getattr(self, "_admission_completed", None),
                    already_admitted=getattr(self, "_already_admitted", None),
                )
                return
            build_plan_from_decomposition(work_orders)
        except DirectorBridgeError as exc:
            raise AutonomousExecutionError(f"cannot freeze work order: {exc}") from exc

        self.frozen_work_order = list(work_orders)
        self.critic_required = any(
            bool(entry.get("operator_gate_required")) for entry in work_orders
        )
        self.entry_route = route_entry(
            work_orders,
            admission_capacity=getattr(self, "_admission_capacity", None),
            provider_health=getattr(self, "_provider_health", None),
            completed=getattr(self, "_admission_completed", None),
            already_admitted=getattr(self, "_already_admitted", None),
        )

    def begin_execution(
        self,
        *,
        cwd: str | None = None,
        max_repairs: int = 2,
        agent_producers: Mapping[str, Any] | None = None,
        provider_bindings: Mapping[str, Any] | None = None,
    ) -> "AutonomousExecutionTask":
        if self.state != READY:
            raise AutonomousExecutionError(
                f"begin_execution only from READY, not {self.state}"
            )
        if self.frozen_work_order is None:
            raise AutonomousExecutionError("work order must be frozen before EXECUTING")
        if cwd is None:
            raise AutonomousExecutionError(
                "begin_execution requires cwd (git worktree) for factory-run evidence"
            )
        self.worktree_cwd = str(cwd)
        self.factory_engine = FactoryRunEngine(
            task_id=self.task_id,
            cwd=cwd,
            max_repairs=max_repairs,
            agent_producers=agent_producers,
            provider_bindings=provider_bindings,
            observability_ledger=self.observability_ledger,
        )
        self._transition(EXECUTING)
        return self

    def begin_verification(self) -> "AutonomousExecutionTask":
        if self.state != EXECUTING:
            raise AutonomousExecutionError(
                f"begin_verification only from EXECUTING, not {self.state}"
            )
        self._transition(VERIFYING)
        return self

    def execute_phases(
        self,
        phases: Sequence[PhaseSpec],
    ) -> FactoryRunResult:
        """Run classified phases during EXECUTING via the factory runner."""
        if self.state != EXECUTING:
            raise AutonomousExecutionError(
                f"execute_phases only from EXECUTING, not {self.state}"
            )
        return self._run_factory_phases(phases, mark_verification=False)

    def verify_phases(
        self,
        phases: Sequence[PhaseSpec],
    ) -> FactoryRunResult:
        """Run deterministic verification phases during VERIFYING."""
        if self.state != VERIFYING:
            raise AutonomousExecutionError(
                f"verify_phases only from VERIFYING, not {self.state}"
            )
        return self._run_factory_phases(phases, mark_verification=True)

    def _run_factory_phases(
        self,
        phases: Sequence[PhaseSpec],
        *,
        mark_verification: bool,
    ) -> FactoryRunResult:
        if self.factory_engine is None:
            raise AutonomousExecutionError("factory engine not initialized")
        try:
            result = self.factory_engine.run_phases(
                phases, mark_verification=mark_verification,
            )
        except FactoryRunError as exc:
            raise AutonomousExecutionError(str(exc)) from exc
        self.factory_run = result.as_dict()
        if result.unresolvable:
            # Exceeded repair bound / failed deterministic phase → existing class
            self.interrupt(
                interruption_class=UNRESOLVABLE_BLOCKER,
                reason=str(result.unresolvable.get("reason") or "factory phase failed"),
                required_decision_or_action=(
                    f"resolve blocker for phase "
                    f"{result.unresolvable.get('phase_id')!r}"
                ),
            )
        return result

    def request_clarification(self, *, question: str, phase: str | None = None) -> None:
        """Reject unclassified clarification during EXECUTING/VERIFYING."""
        phase = phase or self.state
        if phase in _EXECUTION_PHASES:
            raise AutonomousExecutionError(
                "unclassified clarification during "
                f"{phase} is an avoidable interruption and is rejected"
            )
        raise AutonomousExecutionError(
            f"clarification outside CLARIFYING is rejected (phase={phase})"
        )

    def interrupt(
        self,
        *,
        interruption_class: str,
        reason: str,
        required_decision_or_action: str,
        gate_kind: str | None = None,
        prepared_action: Mapping[str, Any] | None = None,
    ) -> "AutonomousExecutionTask":
        """Record a required interruption. Fail closed on unknown classes."""
        if interruption_class not in INTERRUPTION_CLASSES:
            raise AutonomousExecutionError(
                f"unclassified interruption {interruption_class!r} rejected; "
                f"allowed: {sorted(INTERRUPTION_CLASSES)}"
            )
        if self.state not in {EXECUTING, VERIFYING, CLARIFYING, READY}:
            raise AutonomousExecutionError(
                f"interrupt not allowed from {self.state}"
            )
        if not reason or not required_decision_or_action:
            raise AutonomousExecutionError(
                "interruption requires reason and required_decision_or_action"
            )

        phase = self.state
        record = InterruptionRecord(
            interruption_class=interruption_class,
            phase=phase,
            reason=reason,
            required_decision_or_action=required_decision_or_action,
            required=True,
        )
        self.interruptions.append(record)

        if interruption_class == MANDATORY_OPERATOR_GATE:
            if gate_kind not in EXISTING_OPERATOR_GATE_KINDS:
                raise AutonomousExecutionError(
                    f"MANDATORY_OPERATOR_GATE requires an existing gate kind "
                    f"in {sorted(EXISTING_OPERATOR_GATE_KINDS)}, got {gate_kind!r}"
                )
            if not prepared_action:
                raise AutonomousExecutionError(
                    "MANDATORY_OPERATOR_GATE requires prepared_action (exact action)"
                )
            self.prepared_gate_action = {
                "gate_kind": gate_kind,
                "action": dict(prepared_action),
                "decision": required_decision_or_action,
            }
            self._transition(GATED)
            return self

        if interruption_class == UNRESOLVABLE_BLOCKER:
            self.blocker = {
                "reason": reason,
                "required_decision_or_action": required_decision_or_action,
                "phase": phase,
            }
            self._transition(BLOCKED)
            return self

        # NEW_MATERIAL_PRODUCT_DECISION — re-enter bounded clarification once.
        # Beyond MAX_MATERIAL_CLARIFICATIONS → UNRESOLVABLE_BLOCKER (no loop).
        if self.material_clarification_rounds >= MAX_MATERIAL_CLARIFICATIONS:
            # Reclassify the just-appended record to the terminal blocker class.
            self.interruptions[-1] = InterruptionRecord(
                interruption_class=UNRESOLVABLE_BLOCKER,
                phase=phase,
                reason=(
                    f"material clarification bound exceeded "
                    f"({MAX_MATERIAL_CLARIFICATIONS}); repeated ask: {reason}"
                ),
                required_decision_or_action=required_decision_or_action,
                required=True,
            )
            self.blocker = {
                "reason": self.interruptions[-1].reason,
                "required_decision_or_action": required_decision_or_action,
                "phase": phase,
                "interruption_class": UNRESOLVABLE_BLOCKER,
                "bound": MAX_MATERIAL_CLARIFICATIONS,
            }
            self._transition(BLOCKED)
            return self
        self.clarification_questions = [required_decision_or_action]
        self._enter_clarifying(required_decision_or_action)
        return self

    def complete_done(
        self,
        evidence: Mapping[str, Any] | None = None,
    ) -> "AutonomousExecutionTask":
        """VERIFYING → DONE only with provenance-bearing factory evidence.

        Caller-asserted boolean-only payloads are rejected fail-closed.
        Bare critic_verdict strings are not Critic evidence — when Critic is
        required, critic_evidence must bind verdict to task/work-order/node.
        When operator_gate_required, DONE also requires operator authorization
        bound to the prepared gated action. Non-gated flows must not invent a gate.
        """
        if self.state != VERIFYING:
            raise AutonomousExecutionError(
                f"complete_done only from VERIFYING, not {self.state}"
            )
        evidence = dict(evidence or {})
        if boolean_only_done_attempt(evidence) and not evidence.get("factory_envelopes"):
            raise AutonomousExecutionError(
                "DONE fails closed: boolean-only self-report rejected; "
                "provenance-bearing factory-run evidence is required"
            )
        if self.factory_engine is None:
            raise AutonomousExecutionError(
                "DONE fails closed: factory engine missing; run execute/verify phases first"
            )

        gate_required = self._operator_gate_required()
        # Releasing the last gate clears operator_gate_required before DONE.
        # The authorization already bound to the prepared action is that gate's evidence.
        released_in_turn = (
            self.prepared_gate_action is not None
            and self.operator_authorization is not None
        )
        if gate_required or released_in_turn:
            self._enforce_operator_authorization_for_done(evidence)
            gate_required = True
        elif evidence.get("operator_authorization") and self.prepared_gate_action is None:
            raise AutonomousExecutionError(
                "non-gated flow must not invent an Operator gate"
            )

        if self.critic_required:
            supplied = evidence.get("critic_evidence")
            supplied = dict(supplied) if isinstance(supplied, dict) else {}
            bound = _require_bound_critic_evidence(
                evidence,
                task_id=self.task_id,
                frozen_work_order=self.frozen_work_order or [],
            )
            evidence = dict(evidence)
            evidence["critic_verdict"] = bound["verdict"]
            evidence["critic_evidence"] = {**supplied, **bound}

        if self.critic_required and isinstance(evidence.get("critic_evidence"), dict):
            ce = dict(evidence["critic_evidence"])
            fp = self.factory_engine.verification_fingerprint
            if fp and not ce.get("content_fingerprint") and not ce.get("verification_fingerprint"):
                ce["content_fingerprint"] = fp
                ce["verification_fingerprint"] = fp
            evidence["critic_evidence"] = ce
        if gate_required and self.operator_authorization is not None:
            from datetime import datetime, timezone
            auth = dict(self.operator_authorization)
            evidence["operator_authorization"] = {
                "interruption_class": "MANDATORY_OPERATOR_GATE",
                "authorized": True,
                "prepared_gate_action": dict(self.prepared_gate_action or {}),
                "timestamp": auth.get("timestamp") or auth.get("authorized_at") or datetime.now(timezone.utc).isoformat(),
                "authorized_by": auth.get("authorized_by"),
            }
        try:
            validated = build_done_evidence_from_factory(
                engine=self.factory_engine,
                critic_required=bool(self.critic_required),
                operator_gate_required=gate_required,
                prepared_gate_action=self.prepared_gate_action if gate_required else None,
                supplemental=evidence,
            )
        except FactoryRunError as exc:
            raise AutonomousExecutionError(str(exc)) from exc
        if evidence.get("critic_evidence"):
            self._require_critic_revision_at_head(evidence["critic_evidence"])
            validated["critic_evidence"] = dict(evidence["critic_evidence"])
        if self.operator_authorization is not None:
            validated["operator_authorization"] = dict(self.operator_authorization)
        elif evidence.get("operator_authorization"):
            validated["operator_authorization"] = dict(evidence["operator_authorization"])
        self.evidence = validated
        self.residual_uncertainty = list(validated.get("residual_uncertainty") or [])
        self.factory_run = {
            "envelopes": validated.get("factory_envelopes"),
            "ledger": validated.get("factory_ledger"),
            "verification_fingerprint": validated.get("verification_fingerprint"),
            "content_fingerprint": validated.get("content_fingerprint"),
        }
        self._transition(DONE)
        return self

    def _require_critic_revision_at_head(self, critic_evidence: Mapping[str, Any]) -> None:
        """Critic evidence proves the git HEAD of the work it binds, read at DONE (#134)."""
        work_id = (
            critic_evidence.get("work_order_id")
            or critic_evidence.get("work_id")
            or critic_evidence.get("node_id")
        )
        worktrees = {
            run["evidence"]["work_id"]: run["engine"].cwd
            for run in self.workstream_runs
            if run.get("passed") and run.get("engine") is not None
        }
        if worktrees and work_id not in worktrees:
            raise AutonomousExecutionError(
                f"DONE fails closed: critic_evidence work {work_id!r} has no worktree in this run"
            )
        cwd = worktrees[work_id] if worktrees else self.factory_engine.cwd
        expected = _git_head(cwd)
        if not expected:
            raise AutonomousExecutionError(
                f"DONE fails closed: git HEAD unavailable for critic_evidence work {work_id!r}"
            )
        received = str(critic_evidence.get("verified_revision") or "").strip()
        if received.lower() != expected:
            raise AutonomousExecutionError(
                f"DONE fails closed: critic_evidence.verified_revision {received!r} does not match "
                f"work {work_id!r} worktree HEAD {expected!r}"
            )

    def operator_attention(self) -> dict[str, Any]:
        required = [i for i in self.interruptions if i.required]
        avoidable = [i for i in self.interruptions if not i.required]
        total = len(self.interruptions)
        return {
            "total_interruptions": total,
            "required_interruptions": len(required),
            "avoidable_interruptions": len(avoidable),
            "interruptions": [i.as_dict() for i in self.interruptions],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "planning_onramp": self.planning_onramp,
            "clarification_questions": list(self.clarification_questions),
            "discovered_facts": list(self.discovered_facts),
            "frozen_work_order": self.frozen_work_order,
            "entry_route": self.entry_route,
            "critic_required": self.critic_required,
            "prepared_gate_action": self.prepared_gate_action,
            "blocker": self.blocker,
            "residual_uncertainty": list(self.residual_uncertainty),
            "evidence": self.evidence,
            "factory_run": self.factory_run,
            "worktree_cwd": self.worktree_cwd,
            "profile_id": self.profile_id,
            "profile_context": self.profile_context,
            "decision_evidence": list(self.decision_evidence),
            "material_clarification_rounds": self.material_clarification_rounds,
            "coordinator_dispatch": self.coordinator_dispatch,
            "operator_authorization": self.operator_authorization,
            "operator_attention": self.operator_attention(),
        }

    def consult_decision(
        self,
        raw_request: Mapping[str, Any],
        *,
        provider: Any | None = None,
        use_jev: bool = False,
        jev_transport: Any | None = None,
    ) -> DecisionEvidence:
        """Optional bounded decision consult — evidence only, never authority.

        DONE still requires factory envelopes; this evidence cannot alone mark
        DONE. Unavailable JEV / missing provider is GAP (passed=False).
        """
        if use_jev:
            from studio_jev_decision_provider import decide_with_jev

            evidence = decide_with_jev(raw_request, transport=jev_transport)
        else:
            evidence = resolve_decision(raw_request, provider=provider)
        payload = evidence.as_dict()
        self.decision_evidence.append(payload)
        if self.observability_ledger is not None:
            req_id = (evidence.provenance or {}).get("request_id") or "none"
            self.observability_ledger.record(
                task_id=self.task_id,
                event="phase_end",
                phase_id="bounded_decision",
                producer_kind="AGENT",
                state=self.state,
                provider=evidence.provider_id,
                evidence_references=[f"decision:{evidence.status}:{req_id}"],
                gate_result="pass" if evidence.passed else "fail",
            )
        return evidence

    def _observe_state(self, new_state: str) -> None:
        if self.observability_ledger is None:
            return
        self.observability_ledger.set_state(self.task_id, new_state)
        self.observability_ledger.record(
            task_id=self.task_id,
            event="phase_end" if new_state in TERMINAL_STATES else "phase_start",
            phase_id=f"lifecycle:{new_state}",
            producer_kind="CODE",
            state=new_state,
            evidence_references=[f"autonomous-state:{new_state}"],
        )


    def _transition(self, new_state: str) -> None:
        allowed = LEGAL_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise AutonomousExecutionError(
                f"illegal transition {self.state} → {new_state}"
            )
        self.state = new_state
        self._observe_state(new_state)

    def _enter_clarifying(self, reason: str) -> None:
        if self.material_clarification_rounds >= MAX_MATERIAL_CLARIFICATIONS:
            raise AutonomousExecutionError(
                f"material clarification bound exceeded ({MAX_MATERIAL_CLARIFICATIONS}): {reason}"
            )
        self.material_clarification_rounds += 1
        self._transition(CLARIFYING)

    def _operator_gate_required(self) -> bool:
        return any(
            bool(entry.get("operator_gate_required"))
            for entry in (self.frozen_work_order or [])
        )

    def prepare_operator_gate(
        self,
        *,
        gate_kind: str,
        prepared_action: Mapping[str, Any],
        required_decision_or_action: str,
    ) -> "AutonomousExecutionTask":
        """Record the exact gated action without inventing a new gate category.

        Non-gated decompositions must not call this.
        """
        if not self._operator_gate_required():
            raise AutonomousExecutionError(
                "non-gated flow must not invent an Operator gate"
            )
        if gate_kind not in EXISTING_OPERATOR_GATE_KINDS:
            raise AutonomousExecutionError(
                f"prepare_operator_gate requires an existing gate kind "
                f"in {sorted(EXISTING_OPERATOR_GATE_KINDS)}, got {gate_kind!r}"
            )
        if not prepared_action:
            raise AutonomousExecutionError(
                "prepare_operator_gate requires prepared_action (exact action)"
            )
        self.prepared_gate_action = {
            "gate_kind": gate_kind,
            "action": dict(prepared_action),
            "decision": required_decision_or_action,
        }
        return self

    def authorize_operator_gate(
        self,
        authorization: Mapping[str, Any],
    ) -> "AutonomousExecutionTask":
        """Bind Operator authorization to the prepared gated action."""
        if self.prepared_gate_action is None:
            raise AutonomousExecutionError(
                "authorize_operator_gate requires prepared_gate_action first"
            )
        if not isinstance(authorization, Mapping) or not authorization:
            raise AutonomousExecutionError(
                "operator authorization must be a non-empty mapping"
            )
        bound = _bind_operator_authorization(
            authorization,
            prepared=self.prepared_gate_action,
            task_id=self.task_id,
        )
        self.operator_authorization = bound
        return self

    def _enforce_operator_authorization_for_done(
        self,
        evidence: Mapping[str, Any],
    ) -> None:
        if self.prepared_gate_action is None:
            raise AutonomousExecutionError(
                "DONE fails closed: operator_gate_required without prepared gated action"
            )
        auth = evidence.get("operator_authorization") or self.operator_authorization
        if not auth:
            raise AutonomousExecutionError(
                "DONE fails closed: operator_gate_required needs operator authorization "
                "bound to the prepared gated action"
            )
        bound = _bind_operator_authorization(
            auth,
            prepared=self.prepared_gate_action,
            task_id=self.task_id,
        )
        self.operator_authorization = bound

    def dispatch_route(
        self,
        *,
        worktrees: Mapping[str, WorktreeIdentity | Mapping[str, str]] | None = None,
        overlap_policy: str = OVERLAP_REFUSE,
    ) -> dict[str, Any]:
        """Apply the frozen entry_route during EXECUTING.

        DIRECT: no coordinator tasks. DELEGATE / DIRECTOR: one coordinator
        claim per delegation with normalized owned_paths and worktree identity.
        Overlapping owned_paths are refused before concurrent dispatch.
        """
        if self.state != EXECUTING:
            raise AutonomousExecutionError(
                f"dispatch_route only from EXECUTING, not {self.state}"
            )
        if self.entry_route is None or self.frozen_work_order is None:
            raise AutonomousExecutionError("dispatch_route requires frozen route + work order")

        path = self.entry_route.get("path")
        delegations = list(self.entry_route.get("delegations") or [])
        if path == "direct":
            if delegations:
                raise AutonomousExecutionError(
                    "DIRECT route must not invent coordinator work"
                )
            result = {
                "path": path,
                "coordinator_used": False,
                "tasks": [],
                "join": None,
            }
            self.coordinator_dispatch = result
            return result

        # Admission decides which delegations may claim. Overlap, capacity,
        # rate limits, gates, and unmet dependencies affect only that item.
        prepared: list[tuple[dict[str, Any], tuple[str, ...]]] = []
        for delegation in delegations:
            for work_id in delegation["work_ids"]:
                narrowed = dict(delegation)
                narrowed["work_ids"] = [work_id]
                owned = owned_paths_for_delegation(narrowed, self.frozen_work_order)
                normalized = normalize_dispatch_owned_paths(owned)
                prepared.append((narrowed, normalized))
        from studio_parallel_coordinator import ADMISSION_ADMITTED
        admission = (self.entry_route or {}).get("admission") or {
            "capacity": len(prepared),
            "admitted_count": len(prepared),
            "results": [
                {"work_id": delegation["work_ids"][0], "disposition": ADMISSION_ADMITTED}
                for delegation, _normalized in prepared
            ],
        }
        admitted_ids = {
            row["work_id"]
            for row in admission.get("results") or []
            if row.get("disposition") == ADMISSION_ADMITTED
        }
        prepared = [
            item for item in prepared
            if item[0]["work_ids"][0] in admitted_ids
        ]

        coord = ParallelCoordinator(overlap_policy=overlap_policy)
        self.coordinator = coord
        worktrees = dict(worktrees or getattr(self, "_dispatch_worktrees", None) or {})
        self._dispatch_worktrees = worktrees
        claimed: list[dict[str, Any]] = []
        for index, (delegation, normalized) in enumerate(prepared):
            task_id = f"{self.task_id}:deleg:{index}:{delegation['work_ids'][0]}"
            work_id = delegation["work_ids"][0] if len(delegation["work_ids"]) == 1 else None
            # task_id must stay distinct from work_id
            if work_id is not None and work_id == task_id:
                task_id = f"{task_id}:coord"
            try:
                claim = coord.claim_task(
                    task_id=task_id,
                    owned_paths=normalized,
                    work_id=work_id,
                )
            except ParallelCoordinatorError as exc:
                raise AutonomousExecutionError(str(exc)) from exc
            wt_key = delegation["work_ids"][0]
            wt = worktrees.get(task_id) or worktrees.get(wt_key) or worktrees.get(str(index))
            if wt is not None:
                coord.bind_worktree(task_id, wt)
            coord.set_status(task_id, STATUS_RUNNING)
            claimed.append({
                "task_id": task_id,
                "delegation": delegation,
                "owned_paths": list(normalized),
                "claim": claim,
            })

        # Claimed tasks stay RUNNING. Route creation is not completion.
        result = {
            "path": path,
            "coordinator_used": bool(claimed),
            "tasks": claimed,
            "join": None,
            "admission": admission,
        }
        self.coordinator_dispatch = result
        return result

    def _admission_row(self, work_id: str) -> dict[str, Any]:
        rows = ((self.entry_route or {}).get("admission") or {}).get("results") or []
        for row in rows:
            if row.get("work_id") == work_id:
                return row
        return {}

    def _require_execution_cwd(self, item: Mapping[str, Any], snapshot: Mapping[str, Any]) -> tuple[str | None, dict | None, str | None]:
        """Return cwd, worktree identity, and a fail-closed error.

        A worktree-isolated claim never runs in the parent cwd. A recorded
        worktree identity is used only when that path and git index exist.
        """
        from pathlib import Path
        import subprocess

        delegation = item["delegation"]
        worktree = snapshot.get("worktree") if isinstance(snapshot, Mapping) else None
        isolated = delegation.get("isolation") == "worktree" or bool(worktree)
        if not isolated:
            return str(self.worktree_cwd), None, None
        if not isinstance(worktree, Mapping):
            return None, None, "worktree-isolated workstream has no worktree identity"
        path = worktree.get("path")
        index = worktree.get("index_path")
        if not isinstance(path, str) or not Path(path).is_dir():
            return None, dict(worktree), f"worktree path is not a directory: {path!r}"
        if not isinstance(index, str) or not Path(index).is_file():
            return None, dict(worktree), f"worktree index is not a file: {index!r}"
        probe = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, env=_git_subprocess_env(),
        )
        if probe.returncode != 0:
            return None, dict(worktree), f"worktree path is not a git worktree: {path!r}"
        if Path(probe.stdout.strip()).resolve() != Path(path).resolve():
            return None, dict(worktree), f"worktree path is not the git toplevel: {path!r}"
        return str(Path(path).resolve()), dict(worktree), None

    def _run_one_admitted(
        self,
        item: dict[str, Any],
        execute_phases: Sequence[Any],
        verify_phases: Sequence[Any],
    ) -> dict[str, Any]:
        from dataclasses import replace

        work_ids = list(item["delegation"]["work_ids"])
        work_id = work_ids[0]
        snapshot = self.coordinator.task(item["task_id"])
        cwd, worktree, worktree_error = self._require_execution_cwd(item, snapshot)
        admission_row = self._admission_row(work_id)
        selected = admission_row.get("provider")
        selected_model = admission_row.get("model")
        model_capacity = admission_row.get("model_capacity") or "unsupported"
        if worktree_error:
            evidence = {
                "work_id": work_id,
                "work_ids": work_ids,
                "task_id": item["task_id"],
                "admit_task_id": f"admit:{work_id}",
                "owned_paths": list(item["owned_paths"]),
                "provider": selected,
                "cwd": None,
                "worktree": worktree,
                "worktree_error": worktree_error,
                "passed": False,
                "incomplete": False,
                "factory_run": None,
                "verification": None,
            }
            item["workstream_evidence"] = evidence
            return {"evidence": evidence, "engine": None, "passed": False, "incomplete": False}

        bindings = dict(self.factory_engine.provider_bindings)
        executor = (getattr(self, "_provider_executors", None) or {}).get(selected)
        if selected and any(getattr(phase, "phase_kind", None) != "CODE" for phase in (*execute_phases, *verify_phases)):
            if executor is not None:
                binding = _AdmittedProviderBinding(str(selected), executor, model=selected_model)
            else:
                from studio_live_provider_runtime import build_live_provider_binding
                binding = build_live_provider_binding(
                    str(selected),
                    **({"model": selected_model} if selected_model else {}),
                )
            for phase in (*execute_phases, *verify_phases):
                if getattr(phase, "phase_kind", None) != "CODE":
                    bindings[phase.producer] = binding
        engine = FactoryRunEngine(
            task_id=f"{self.task_id}:{work_id}",
            cwd=cwd,
            max_repairs=self.factory_engine.max_repairs,
            agent_producers=dict(self.factory_engine.agent_producers),
            provider_bindings=bindings,
            observability_ledger=self.observability_ledger,
        )
        bound_exec = [replace(phase, work_order_id=work_id) for phase in execute_phases]
        bound_verify = [replace(phase, work_order_id=work_id) for phase in verify_phases]
        exec_result = engine.run_phases(bound_exec) if bound_exec else None
        verify_result = None
        incomplete = not bound_exec and not bound_verify
        passed = bool(exec_result is not None and exec_result.all_passed and not exec_result.unresolvable)
        if passed and bound_verify:
            verify_result = engine.run_phases(bound_verify, mark_verification=True)
            passed = bool(verify_result.all_passed and not verify_result.unresolvable)
        elif passed:
            passed = False
            incomplete = True
        evidence = {
            "work_id": work_id,
            "work_ids": work_ids,
            "task_id": item["task_id"],
            "admit_task_id": f"admit:{work_id}",
            "owned_paths": list(item["owned_paths"]),
            "provider": selected,
            "model": selected_model,
            "model_capacity": model_capacity,
            "cwd": str(cwd),
            "worktree": worktree,
            "worktree_error": None,
            "passed": passed,
            "incomplete": incomplete,
            "factory_run": exec_result.as_dict() if exec_result is not None else None,
            "verification": verify_result.as_dict() if verify_result is not None else None,
            "content_fingerprint": engine.last_content_fingerprint,
            "verification_fingerprint": engine.verification_fingerprint,
            "artifact_paths": list(engine._relevant_artifact_paths),
            "provenance": _execution_provenance(selected, selected_model, exec_result),
        }
        violations = workstream_scope_violations(
            cwd,
            list(item["owned_paths"]),
            list(engine._relevant_artifact_paths),
        )
        if violations:
            passed = False
            evidence["passed"] = False
            evidence["scope_violations"] = violations
        item["workstream_evidence"] = evidence
        return {"evidence": evidence, "engine": engine, "passed": passed, "incomplete": incomplete}

    def execute_admitted_workstreams(
        self,
        execute_phases: Sequence[Any],
        verify_phases: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run one FactoryRunEngine per admitted claim, concurrently when several are admitted.

        A shared parent phase result is not copied onto sibling workstreams.
        One workstream's failure does not cancel the others.
        """
        from concurrent.futures import ThreadPoolExecutor

        if self.state != EXECUTING:
            raise AutonomousExecutionError(
                f"execute_admitted_workstreams only from EXECUTING, not {self.state}"
            )
        if not self.coordinator_dispatch or self.coordinator is None:
            raise AutonomousExecutionError(
                "execute_admitted_workstreams requires prior dispatch_route"
            )
        if self.factory_engine is None:
            raise AutonomousExecutionError("factory engine not initialized")

        phases = list(execute_phases)
        verify = list(verify_phases or [])
        runs = self._run_rolling(phases, verify)
        self.workstream_runs = runs
        self.capture_durable_completions()
        for run in reversed(runs):
            if run["evidence"].get("factory_run") is not None:
                self.factory_run = run["evidence"]["factory_run"]
                break
        return [run["evidence"] for run in runs]

    def workstream_evidence(self) -> list[dict[str, Any]]:
        return [dict(run["evidence"]) for run in self.workstream_runs]

    def _exception_run(self, item: Mapping[str, Any], exc: BaseException) -> dict[str, Any]:
        work_ids = list(item["delegation"]["work_ids"])
        work_id = work_ids[0]
        evidence = {
            "work_id": work_id,
            "work_ids": work_ids,
            "task_id": item["task_id"],
            "admit_task_id": f"admit:{work_id}",
            "owned_paths": list(item.get("owned_paths") or []),
            "provider": self._admission_row(work_id).get("provider"),
            "cwd": None,
            "worktree": None,
            "worktree_error": None,
            "passed": False,
            "incomplete": False,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "factory_run": None,
            "verification": None,
        }
        item["workstream_evidence"] = evidence
        return {"evidence": evidence, "engine": None, "passed": False, "incomplete": False}

    def _claim_rolling(self, work_id: str) -> None:
        existing = {
            item["delegation"]["work_ids"][0]
            for item in self.coordinator_dispatch.get("tasks") or []
        }
        if work_id in existing:
            return
        source = next(
            item for item in self.entry_route.get("delegations") or []
            if work_id in item["work_ids"]
        )
        delegation = dict(source)
        delegation["work_ids"] = [work_id]
        normalized = normalize_dispatch_owned_paths(
            owned_paths_for_delegation(delegation, self.frozen_work_order)
        )
        task_id = f"{self.task_id}:deleg:roll:{work_id}"
        claim = self.coordinator.claim_task(
            task_id=task_id,
            owned_paths=normalized,
            work_id=work_id,
        )
        worktree = (getattr(self, "_dispatch_worktrees", None) or {}).get(work_id)
        if worktree is not None:
            self.coordinator.bind_worktree(task_id, worktree)
        self.coordinator.set_status(task_id, STATUS_RUNNING)
        self.coordinator_dispatch["tasks"].append({
            "task_id": task_id,
            "delegation": delegation,
            "owned_paths": list(normalized),
            "claim": claim,
        })

    
    def _open_rolling_slots(self, capacity: int, running_ids: set[str], finished_ids: set[str]) -> None:
        from studio_parallel_coordinator import ADMISSION_ADMITTED, admit_workstreams

        items = []
        by_id = {entry["work_id"]: entry for entry in self.frozen_work_order or []}
        for delegation in self.entry_route.get("delegations") or []:
            for work_id in delegation["work_ids"]:
                work_order = by_id.get(work_id, {})
                dependencies = list(work_order.get("dependencies") or [])
                if not dependencies:
                    dependencies = list(delegation.get("dependencies") or [])
                items.append({
                    "work_id": work_id,
                    "owned_paths": list(work_order.get("owned_paths") or []),
                    "dependencies": dependencies,
                    "gated": bool(work_order.get("operator_gate_required")),
                    "providers": list(work_order.get("providers") or ["default"]),
                    "targets": list(work_order.get("targets") or []) or None,
                })
        current = (self.entry_route.get("admission") or {}).get("results") or []
        already = {}
        queued = []
        for row in current:
            disposition = row.get("disposition")
            work_id = row.get("work_id")
            if work_id in running_ids or work_id in finished_ids or disposition in {"ADMITTED", "ALREADY_ADMITTED"}:
                already[work_id] = row
            if disposition == "ADMITTED" and work_id not in running_ids and work_id not in finished_ids:
                queued.append(work_id)
        free = max(capacity - len(running_ids) - len(queued), 0)
        admission = admit_workstreams(
            items,
            capacity=free,
            provider_health=getattr(self, "_provider_health", None),
            completed=_rolling_completed(self),
            already_admitted=already,
        )
        occupied = set(running_ids) | set(finished_ids)
        results = []
        for row in admission.get("results") or []:
            row = dict(row)
            if row.get("work_id") in occupied and row.get("disposition") == "ALREADY_ADMITTED":
                row["disposition"] = "ADMITTED"
            results.append(row)
        admission = dict(admission)
        admission["results"] = results
        admission["admitted_count"] = sum(1 for row in results if row.get("disposition") == "ADMITTED")
        self.entry_route["admission"] = admission
        self.coordinator_dispatch["admission"] = admission
        for row in admission.get("results") or []:
            if row.get("disposition") == ADMISSION_ADMITTED and row["work_id"] not in running_ids | finished_ids:
                self._claim_rolling(row["work_id"])

    def _run_rolling(self, phases: Sequence[Any], verify: Sequence[Any]) -> list[dict[str, Any]]:
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        admission = (self.entry_route or {}).get("admission") or {}
        capacity = admission.get("capacity")
        if capacity is None:
            capacity = max(len(self.coordinator_dispatch.get("tasks") or []), 1)
        elif isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise AutonomousExecutionError(f"admission capacity must be a non-negative int, got {capacity!r}")
        if capacity == 0:
            return []
        finished: list[dict[str, Any]] = []
        finished_ids: set[str] = set()
        started: set[str] = set()
        running: dict[Any, dict[str, Any]] = {}
        pool = ThreadPoolExecutor(max_workers=capacity)
        try:
            while True:
                running_ids = {
                    item["delegation"]["work_ids"][0] for item in running.values()
                }
                self._open_rolling_slots(capacity, running_ids, finished_ids)
                for item in list(self.coordinator_dispatch.get("tasks") or []):
                    work_id = item["delegation"]["work_ids"][0]
                    if work_id in started or len(running) >= capacity:
                        continue
                    future = pool.submit(self._run_one_admitted, item, phases, verify)
                    running[future] = item
                    started.add(work_id)
                if not running:
                    break
                done, _pending = wait(set(running), return_when=FIRST_COMPLETED)
                for future in done:
                    item = running.pop(future)
                    work_id = item["delegation"]["work_ids"][0]
                    try:
                        run = future.result()
                    except Exception as exc:
                        run = self._exception_run(item, exc)
                    finished.append(run)
                    finished_ids.add(work_id)
                    self.workstream_runs = list(finished)
        finally:
            pool.shutdown(wait=True, cancel_futures=False)
        return finished

    def capture_durable_completions(self) -> None:
        store = dict(getattr(self, "_durable_completions", None) or {})
        for run in self.workstream_runs:
            evidence = run.get("evidence") or {}
            engine = run.get("engine")
            if not run.get("passed") or engine is None or not engine.verification_fingerprint:
                continue
            if not evidence.get("worktree"):
                continue
            record = _factory_evidence_record(evidence, engine)
            ref = _write_factory_evidence(record)
            if not ref:
                continue
            evidence["evidence_ref"] = ref
            store[evidence["work_id"]] = record
        self._durable_completions = store

    def _durable_record_is_fresh(self, work_id: str, record: Mapping[str, Any] | None) -> bool:
        from pathlib import Path
        from studio_factory_run import capture_content_fingerprint

        if not isinstance(record, Mapping):
            return False
        if record.get("work_id") != work_id:
            return False
        if not record.get("task_id") or not record.get("run_id"):
            return False
        worktree = record.get("worktree")
        if not isinstance(worktree, Mapping) or not worktree.get("path") or not worktree.get("index_path"):
            return False
        content = record.get("content_fingerprint")
        verification = record.get("verification_fingerprint")
        if not isinstance(content, str) or not content:
            return False
        if not isinstance(verification, str) or not verification:
            return False
        if not isinstance(record.get("provider"), str) or not record.get("provider"):
            return False
        if "session_id" not in record:
            return False
        envelopes = record.get("factory_envelopes")
        if not isinstance(envelopes, list) or not any(
            isinstance(env, Mapping) and env.get("work_order_id") == work_id and env.get("status") == "PASSED"
            for env in envelopes
        ):
            return False
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping) or not provenance:
            return False
        if provenance.get("provider") != record.get("provider"):
            return False
        if "model" not in provenance:
            return False
        if not Path(worktree["path"]).is_dir() or not Path(worktree["index_path"]).is_file():
            return False
        try:
            current = capture_content_fingerprint(
                worktree["path"],
                artifact_paths=list(record.get("artifact_paths") or []),
            )
        except Exception:
            return False
        return current == content

    def contributing_workstreams_fresh(self) -> tuple[bool, str | None]:
        seen: set[str] = set()
        for run in self.workstream_runs:
            if not run.get("passed"):
                continue
            evidence = run.get("evidence") or {}
            work_id = evidence.get("work_id")
            engine = run.get("engine")
            if engine is None or not engine.verification_fingerprint or not engine.verification_is_fresh():
                return False, work_id
            seen.add(work_id)
        for work_id, record in (getattr(self, "_durable_completions", None) or {}).items():
            if work_id in seen:
                continue
            if not self._durable_record_is_fresh(work_id, record):
                return False, work_id
        return True, None

    def join_required_work(self) -> dict[str, Any]:
        """Join every governed work id, not only the currently claimed subset."""
        from studio_parallel_coordinator import (
            ADMISSION_ADMITTED,
            ADMISSION_ALREADY,
            ADMISSION_GATED,
            ADMISSION_REFUSED,
            STATUS_FAILED,
            join_admissions,
        )

        required = [entry["work_id"] for entry in (self.frozen_work_order or [])]
        admission = (self.entry_route or {}).get("admission") or {}
        rows = {row["work_id"]: row for row in admission.get("results") or []}
        completed = set(getattr(self, "_admission_completed", None) or [])
        by_evidence = {run["evidence"]["work_id"]: run for run in self.workstream_runs}
        statuses: dict[str, str] = {}
        for work_id in required:
            row = rows.get(work_id)
            if row is None:
                statuses[work_id] = "PENDING"
                continue
            disposition = row.get("disposition")
            run = by_evidence.get(work_id)
            if disposition in {ADMISSION_ADMITTED, ADMISSION_ALREADY} and run is not None:
                statuses[work_id] = _explicit_work_status(run, work_id)
            elif disposition == ADMISSION_ALREADY:
                record = (getattr(self, "_durable_completions", None) or {}).get(work_id)
                statuses[work_id] = STATUS_DONE if self._durable_record_is_fresh(work_id, record) else "PENDING"
            elif disposition == ADMISSION_ADMITTED:
                statuses[work_id] = "PENDING"
            elif disposition == ADMISSION_REFUSED:
                statuses[work_id] = STATUS_FAILED
            elif disposition == ADMISSION_GATED:
                statuses[work_id] = "GATED"
            else:
                statuses[work_id] = str(disposition or "PENDING")
        join = join_admissions(required=required, statuses=statuses)
        join["statuses"] = statuses
        if self.coordinator_dispatch is None:
            raise AutonomousExecutionError("join_required_work requires prior dispatch_route")
        self.coordinator_dispatch["admission_join"] = join
        self.coordinator_dispatch["join"] = join
        if self.coordinator is not None:
            for item in self.coordinator_dispatch.get("tasks") or []:
                work_id = item["delegation"]["work_ids"][0]
                status = statuses.get(work_id)
                if status == STATUS_DONE:
                    self.coordinator.set_status(item["task_id"], STATUS_DONE)
                    evidence_ref = (
                        (by_evidence.get(work_id) or {}).get("evidence") or {}
                    ).get("evidence_ref") or f"workstream:{self.task_id}:{work_id}"
                    self.coordinator.attach_evidence_ref(item["task_id"], evidence_ref)
                elif status == STATUS_FAILED:
                    self.coordinator.mark_failed(item["task_id"], reason="workstream_not_done")
        return join

    def adopt_workstream_evidence(self) -> None:
        """Fold per-workstream factory envelopes into the parent engine."""
        from pathlib import Path

        if self.factory_engine is None:
            raise AutonomousExecutionError("factory engine not initialized")
        parent = self.factory_engine
        parent.envelopes = []
        parent.ledger = []
        parent.verification_phase_ids = []
        parent._relevant_artifact_paths = []
        last = None
        for run in self.workstream_runs:
            if not run["passed"]:
                continue
            engine = run["engine"]
            parent.envelopes.extend(engine.envelopes)
            parent.ledger.extend(engine.ledger)
            parent.verification_phase_ids.extend(engine.verification_phase_ids)
            if engine.verification_fingerprint:
                last = engine
        if last is None:
            raise AutonomousExecutionError(
                "DONE fails closed: admitted work has no verification evidence"
            )
        parent.verification_fingerprint = last.verification_fingerprint
        parent.last_content_fingerprint = last.last_content_fingerprint
        parent._relevant_artifact_paths = list(last._relevant_artifact_paths)
        parent.cwd = Path(last.cwd)

    def finish_delegations(self, *, passed: bool) -> dict[str, Any]:
        """Join the full required work set. One boolean cannot complete siblings."""
        if self.coordinator is None or not self.coordinator_dispatch:
            raise AutonomousExecutionError("finish_delegations requires prior dispatch_route")
        if self.coordinator_dispatch.get("admission_join") is not None:
            join = self.coordinator_dispatch["join"]
            if passed and join.get("verdict") != JOIN_JOINED:
                raise AutonomousExecutionError(
                    f"required-work join not JOINED: {join.get('verdict')}"
                )
            return join
        if not self.coordinator_dispatch.get("coordinator_used"):
            if (self.entry_route or {}).get("admission"):
                join = self.join_required_work()
                if passed and join.get("verdict") != JOIN_JOINED:
                    raise AutonomousExecutionError(
                        f"required-work join not JOINED: {join.get('verdict')}"
                    )
                return join
            return {"verdict": None, "contributing": []}
        tasks = list(self.coordinator_dispatch["tasks"])
        if len(tasks) != 1:
            raise AutonomousExecutionError(
                "one shared phase result cannot complete multiple admitted workstreams"
            )
        item = tasks[0]
        if passed:
            self.coordinator.set_status(item["task_id"], STATUS_DONE)
            self.coordinator.attach_evidence_ref(
                item["task_id"],
                f"autonomous:{self.task_id}:{item['task_id']}",
            )
        else:
            self.coordinator.mark_failed(item["task_id"], reason="execution_incomplete")
        join = self.join_required_work()
        if passed and join.get("verdict") != JOIN_JOINED:
            raise AutonomousExecutionError(
                f"required-work join not JOINED: {join.get('verdict')}"
            )
        return join

    def join_delegations(self, *, required: Sequence[str] | None = None) -> dict[str, Any]:
        if self.coordinator is None:
            raise AutonomousExecutionError("join_delegations requires prior dispatch_route")
        return self.coordinator.join(required=required)

    def execute_profile_phases(
        self,
        profile_id: str,
        *,
        code_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        agent_producers: Mapping[str, Any] | None = None,
        mark_verification: bool = False,
    ) -> FactoryRunResult:
        """Run profile → PhaseSpec-compatible structures through FactoryRunEngine."""
        specs = build_profile_phase_specs(
            profile_id,
            work_order_id=(self.frozen_work_order or [{}])[0].get("work_id")
            if self.frozen_work_order else None,
            code_overrides=code_overrides,
        )
        if agent_producers and self.factory_engine is not None:
            # Bind additional producers without changing FactoryRunEngine signature.
            self.factory_engine.agent_producers.update(dict(agent_producers))
        if mark_verification:
            return self.verify_phases(specs)
        return self.execute_phases(specs)


def _git_subprocess_env() -> dict[str, str]:
    """Git probes must follow the worktree, not an inherited GIT_DIR."""
    import os
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git_head(cwd: Any) -> str:
    """Full HEAD SHA of the worktree at ``cwd``; empty when git cannot resolve it."""
    import subprocess
    probe = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        env=_git_subprocess_env(),
    )
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _execution_provenance(provider: str | None, model: str | None, exec_result: Any) -> dict[str, Any]:
    """Admission's provider and model stay on the evidence record."""
    provenance: dict[str, Any] = {}
    if exec_result is not None:
        envelopes = exec_result.as_dict().get("envelopes") or []
        if envelopes and isinstance(envelopes[-1], Mapping):
            raw = envelopes[-1].get("provenance") or {}
            if isinstance(raw, Mapping):
                provenance.update(raw)
    provenance["provider"] = provider
    provenance["model"] = model
    return provenance


def normalize_dispatch_owned_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Front-door/autonomous owned_paths normalize before concurrent dispatch.

    Rejects absolute paths and '..' traversal. Coordinator-internal hardening
    remains Worker C's surface — this only gates dispatch at our layer.
    """
    if not paths:
        raise AutonomousExecutionError("owned_paths claim must include at least one path")
    normalized: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise AutonomousExecutionError(
                f"owned_paths entry must be a non-empty string, got {raw!r}"
            )
        cleaned = raw.strip().replace("\\", "/")
        if cleaned.startswith("/") or cleaned.startswith("~"):
            raise AutonomousExecutionError(
                f"owned_paths must be relative (reject absolute): {raw!r}"
            )
        # Windows drive-letter absolute paths
        if len(cleaned) >= 2 and cleaned[1] == ":" and cleaned[0].isalpha():
            raise AutonomousExecutionError(
                f"owned_paths must be relative (reject absolute): {raw!r}"
            )
        parts = [p for p in cleaned.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise AutonomousExecutionError(
                f"owned_paths must not contain '..': {raw!r}"
            )
        try:
            normalized.append(normalize_owned_path("/".join(parts) if parts else cleaned))
        except ParallelCoordinatorError as exc:
            raise AutonomousExecutionError(str(exc)) from exc
    if len(set(normalized)) != len(normalized):
        raise AutonomousExecutionError(f"duplicate owned_paths after normalize: {normalized}")
    return tuple(normalized)




def _explicit_work_status(run: Mapping[str, Any], work_id: str) -> str:
    """DONE only when this run's envelopes name that work id."""
    from studio_parallel_coordinator import STATUS_FAILED

    evidence = run.get("evidence") or {}
    if evidence.get("work_id") != work_id:
        return "PENDING"
    envelopes = ((evidence.get("factory_run") or {}).get("envelopes") or [])
    verification = ((evidence.get("verification") or {}).get("envelopes") or [])
    bound = [env for env in [*envelopes, *verification] if isinstance(env, Mapping)]
    if bound and not any(env.get("work_order_id") == work_id for env in bound):
        return "PENDING"
    if run.get("incomplete"):
        return "PENDING"
    if run.get("passed"):
        return "DONE"
    return STATUS_FAILED


def _factory_evidence_record(evidence: Mapping[str, Any], engine: Any) -> dict[str, Any]:
    import hashlib
    import json

    envelopes = list(((evidence.get("factory_run") or {}).get("envelopes") or []))
    verification = list(((evidence.get("verification") or {}).get("envelopes") or []))
    governed = [*envelopes, *verification]
    session_id = None
    for env in governed:
        if isinstance(env, Mapping) and (env.get("provenance") or {}).get("session"):
            session_id = (env.get("provenance") or {}).get("session")
            break
    digest = hashlib.sha256(json.dumps(governed, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    work_id = evidence["work_id"]
    task_id = evidence["task_id"]
    verification_fingerprint = engine.verification_fingerprint
    raw_provenance = evidence.get("provenance") or {}
    provenance = dict(raw_provenance) if isinstance(raw_provenance, Mapping) else {}
    provenance["provider"] = evidence.get("provider") or provenance.get("provider") or "default"
    provenance["model"] = evidence.get("model") if "model" in evidence else provenance.get("model")
    return {
        "work_id": work_id,
        "task_id": task_id,
        "run_id": evidence.get("admit_task_id"),
        "admit_task_id": evidence.get("admit_task_id"),
        "provider": evidence.get("provider") or "default",
        "model": evidence.get("model"),
        "session_id": session_id,
        "worktree": evidence.get("worktree"),
        "cwd": evidence.get("cwd"),
        "owned_paths": list(evidence.get("owned_paths") or []),
        "content_fingerprint": engine.last_content_fingerprint,
        "verification_fingerprint": verification_fingerprint,
        "artifact_paths": list(engine._relevant_artifact_paths),
        "passed": True,
        "provenance": provenance,
        "factory_envelopes": governed,
        "envelope_digest": digest,
        "evidence_ref": f"factory-ledger:{task_id}:{work_id}:{verification_fingerprint}:{digest}",
    }


def _write_factory_evidence(record: Mapping[str, Any]) -> str | None:
    import hashlib
    import json
    import subprocess
    from pathlib import Path

    worktree = record.get("worktree") or {}
    path = worktree.get("path") if isinstance(worktree, Mapping) else None
    if not isinstance(path, str):
        return None
    probe = subprocess.run(
        ["git", "-C", path, "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        env=_git_subprocess_env(),
    )
    if probe.returncode != 0:
        return None
    ref = str(record["evidence_ref"])
    directory = Path(probe.stdout.strip()) / "studio-factory-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(ref.encode("utf-8")).hexdigest() + ".json"
    (directory / name).write_text(json.dumps(record, sort_keys=True, default=str), encoding="utf-8")
    return ref


def resolve_factory_evidence(refs: Mapping[str, Any] | None, worktrees: Mapping[str, Any] | None) -> dict[str, Any]:
    """Load previously emitted Factory evidence by reference."""
    import hashlib
    import json
    import subprocess
    from pathlib import Path

    if not isinstance(refs, Mapping) or not refs:
        return {}
    directories: list[Path] = []
    for identity in (worktrees or {}).values():
        path = getattr(identity, "path", None)
        if path is None and isinstance(identity, Mapping):
            path = identity.get("path")
        if not isinstance(path, str):
            continue
        probe = subprocess.run(
            ["git", "-C", path, "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            env=_git_subprocess_env(),
        )
        if probe.returncode != 0:
            continue
        directories.append(Path(probe.stdout.strip()) / "studio-factory-evidence")
    resolved: dict[str, Any] = {}
    for work_id, ref in refs.items():
        if not isinstance(work_id, str) or not isinstance(ref, str) or not ref.startswith("factory-ledger:"):
            continue
        name = hashlib.sha256(ref.encode("utf-8")).hexdigest() + ".json"
        for directory in directories:
            file = directory / name
            if not file.is_file():
                continue
            try:
                record = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("evidence_ref") == ref and record.get("work_id") == work_id:
                resolved[work_id] = record
                break
    return resolved



_PROTECTED_SCOPE_PREFIXES = (
    "_studio/",
    ".claude/",
    "docs/decisions/",
    "docs/release/",
    ".github/workflows/",
)


def _scope_path(path: str) -> str:
    """Drop a leading ./ only. lstrip("./") would also erase .claude and .github."""
    name = str(path).strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name[1:] if name.startswith("/") else name


def _changed_worktree_paths(cwd: str) -> list[str]:
    import subprocess
    probe = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        env=_git_subprocess_env(),
    )
    if probe.returncode != 0:
        return ["<git-status-failed>"]
    paths: list[str] = []
    for line in probe.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        paths.append(entry)
    return paths


def _path_is_protected(path: str) -> bool:
    name = _scope_path(path)
    if "/" not in name and name.startswith("studio_") and name.endswith(".py"):
        return True
    return any(name == prefix.rstrip("/") or name.startswith(prefix) for prefix in _PROTECTED_SCOPE_PREFIXES)


def _path_is_covered(path: str, roots: list[str], *, allow_blanket: bool) -> bool:
    name = _scope_path(path)
    for root in roots:
        owner = _scope_path(root)
        if not owner or owner in {".", "/"}:
            if allow_blanket:
                return True
            continue
        if name == owner or name.startswith(owner.rstrip("/") + "/"):
            return True
    return False


def workstream_scope_violations(cwd: str, owned_paths: list[str], artifact_paths: list[str]) -> list[str]:
    """Fail closed when a write-class worktree changes a path it does not own.

    Declared factory artifact paths are in scope. Workflow Kit runtime, config,
    and governance paths stay protected unless an owned path names them.
    The worktree is not rolled back.
    """
    violations: list[str] = []
    for path in _changed_worktree_paths(cwd):
        if path == "<git-status-failed>":
            violations.append(path)
            continue
        if _path_is_protected(path):
            if not _path_is_covered(path, owned_paths, allow_blanket=False):
                violations.append(path)
            continue
        if _path_is_covered(path, owned_paths, allow_blanket=False) or _path_is_covered(path, artifact_paths, allow_blanket=False):
            continue
        violations.append(path)
    return violations


def owned_paths_for_delegation(
    delegation: Mapping[str, Any],
    work_orders: Sequence[Mapping[str, Any]],
) -> list[str]:
    by_id = {entry["work_id"]: entry for entry in work_orders}
    paths: list[str] = []
    for work_id in delegation.get("work_ids") or ():
        entry = by_id.get(work_id)
        if entry is None:
            raise AutonomousExecutionError(
                f"delegation work_id {work_id!r} missing from frozen work order"
            )
        owned = entry.get("owned_paths")
        if not owned:
            raise AutonomousExecutionError(
                f"work order {work_id!r} missing owned_paths for coordinator dispatch"
            )
        paths.extend(list(owned))
    return paths


def build_profile_phase_specs(
    profile_id: str,
    *,
    work_order_id: str | None = None,
    code_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[PhaseSpec]:
    """Materialize profile phase dicts into PhaseSpec for the common Factory engine."""
    from studio_workflow_profiles import phase_dicts_for_factory

    overrides = dict(code_overrides or {})
    specs: list[PhaseSpec] = []
    for raw in phase_dicts_for_factory(profile_id, work_order_id=work_order_id):
        payload = {k: v for k, v in raw.items() if k in _PHASESPEC_FIELDS}
        extra = overrides.get(raw["phase_id"]) or {}
        payload.update({k: v for k, v in extra.items() if k in _PHASESPEC_FIELDS or k in {"operation", "command", "artifact_paths", "require_nonempty", "producer", "notes", "adapter_input", "routing_request"}})
        # Drop non-PhaseSpec keys that may have leaked
        payload = {k: v for k, v in payload.items() if k in _PHASESPEC_FIELDS}
        specs.append(PhaseSpec(**payload))
    return specs


def _require_bound_critic_evidence(
    evidence: Mapping[str, Any],
    *,
    task_id: str,
    frozen_work_order: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bare critic_verdict 'PASS'/'APPROVE' is not Critic evidence."""
    bound = evidence.get("critic_evidence")
    if not isinstance(bound, Mapping):
        raise AutonomousExecutionError(
            "DONE fails closed: bare critic_verdict is not Critic evidence; "
            "critic_evidence must bind verdict to task/work-order/node"
        )
    verdict = bound.get("verdict") or bound.get("critic_verdict")
    if verdict not in ACCEPTED_CRITIC_VERDICTS:
        raise AutonomousExecutionError(
            f"DONE fails closed: Critic verdict {verdict!r} not in "
            f"{sorted(ACCEPTED_CRITIC_VERDICTS)}"
        )
    bound_task = bound.get("task_id")
    if bound_task != task_id:
        raise AutonomousExecutionError(
            f"DONE fails closed: critic_evidence.task_id {bound_task!r} "
            f"does not match task {task_id!r}"
        )
    work_order_id = bound.get("work_order_id") or bound.get("work_id")
    node_id = bound.get("node_id")
    if not work_order_id and not node_id:
        raise AutonomousExecutionError(
            "DONE fails closed: critic_evidence must bind to work_order_id or node_id"
        )
    known = {entry.get("work_id") for entry in frozen_work_order}
    if work_order_id and work_order_id not in known and not node_id:
        raise AutonomousExecutionError(
            f"DONE fails closed: critic_evidence.work_order_id {work_order_id!r} "
            f"not in frozen work order"
        )
    return {
        "verdict": verdict,
        "task_id": task_id,
        "work_order_id": work_order_id,
        "node_id": node_id,
        "summary": bound.get("summary") or bound.get("rationale") or "",
    }


def _bind_operator_authorization(
    authorization: Mapping[str, Any],
    *,
    prepared: Mapping[str, Any],
    task_id: str,
) -> dict[str, Any]:
    if authorization.get("authorized") is not True and authorization.get("operator_authorized") is not True:
        raise AutonomousExecutionError(
            "DONE fails closed: operator authorization must set authorized=True"
        )
    prepared_action = dict(prepared.get("action") or {})
    auth_action = authorization.get("action")
    if not isinstance(auth_action, Mapping):
        auth_action = authorization.get("prepared_action")
    if not isinstance(auth_action, Mapping) or not auth_action:
        raise AutonomousExecutionError(
            "DONE fails closed: operator authorization missing prepared-action binding"
        )
    if dict(auth_action) != prepared_action:
        raise AutonomousExecutionError(
            "DONE fails closed: operator authorization is not bound to the prepared gated action"
        )
    auth_task = authorization.get("task_id")
    if auth_task != task_id:
        raise AutonomousExecutionError(
            f"DONE fails closed: operator authorization.task_id {auth_task!r} "
            f"does not match task {task_id!r}"
        )
    work_id = prepared_action.get("work_id")
    if work_id and auth_action.get("work_id") != work_id:
        raise AutonomousExecutionError(
            "DONE fails closed: operator authorization work_id does not match the prepared action"
        )
    for key in ("fingerprint", "content_fingerprint", "revision"):
        if key not in prepared_action:
            continue
        presented = authorization.get(key, auth_action.get(key))
        if presented != prepared_action[key]:
            raise AutonomousExecutionError(
                f"DONE fails closed: stale operator authorization {key}"
            )
    return {
        "authorized": True,
        "task_id": task_id,
        "gate_kind": prepared.get("gate_kind"),
        "action": dict(prepared_action),
        "work_id": work_id,
        "authorized_by": authorization.get("authorized_by") or authorization.get("operator") or "operator",
    }


def _question_covered_by_discoverable(question: str, fact_set: set[str]) -> bool:
    """A question is not asked when a discoverable fact already answers it."""
    q = question.casefold().strip()
    if q in fact_set:
        return True
    for fact in fact_set:
        if fact.startswith("covers:"):
            topic = fact[len("covers:"):].strip()
            if topic and topic in q:
                return True
        if q and (q in fact or fact in q):
            return True
    return False


def _validate_done_evidence(
    evidence: Mapping[str, Any],
    *,
    critic_required: bool,
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise AutonomousExecutionError("DONE evidence must be a mapping")

    required_bools = (
        "acceptance_criteria_satisfied",
        "tests_checks_passed",
        "no_hidden_unresolved_blocker",
    )
    missing = [k for k in required_bools if k not in evidence]
    if "regression_risk_evidence" not in evidence:
        missing.append("regression_risk_evidence")
    if "residual_uncertainty" not in evidence:
        missing.append("residual_uncertainty")
    if "artifact_runtime_verified" not in evidence:
        missing.append("artifact_runtime_verified")
    if missing:
        raise AutonomousExecutionError(
            f"DONE evidence incomplete; missing: {missing}"
        )

    for key in required_bools:
        if evidence[key] is not True:
            raise AutonomousExecutionError(
                f"DONE fails closed: {key} must be True, got {evidence[key]!r}"
            )

    art = evidence["artifact_runtime_verified"]
    if art is not True and art != "not_applicable":
        raise AutonomousExecutionError(
            "DONE fails closed: artifact_runtime_verified must be True or "
            f"'not_applicable', got {art!r}"
        )

    if not evidence["regression_risk_evidence"]:
        raise AutonomousExecutionError(
            "DONE fails closed: regression_risk_evidence must be present and non-empty"
        )

    residual = evidence["residual_uncertainty"]
    if not isinstance(residual, list):
        raise AutonomousExecutionError(
            "residual_uncertainty must be a list (may be empty)"
        )

    if critic_required:
        verdict = evidence.get("critic_verdict")
        if verdict not in ACCEPTED_CRITIC_VERDICTS:
            raise AutonomousExecutionError(
                f"DONE fails closed: Critic required; verdict {verdict!r} not in "
                f"{sorted(ACCEPTED_CRITIC_VERDICTS)}"
            )
        critic_not_required = False
    else:
        if evidence.get("critic_not_required") is not True and "critic_verdict" not in evidence:
            raise AutonomousExecutionError(
                "DONE fails closed: when Critic is not required, set "
                "critic_not_required=True (or supply an accepted critic_verdict)"
            )
        if "critic_verdict" in evidence:
            verdict = evidence["critic_verdict"]
            if verdict not in ACCEPTED_CRITIC_VERDICTS:
                raise AutonomousExecutionError(
                    f"DONE fails closed: critic_verdict {verdict!r} not accepted"
                )
        critic_not_required = evidence.get("critic_not_required") is True

    return {
        "acceptance_criteria_satisfied": True,
        "tests_checks_passed": True,
        "artifact_runtime_verified": art,
        "regression_risk_evidence": evidence["regression_risk_evidence"],
        "no_hidden_unresolved_blocker": True,
        "residual_uncertainty": list(residual),
        "critic_required": critic_required,
        "critic_not_required": critic_not_required,
        "critic_verdict": evidence.get("critic_verdict"),
    }


def start_task(
    task_id: str,
    signals: Mapping[str, Any],
    *,
    discoverable_facts: Sequence[str] | None = None,
    proposed_decomposition: Any = None,
    availability: Mapping[str, Any] | None = None,
    profile_id: str | None = None,
    observability_ledger: RuntimeObservabilityLedger | None = None,
    admission_capacity: int | None = None,
    provider_health: Mapping[str, Any] | None = None,
    admission_completed: Sequence[str] | None = None,
    already_admitted: Mapping[str, Any] | None = None,
    provider_executors: Mapping[str, Any] | None = None,
    durable_completions: Mapping[str, Any] | None = None,
    authoritative_completions: Mapping[str, Any] | None = None,
) -> AutonomousExecutionTask:
    """Convenience: create task and run intake."""
    task = AutonomousExecutionTask(task_id=task_id)
    return task.intake(
        signals,
        discoverable_facts=discoverable_facts,
        proposed_decomposition=proposed_decomposition,
        availability=availability,
        profile_id=profile_id,
        observability_ledger=observability_ledger,
        admission_capacity=admission_capacity,
        provider_health=provider_health,
        admission_completed=admission_completed,
        already_admitted=already_admitted,
        provider_executors=provider_executors,
        authoritative_completions=authoritative_completions,
    )
