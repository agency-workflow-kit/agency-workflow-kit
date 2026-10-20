#!/usr/bin/env python3
"""In-memory runtime observability ledger.

Telemetry is runtime evidence only — not durable Notion product truth or
GitHub project/engineering truth, not a task database, and never authoritative for orchestration, routing, retry,
governance, or DONE. Missing provider metrics stay null; this module never
invents tokens, cost, or model identity.

Event vocabulary aligns with the Factory Run Engine ledger
(``phase_start`` / ``phase_end`` / ``gate_pass`` / ``gate_fail`` / ``repair``).
Worktree identity matches Parallel Coordinator ownership records
(path / branch / head / index_path). Evidence is referenced by path or id
only — never stored as a second evidence store.

Front Door attaches the default ledger. This module does not import
Front Door, Factory, or the Parallel Coordinator. The ledger is never
authoritative and never source_of_truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


class RuntimeObservabilityError(RuntimeError):
    """Fail-closed observability contract violation."""


# Factory ledger event vocabulary (must stay byte-compatible for join).
EVENT_PHASE_START = "phase_start"
EVENT_PHASE_END = "phase_end"
EVENT_GATE_PASS = "gate_pass"
EVENT_GATE_FAIL = "gate_fail"
EVENT_REPAIR = "repair"

LEDGER_EVENTS = frozenset({
    EVENT_PHASE_START,
    EVENT_PHASE_END,
    EVENT_GATE_PASS,
    EVENT_GATE_FAIL,
    EVENT_REPAIR,
})

# Producer kinds — same literals as studio_factory_run.
PRODUCER_KIND_CODE = "CODE"
PRODUCER_KIND_AGENT = "AGENT"
PRODUCER_KIND_TOOL = "TOOL"
PRODUCER_KINDS = frozenset({
    PRODUCER_KIND_CODE,
    PRODUCER_KIND_AGENT,
    PRODUCER_KIND_TOOL,
})

GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_RESULTS = frozenset({GATE_PASS, GATE_FAIL})

# Operator interruption classes — exact literals from autonomous execution /
# parallel coordinator. Observability records them; it does not invent new
# approval categories.
MANDATORY_OPERATOR_GATE = "MANDATORY_OPERATOR_GATE"
UNRESOLVABLE_BLOCKER = "UNRESOLVABLE_BLOCKER"
NEW_MATERIAL_PRODUCT_DECISION = "NEW_MATERIAL_PRODUCT_DECISION"

INTERRUPTION_CLASSES = frozenset({
    MANDATORY_OPERATOR_GATE,
    UNRESOLVABLE_BLOCKER,
    NEW_MATERIAL_PRODUCT_DECISION,
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeObservabilityError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeObservabilityError(f"{field_name} must be a string or null")
    text = value.strip()
    return text or None


def _optional_nonneg_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeObservabilityError(f"{field_name} must be an int or null")
    if value < 0:
        raise RuntimeObservabilityError(f"{field_name} must be >= 0")
    return value


def _optional_nonneg_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeObservabilityError(f"{field_name} must be a number or null")
    number = float(value)
    if number < 0:
        raise RuntimeObservabilityError(f"{field_name} must be >= 0")
    return number


def _duration_ms(
    *,
    started_at: str | None,
    ended_at: str | None,
    duration_ms: int | None,
) -> int | None:
    """Prefer an explicit duration; otherwise derive from timestamps.

    Never invent a duration when neither source is available.
    """
    if duration_ms is not None:
        return duration_ms
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError as exc:
        raise RuntimeObservabilityError(
            f"cannot derive duration_ms from timestamps: {exc}"
        ) from exc
    delta_ms = int((end - start).total_seconds() * 1000)
    if delta_ms < 0:
        raise RuntimeObservabilityError("ended_at must be >= started_at")
    return delta_ms


@dataclass(frozen=True)
class WorktreeIdentity:
    """Isolated worktree ownership record — identity only, no SCM mutation.

    Field names match ParallelCoordinator.WorktreeIdentity so a join can
    pass records through without reshaping.
    """

    path: str
    branch: str
    head: str
    index_path: str

    def __post_init__(self) -> None:
        for field_name in ("path", "branch", "head", "index_path"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeObservabilityError(
                    f"worktree.{field_name} must be a non-empty string"
                )

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "branch": self.branch,
            "head": self.head,
            "index_path": self.index_path,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorktreeIdentity":
        try:
            return cls(
                path=str(value["path"]),
                branch=str(value["branch"]),
                head=str(value["head"]),
                index_path=str(value["index_path"]),
            )
        except KeyError as exc:
            raise RuntimeObservabilityError(
                "worktree identity requires path/branch/head/index_path; "
                f"missing {exc}"
            ) from exc


@dataclass
class InterruptionTelemetry:
    """One Operator interruption observed on a task/phase.

    ``required=False`` marks an avoidable interruption for aggregation.
    The three named classes are always required when recorded via the
    typed API; callers may still attach raw avoidable records for
    accounting (they should not be emitted by production APIs).
    """

    task_id: str
    interruption_class: str
    reason: str
    required: bool
    phase: str | None = None
    required_decision_or_action: str | None = None
    open: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "class": self.interruption_class,
            "phase": self.phase,
            "reason": self.reason,
            "required_decision_or_action": self.required_decision_or_action,
            "required": self.required,
            "open": self.open,
        }


@dataclass
class TelemetryRecord:
    """One append-only telemetry sample for a task/phase event.

    Provider metrics (``tokens``, ``cost``, ``runtime``, ``provider``,
    ``model``, ``effort``) stay null when the producer did not report them.
    """

    task_id: str
    event: str
    phase_id: str | None = None
    state: str | None = None
    producer_kind: str | None = None
    runtime: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    tokens: int | None = None
    cost: float | None = None
    retries: int = 0
    gate_result: str | None = None
    interruptions: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)
    evidence_references: list[str] = field(default_factory=list)
    worktree: WorktreeIdentity | None = None
    completion_reason: str | None = None
    block_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase_id": self.phase_id,
            "event": self.event,
            "state": self.state,
            "producer_kind": self.producer_kind,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "cost": self.cost,
            "retries": self.retries,
            "gate_result": self.gate_result,
            "interruptions": [dict(item) for item in self.interruptions],
            "artifacts": list(self.artifacts),
            "evidence_references": list(self.evidence_references),
            "worktree": self.worktree.as_dict() if self.worktree else None,
            "completion_reason": self.completion_reason,
            "block_reason": self.block_reason,
        }

    def as_factory_ledger_entry(self) -> dict[str, Any]:
        """Project onto the thin Factory Run Engine ledger shape."""
        return {
            "phase_id": self.phase_id,
            "event": self.event,
            "producer_kind": self.producer_kind,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "duration_ms": self.duration_ms,
            "gate_result": self.gate_result,
            "retry_count": self.retries,
            "artifacts": [
                a if isinstance(a, str) else (a.get("path") or a.get("kind") or "")
                for a in self.artifacts
                if isinstance(a, (str, dict))
            ],
            "interruptions": [dict(item) for item in self.interruptions],
        }


def aggregate_operator_interruptions(
    interruptions: Sequence[Mapping[str, Any] | InterruptionTelemetry],
) -> dict[str, Any]:
    """Aggregate operator_interruptions so required + avoidable == total.

    Shape:
      ``{"total": int, "required": int, "avoidable": int, "interruptions": [...]}``
    """
    records: list[dict[str, Any]] = []
    required_count = 0
    avoidable_count = 0
    for item in interruptions:
        if isinstance(item, InterruptionTelemetry):
            payload = item.as_dict()
        else:
            payload = dict(item)
            if "class" not in payload and "interruption_class" in payload:
                payload["class"] = payload["interruption_class"]
            if "required" not in payload:
                cls = payload.get("class")
                payload["required"] = cls in INTERRUPTION_CLASSES
        if payload.get("required"):
            required_count += 1
        else:
            avoidable_count += 1
        records.append(payload)
    total = required_count + avoidable_count
    return {
        "total": total,
        "required": required_count,
        "avoidable": avoidable_count,
        "interruptions": records,
    }


class RuntimeObservabilityLedger:
    """Append-only in-memory run ledger for one or more task scopes.

    Not durable persistence and not a planner/task store. Callers own
    evidence artifacts elsewhere and attach references only.
    """

    def __init__(self, *, run_id: str | None = None) -> None:
        self.run_id = run_id
        self._records: list[TelemetryRecord] = []
        self._interruptions: list[InterruptionTelemetry] = []
        self._worktrees: dict[str, WorktreeIdentity] = {}
        self._states: dict[str, str] = {}

    def record(
        self,
        *,
        task_id: str,
        event: str,
        phase_id: str | None = None,
        state: str | None = None,
        producer_kind: str | None = None,
        runtime: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        duration_ms: int | None = None,
        tokens: int | None = None,
        cost: float | None = None,
        retries: int = 0,
        gate_result: str | None = None,
        interruptions: Sequence[Mapping[str, Any]] | None = None,
        artifacts: Sequence[Any] | None = None,
        evidence_references: Sequence[str] | None = None,
        worktree: WorktreeIdentity | Mapping[str, Any] | None = None,
        completion_reason: str | None = None,
        block_reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one telemetry sample. Missing provider metrics stay null."""
        tid = _require_nonempty_str(task_id, "task_id")
        evt = _require_nonempty_str(event, "event")
        if evt not in LEDGER_EVENTS:
            raise RuntimeObservabilityError(
                f"unknown event {evt!r}; allowed: {sorted(LEDGER_EVENTS)}"
            )

        kind = _optional_str(producer_kind, "producer_kind")
        if kind is not None and kind not in PRODUCER_KINDS:
            raise RuntimeObservabilityError(
                f"unknown producer_kind {kind!r}; allowed: {sorted(PRODUCER_KINDS)}"
            )

        gate = _optional_str(gate_result, "gate_result")
        if gate is not None and gate not in GATE_RESULTS:
            raise RuntimeObservabilityError(
                f"unknown gate_result {gate!r}; allowed: {sorted(GATE_RESULTS)}"
            )

        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise RuntimeObservabilityError("retries must be an int >= 0")

        started = _optional_str(started_at, "started_at")
        ended = _optional_str(ended_at, "ended_at")

        resolved_state = _optional_str(state, "state")
        if resolved_state is None:
            resolved_state = self._states.get(tid)

        identity = self._coerce_worktree(worktree) if worktree is not None else (
            self._worktrees.get(tid)
        )

        sample = TelemetryRecord(
            task_id=tid,
            event=evt,
            phase_id=_optional_str(phase_id, "phase_id"),
            state=resolved_state,
            producer_kind=kind,
            runtime=_optional_str(runtime, "runtime"),
            provider=_optional_str(provider, "provider"),
            model=_optional_str(model, "model"),
            effort=_optional_str(effort, "effort"),
            started_at=started,
            ended_at=ended,
            duration_ms=_duration_ms(
                started_at=started,
                ended_at=ended,
                duration_ms=_optional_nonneg_int(duration_ms, "duration_ms"),
            ),
            tokens=_optional_nonneg_int(tokens, "tokens"),
            cost=_optional_nonneg_float(cost, "cost"),
            retries=retries,
            gate_result=gate,
            interruptions=[dict(item) for item in (interruptions or ())],
            artifacts=list(artifacts or ()),
            evidence_references=[
                _require_nonempty_str(ref, "evidence_references entry")
                for ref in (evidence_references or ())
            ],
            worktree=identity,
            completion_reason=_optional_str(completion_reason, "completion_reason"),
            block_reason=_optional_str(block_reason, "block_reason"),
        )
        self._records.append(sample)
        return sample.as_dict()

    def phase_start(
        self,
        *,
        task_id: str,
        phase_id: str,
        producer_kind: str,
        state: str | None = None,
        runtime: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        started_at: str | None = None,
        evidence_references: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self.record(
            task_id=task_id,
            event=EVENT_PHASE_START,
            phase_id=phase_id,
            producer_kind=producer_kind,
            state=state,
            runtime=runtime,
            provider=provider,
            model=model,
            effort=effort,
            started_at=started_at or _utc_now(),
            evidence_references=evidence_references,
        )

    def phase_end(
        self,
        *,
        task_id: str,
        phase_id: str,
        producer_kind: str,
        state: str | None = None,
        runtime: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        duration_ms: int | None = None,
        tokens: int | None = None,
        cost: float | None = None,
        retries: int = 0,
        gate_result: str | None = None,
        artifacts: Sequence[Any] | None = None,
        evidence_references: Sequence[str] | None = None,
        interruptions: Sequence[Mapping[str, Any]] | None = None,
        completion_reason: str | None = None,
        block_reason: str | None = None,
    ) -> dict[str, Any]:
        return self.record(
            task_id=task_id,
            event=EVENT_PHASE_END,
            phase_id=phase_id,
            producer_kind=producer_kind,
            state=state,
            runtime=runtime,
            provider=provider,
            model=model,
            effort=effort,
            started_at=started_at,
            ended_at=ended_at or _utc_now(),
            duration_ms=duration_ms,
            tokens=tokens,
            cost=cost,
            retries=retries,
            gate_result=gate_result,
            artifacts=artifacts,
            evidence_references=evidence_references,
            interruptions=interruptions,
            completion_reason=completion_reason,
            block_reason=block_reason,
        )

    def gate(
        self,
        *,
        task_id: str,
        phase_id: str,
        producer_kind: str,
        passed: bool,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        runtime: str | None = None,
        duration_ms: int | None = None,
        retries: int = 0,
        artifacts: Sequence[Any] | None = None,
        evidence_references: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self.record(
            task_id=task_id,
            event=EVENT_GATE_PASS if passed else EVENT_GATE_FAIL,
            phase_id=phase_id,
            producer_kind=producer_kind,
            provider=provider,
            model=model,
            effort=effort,
            runtime=runtime,
            duration_ms=duration_ms,
            retries=retries,
            gate_result=GATE_PASS if passed else GATE_FAIL,
            artifacts=artifacts,
            evidence_references=evidence_references,
            ended_at=_utc_now(),
        )

    def repair(
        self,
        *,
        task_id: str,
        phase_id: str,
        producer_kind: str,
        retries: int,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        runtime: str | None = None,
        artifacts: Sequence[Any] | None = None,
        evidence_references: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self.record(
            task_id=task_id,
            event=EVENT_REPAIR,
            phase_id=phase_id,
            producer_kind=producer_kind,
            provider=provider,
            model=model,
            effort=effort,
            runtime=runtime,
            retries=retries,
            artifacts=artifacts,
            evidence_references=evidence_references,
        )

    def record_interruption(
        self,
        *,
        task_id: str,
        interruption_class: str,
        reason: str,
        required_decision_or_action: str,
        phase: str | None = None,
        phase_id: str | None = None,
        required: bool = True,
        avoidable: bool = False,
    ) -> dict[str, Any]:
        """Record an Operator interruption for aggregation.

        Named interruption classes must be one of the three existing
        classes and are required by default. Set ``avoidable=True`` (or
        ``required=False``) only to account for an avoidable ask that
        production APIs should never emit.
        """
        tid = _require_nonempty_str(task_id, "task_id")
        cls = _require_nonempty_str(interruption_class, "interruption_class")
        why = _require_nonempty_str(reason, "reason")
        action = _require_nonempty_str(
            required_decision_or_action, "required_decision_or_action"
        )
        if avoidable:
            is_required = False
        else:
            is_required = bool(required)
        if is_required and cls not in INTERRUPTION_CLASSES:
            raise RuntimeObservabilityError(
                f"required interruption_class must be one of "
                f"{sorted(INTERRUPTION_CLASSES)}, got {cls!r}"
            )
        item = InterruptionTelemetry(
            task_id=tid,
            interruption_class=cls,
            reason=why,
            required=is_required,
            phase=_optional_str(phase or phase_id, "phase"),
            required_decision_or_action=action,
            open=True,
        )
        self._interruptions.append(item)
        return item.as_dict()

    def operator_interruptions(
        self,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Return ``{total, required, avoidable, interruptions}``.

        ``required + avoidable`` always equals ``total``.
        """
        items: list[InterruptionTelemetry] = list(self._interruptions)
        if task_id is not None:
            tid = _require_nonempty_str(task_id, "task_id")
            items = [item for item in items if item.task_id == tid]
        return aggregate_operator_interruptions(items)

    def bind_worktree(
        self,
        task_id: str,
        worktree: WorktreeIdentity | Mapping[str, Any],
    ) -> dict[str, str]:
        tid = _require_nonempty_str(task_id, "task_id")
        identity = self._coerce_worktree(worktree)
        self._worktrees[tid] = identity
        return identity.as_dict()

    def set_state(self, task_id: str, state: str) -> str:
        tid = _require_nonempty_str(task_id, "task_id")
        value = _require_nonempty_str(state, "state")
        self._states[tid] = value
        return value

    def records(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            return [r.as_dict() for r in self._records]
        tid = _require_nonempty_str(task_id, "task_id")
        return [r.as_dict() for r in self._records if r.task_id == tid]

    def factory_ledger(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        """Project samples onto Factory Run Engine ledger entry dicts."""
        if task_id is None:
            return [r.as_factory_ledger_entry() for r in self._records]
        tid = _require_nonempty_str(task_id, "task_id")
        return [
            r.as_factory_ledger_entry()
            for r in self._records
            if r.task_id == tid
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "records": self.records(),
            "operator_interruptions": self.operator_interruptions(),
            "worktrees": {
                tid: identity.as_dict() for tid, identity in self._worktrees.items()
            },
            "states": dict(self._states),
            # Honesty marker: this ledger is evidence, not source of truth.
            "authoritative": False,
            "source_of_truth": False,
        }

    @staticmethod
    def _coerce_worktree(
        worktree: WorktreeIdentity | Mapping[str, Any],
    ) -> WorktreeIdentity:
        if isinstance(worktree, WorktreeIdentity):
            return worktree
        return WorktreeIdentity.from_mapping(worktree)


def ingest_factory_ledger_entry(
    ledger: RuntimeObservabilityLedger,
    *,
    task_id: str,
    entry: Mapping[str, Any],
    state: str | None = None,
    runtime: str | None = None,
    tokens: int | None = None,
    cost: float | None = None,
    evidence_references: Sequence[str] | None = None,
    worktree: WorktreeIdentity | Mapping[str, Any] | None = None,
    completion_reason: str | None = None,
    block_reason: str | None = None,
) -> dict[str, Any]:
    """Bridge helper: lift a Factory LedgerEntry-shaped dict into this ledger.

    Does not import studio_factory_run. Factory calls this helper; this
    module does not call Factory. The ledger remains non-authoritative.
    """
    return ledger.record(
        task_id=task_id,
        event=str(entry.get("event") or ""),
        phase_id=entry.get("phase_id"),
        state=state,
        producer_kind=entry.get("producer_kind"),
        runtime=runtime,
        provider=entry.get("provider"),
        model=entry.get("model"),
        effort=entry.get("effort"),
        duration_ms=entry.get("duration_ms"),
        tokens=tokens,
        cost=cost,
        retries=int(entry.get("retry_count") or 0),
        gate_result=entry.get("gate_result"),
        interruptions=list(entry.get("interruptions") or ()),
        artifacts=list(entry.get("artifacts") or ()),
        evidence_references=evidence_references,
        worktree=worktree,
        completion_reason=completion_reason,
        block_reason=block_reason,
    )


# ---------------------------------------------------------------------------
# Default in-memory ledger attach. Front Door calls this; it is not authority.
# ---------------------------------------------------------------------------

_DEFAULT_LEDGERS: dict[str, RuntimeObservabilityLedger] = {}


def _default_ledger_key(task_id: str, run_id: str | None) -> str:
    tid = _require_nonempty_str(task_id, "task_id")
    if run_id is None:
        return tid
    rid = _require_nonempty_str(run_id, "run_id")
    return f"{rid}::{tid}"


def attach_default_ledger(
    task_id: str,
    *,
    run_id: str | None = None,
) -> RuntimeObservabilityLedger:
    """Attach the default in-memory observability ledger for a task/run.

    Same ``task_id`` (and optional ``run_id``) returns the same surface for the
    process lifetime. Front Door calls this. This module does not import
    Front Door. The ledger is never authoritative and never source_of_truth.
    """
    key = _default_ledger_key(task_id, run_id)
    existing = _DEFAULT_LEDGERS.get(key)
    if existing is not None:
        return existing
    ledger = RuntimeObservabilityLedger(run_id=run_id)
    _DEFAULT_LEDGERS[key] = ledger
    return ledger


def get_attached_ledger(
    task_id: str,
    *,
    run_id: str | None = None,
) -> RuntimeObservabilityLedger | None:
    """Return a previously attached default ledger, or None."""
    key = _default_ledger_key(task_id, run_id)
    return _DEFAULT_LEDGERS.get(key)


def reset_default_ledgers() -> None:
    """Clear the process-local default ledger registry (tests only)."""
    _DEFAULT_LEDGERS.clear()
