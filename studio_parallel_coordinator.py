#!/usr/bin/env python3
"""Provider-neutral Parallel Task Coordinator core.

Coordinates N independent tasks with explicit owned_paths claims and
isolated worktree identity records. This is NOT a second planner, task
database, Master Plan, UI, or evidence store. The Factory Run Engine
remains single-task execution machinery; callers may invoke it per task
as a library. This module never dispatches specialists and never mutates
a checkout.

Admission is capacity-aware and dependency-aware. A usage limit, gate,
or overlap on one work item does not reject the rest of the batch.

Responsibilities:
- task identity (`task_id`, distinct from optional `work_id`)
- owned_paths claims with overlap detection
- isolated worktree ownership records (path, branch, HEAD, index path)
- overlap refusal OR deterministic serialization (caller-selected policy)
- per-task status and recovery that does not cascade to unrelated tasks
- join metadata: JOINED / NOT_READY / BLOCKED (JoinGate vocabulary)
- batched Operator interruptions using ONLY the three existing classes
- per-task evidence *references* (paths/ids), never a second evidence store

Operator interruption classes mirror studio_autonomous_execution literals
exactly — no new Operator approval category is invented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Mapping, Sequence

from studio_runtime_observability import RuntimeObservabilityLedger


class ParallelCoordinatorError(RuntimeError):
    """Fail-closed coordinator contract violation."""


# Task status vocabulary (coordinator-local; terminal set mirrors JoinGate).
STATUS_PENDING = "PENDING"
STATUS_CLAIMED = "CLAIMED"
STATUS_RUNNING = "RUNNING"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"
STATUS_RECOVERING = "RECOVERING"

ACTIVE_CLAIM_STATUSES = frozenset({
    STATUS_CLAIMED,
    STATUS_RUNNING,
    STATUS_RECOVERING,
})
TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED})
ALL_STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_CLAIMED,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RECOVERING,
})

# Overlap policy — refuse concurrent claims, or serialize overlapping ones.
OVERLAP_REFUSE = "refuse"
OVERLAP_SERIALIZE = "serialize"
OVERLAP_POLICIES = frozenset({OVERLAP_REFUSE, OVERLAP_SERIALIZE})

# Join verdicts — same vocabulary as studio_join_gate.JoinGate.
JOIN_JOINED = "JOINED"
JOIN_NOT_READY = "NOT_READY"
JOIN_BLOCKED = "BLOCKED"

# Operator interruption classes — exact literals from autonomous execution.
MANDATORY_OPERATOR_GATE = "MANDATORY_OPERATOR_GATE"
UNRESOLVABLE_BLOCKER = "UNRESOLVABLE_BLOCKER"
NEW_MATERIAL_PRODUCT_DECISION = "NEW_MATERIAL_PRODUCT_DECISION"

INTERRUPTION_CLASSES = frozenset({
    MANDATORY_OPERATOR_GATE,
    UNRESOLVABLE_BLOCKER,
    NEW_MATERIAL_PRODUCT_DECISION,
})

# Existing Operator gate kinds only — do not invent a new approval category.
EXISTING_OPERATOR_GATE_KINDS = frozenset({
    "operator_gate_required",
    "OPERATOR_APPROVAL_REQUIRED",
})


def normalize_owned_path(path: str) -> str:
    """Normalize a claim path for deterministic overlap comparison.

    Rejects absolute paths, `..` traversal, and alias forms such as
    `/.git/./index` that would otherwise collapse into a dangerous claim.
    """
    if not isinstance(path, str) or not path.strip():
        raise ParallelCoordinatorError(f"owned_paths entry must be a non-empty string, got {path!r}")
    text = path.strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.rstrip("/")
    if text in {"", "."}:
        raise ParallelCoordinatorError(f"owned_paths entry must not be empty or '.', got {path!r}")
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        raise ParallelCoordinatorError(f"owned_paths must be relative, got absolute {path!r}")
    if any(part == ".." for part in text.split("/")):
        raise ParallelCoordinatorError(f"owned_paths must not contain '..': {path!r}")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts:
        raise ParallelCoordinatorError(f"owned_paths entry must not be empty or '.', got {path!r}")
    return "/".join(parts)


def normalize_worktree_path(path: str, *, field_name: str = "path") -> str:
    """Normalize a worktree identity path/index_path.

    Collapses `.` segments and rejects `..`. Rejects the alias absolute
    form that normalizes to `/.git/index`. Absolute paths that are
    otherwise well-formed remain allowed (worktree identity records are
    filesystem locations, not owned_paths claims).
    """
    if not isinstance(path, str) or not path.strip():
        raise ParallelCoordinatorError(f"worktree.{field_name} must be a non-empty string")
    text = path.strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.rstrip("/") or "/"
    parts = text.split("/")
    if any(part == ".." for part in parts):
        raise ParallelCoordinatorError(f"worktree.{field_name} must not contain '..': {path!r}")
    rebuilt: list[str] = []
    absolute = text.startswith("/")
    for part in parts:
        if part in {"", "."}:
            continue
        rebuilt.append(part)
    if absolute:
        normalized = "/" + "/".join(rebuilt) if rebuilt else "/"
    else:
        if not rebuilt:
            raise ParallelCoordinatorError(
                f"worktree.{field_name} must not be empty or '.', got {path!r}"
            )
        normalized = "/".join(rebuilt)
    if normalized == "/.git/index":
        raise ParallelCoordinatorError(
            f"worktree.{field_name} rejects alias path {path!r}"
        )
    return normalized


def paths_overlap(a: str, b: str) -> bool:
    """True when two normalized paths collide (equal or prefix as a path)."""
    left = normalize_owned_path(a)
    right = normalize_owned_path(b)
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


def overlapping_paths(paths_a: Sequence[str], paths_b: Sequence[str]) -> tuple[str, ...]:
    """Return sorted unique overlapping path pairs as 'a<>b' tokens."""
    hits: list[str] = []
    for left in paths_a:
        for right in paths_b:
            if paths_overlap(left, right):
                token = "<>".join(sorted((normalize_owned_path(left), normalize_owned_path(right))))
                if token not in hits:
                    hits.append(token)
    return tuple(sorted(hits))


@dataclass(frozen=True)
class WorktreeIdentity:
    """Isolated worktree ownership record — identity only, no SCM mutation."""

    path: str
    branch: str
    head: str
    index_path: str

    def __post_init__(self) -> None:
        for field_name in ("branch", "head"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ParallelCoordinatorError(
                    f"worktree.{field_name} must be a non-empty string"
                )
        object.__setattr__(self, "path", normalize_worktree_path(self.path, field_name="path"))
        object.__setattr__(
            self, "index_path", normalize_worktree_path(self.index_path, field_name="index_path")
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "branch": self.branch,
            "head": self.head,
            "index_path": self.index_path,
        }


@dataclass
class TaskRecord:
    task_id: str
    owned_paths: tuple[str, ...]
    work_id: str | None = None
    worktree: WorktreeIdentity | None = None
    status: str = STATUS_PENDING
    evidence_ref: str | None = None
    recovery: dict[str, Any] | None = None
    interruptions: list[dict[str, Any]] = field(default_factory=list)
    serialization_group: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "work_id": self.work_id,
            "owned_paths": list(self.owned_paths),
            "worktree": self.worktree.as_dict() if self.worktree else None,
            "status": self.status,
            "evidence_ref": self.evidence_ref,
            "recovery": None if self.recovery is None else dict(self.recovery),
            "interruptions": [dict(item) for item in self.interruptions],
            "serialization_group": self.serialization_group,
        }


class ParallelCoordinator:
    """In-memory coordinator for N independent isolated tasks.

    Not durable persistence and not a planner. Callers own evidence
    artifacts elsewhere and attach references only.
    """

    def __init__(
        self,
        *,
        overlap_policy: str = OVERLAP_REFUSE,
        observability_ledger: RuntimeObservabilityLedger | None = None,
    ) -> None:
        if overlap_policy not in OVERLAP_POLICIES:
            raise ParallelCoordinatorError(
                f"overlap_policy must be one of {sorted(OVERLAP_POLICIES)}, got {overlap_policy!r}"
            )
        self.overlap_policy = overlap_policy
        self._observability = observability_ledger
        self._tasks: dict[str, TaskRecord] = {}
        self._claim_order: list[str] = []
        self._lock = threading.RLock()

    def _emit_observability(
        self,
        *,
        task_id: str,
        event: str,
        phase_id: str,
        evidence_references: Sequence[str] | None = None,
        worktree: WorktreeIdentity | Mapping[str, str] | None = None,
        producer_kind: str = "CODE",
    ) -> None:
        """Emit ledger-shaped telemetry with evidence refs only.

        Does not store a second evidence database — callers own evidence
        elsewhere and attach path/id references.
        """
        if self._observability is None:
            return
        self._observability.record(
            task_id=task_id,
            event=event,
            phase_id=phase_id,
            producer_kind=producer_kind,
            evidence_references=list(evidence_references or ()),
            worktree=worktree,
        )

    # ------------------------------------------------------------------ claims

    def claim_task(
        self,
        *,
        task_id: str,
        owned_paths: Sequence[str],
        work_id: str | None = None,
        worktree: WorktreeIdentity | Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Register a task claim. Enforces overlap policy against active claims."""
        with self._lock:
            return self._claim_task_locked(
                task_id=task_id,
                owned_paths=owned_paths,
                work_id=work_id,
                worktree=worktree,
            )

    def _claim_task_locked(
        self,
        *,
        task_id: str,
        owned_paths: Sequence[str],
        work_id: str | None = None,
        worktree: WorktreeIdentity | Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        task_id = self._require_task_id(task_id)
        if task_id in self._tasks:
            raise ParallelCoordinatorError(f"task_id already claimed: {task_id}")
        if work_id is not None and work_id == task_id:
            raise ParallelCoordinatorError(
                "task_id must be distinct from work_id when both are supplied"
            )
        normalized = tuple(normalize_owned_path(p) for p in owned_paths)
        if not normalized:
            raise ParallelCoordinatorError("owned_paths claim must include at least one path")
        if len(set(normalized)) != len(normalized):
            raise ParallelCoordinatorError(f"duplicate owned_paths in claim for {task_id}")

        conflicts = self._active_overlaps(task_id=task_id, owned_paths=normalized)
        serialization_group: str | None = None
        if conflicts:
            if self.overlap_policy == OVERLAP_REFUSE:
                raise ParallelCoordinatorError(
                    f"owned_paths overlap refused for {task_id}: {conflicts}"
                )
            # Deterministic serialization: join the earliest conflicting group's id,
            # or the earliest conflicting task_id when none yet grouped.
            serialization_group = self._serialization_group_for(conflicts)

        identity = self._coerce_worktree(worktree) if worktree is not None else None
        record = TaskRecord(
            task_id=task_id,
            owned_paths=normalized,
            work_id=work_id,
            worktree=identity,
            status=STATUS_CLAIMED,
            serialization_group=serialization_group,
        )
        self._tasks[task_id] = record
        self._claim_order.append(task_id)
        refs = [f"parallel-claim:{task_id}"]
        if work_id:
            refs.append(f"work-order:{work_id}")
        for path in normalized:
            refs.append(f"owned-path:{path}")
        self._emit_observability(
            task_id=task_id,
            event="phase_start",
            phase_id="parallel_claim",
            evidence_references=refs,
            worktree=identity,
        )
        return {
            "claimed": True,
            "task": record.as_dict(),
            "overlaps": conflicts,
            "overlap_policy": self.overlap_policy,
            "serialized": serialization_group is not None,
        }

    def bind_worktree(
        self,
        task_id: str,
        worktree: WorktreeIdentity | Mapping[str, str],
        *,
        forbidden_index_paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Attach isolated worktree identity/ownership to an existing claim."""
        with self._lock:
            return self._bind_worktree_locked(
                task_id, worktree, forbidden_index_paths=forbidden_index_paths
            )

    def _bind_worktree_locked(
        self,
        task_id: str,
        worktree: WorktreeIdentity | Mapping[str, str],
        *,
        forbidden_index_paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        record = self._get(task_id)
        identity = self._coerce_worktree(worktree)
        forbidden = {
            normalize_owned_path(p) if "/" in p or p.endswith("index") else p
            for p in (forbidden_index_paths or ())
        }
        # Compare raw and normalized forms so callers can pass absolute index paths.
        for candidate in forbidden_index_paths or ():
            if identity.index_path == candidate or identity.index_path.rstrip("/") == candidate.rstrip("/"):
                raise ParallelCoordinatorError(
                    f"worktree index_path collides with forbidden index: {identity.index_path}"
                )
            forbidden.add(candidate)
        if identity.index_path in forbidden:
            raise ParallelCoordinatorError(
                f"worktree index_path collides with forbidden index: {identity.index_path}"
            )
        # One worktree ownership record per path across active claims.
        for other in self._tasks.values():
            if other.task_id == record.task_id or other.worktree is None:
                continue
            if other.status not in ACTIVE_CLAIM_STATUSES | {STATUS_DONE, STATUS_FAILED}:
                continue
            if other.worktree.path == identity.path:
                raise ParallelCoordinatorError(
                    f"worktree path already owned by {other.task_id}: {identity.path}"
                )
            if other.worktree.index_path == identity.index_path:
                raise ParallelCoordinatorError(
                    f"worktree index already owned by {other.task_id}: {identity.index_path}"
                )
        record.worktree = identity
        self._emit_observability(
            task_id=task_id,
            event="phase_start",
            phase_id="parallel_worktree",
            evidence_references=[
                f"worktree-path:{identity.path}",
                f"worktree-index:{identity.index_path}",
            ],
            worktree=identity,
        )
        return {"task_id": task_id, "worktree": identity.as_dict()}

    # ------------------------------------------------------------------ status

    def set_status(self, task_id: str, status: str) -> dict[str, Any]:
        with self._lock:
            if status not in ALL_STATUSES:
                raise ParallelCoordinatorError(
                    f"unknown task status {status!r}; allowed: {sorted(ALL_STATUSES)}"
                )
            record = self._get(task_id)
            if status == STATUS_RECOVERING:
                raise ParallelCoordinatorError(
                    "use begin_recovery() to enter RECOVERING; set_status cannot"
                )
            if record.status == STATUS_FAILED and status == STATUS_DONE:
                raise ParallelCoordinatorError(
                    f"{task_id}: FAILED -> DONE requires begin_recovery()/complete_recovery() "
                    "with explicit recovery evidence"
                )
            if record.status == STATUS_RECOVERING and status not in {STATUS_DONE, STATUS_FAILED, STATUS_RUNNING}:
                raise ParallelCoordinatorError(
                    f"RECOVERING may only move to RUNNING/DONE/FAILED, not {status}"
                )
            if status == STATUS_RUNNING:
                self._assert_serialization_running_exclusive(record)
            record.status = status
            return {"task_id": task_id, "status": status}

    def attach_evidence_ref(self, task_id: str, evidence_ref: str) -> dict[str, Any]:
        """Store a reference to evidence owned elsewhere — not the evidence itself."""
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ParallelCoordinatorError("evidence_ref must be a non-empty string reference")
        record = self._get(task_id)
        record.evidence_ref = evidence_ref.strip()
        self._emit_observability(
            task_id=task_id,
            event="phase_end",
            phase_id="parallel_evidence_ref",
            evidence_references=[record.evidence_ref],
            worktree=record.worktree,
        )
        return {"task_id": task_id, "evidence_ref": record.evidence_ref}

    def mark_failed(self, task_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Fail exactly one task. Unrelated tasks keep their status."""
        with self._lock:
            return self._mark_failed_locked(task_id, reason=reason)

    def _mark_failed_locked(self, task_id: str, *, reason: str | None = None) -> dict[str, Any]:
        record = self._get(task_id)
        record.status = STATUS_FAILED
        record.recovery = {
            "state": "failed",
            "reason": reason or "task failed",
            "cascaded": False,
        }
        return {
            "task_id": task_id,
            "status": STATUS_FAILED,
            "unaffected": tuple(
                tid for tid in self._claim_order if tid != task_id and self._tasks[tid].status != STATUS_FAILED
            ),
        }

    def begin_recovery(self, task_id: str) -> dict[str, Any]:
        """Enter recovery for one FAILED task without failing siblings."""
        with self._lock:
            return self._begin_recovery_locked(task_id)

    def _begin_recovery_locked(self, task_id: str) -> dict[str, Any]:
        record = self._get(task_id)
        if record.status != STATUS_FAILED:
            raise ParallelCoordinatorError(
                f"{task_id} is not FAILED; nothing to recover (status={record.status})"
            )
        record.status = STATUS_RECOVERING
        record.recovery = {
            "state": "recovering",
            "reason": (record.recovery or {}).get("reason", "recovery requested"),
            "cascaded": False,
        }
        siblings = {
            tid: self._tasks[tid].status
            for tid in self._claim_order
            if tid != task_id
        }
        return {
            "task_id": task_id,
            "status": STATUS_RECOVERING,
            "sibling_statuses": siblings,
        }

    def complete_recovery(self, task_id: str, *, success: bool) -> dict[str, Any]:
        with self._lock:
            return self._complete_recovery_locked(task_id, success=success)

    def _complete_recovery_locked(self, task_id: str, *, success: bool) -> dict[str, Any]:
        record = self._get(task_id)
        if record.status != STATUS_RECOVERING:
            raise ParallelCoordinatorError(
                f"{task_id} is not RECOVERING (status={record.status})"
            )
        record.status = STATUS_DONE if success else STATUS_FAILED
        record.recovery = {
            "state": "recovered" if success else "failed",
            "reason": (record.recovery or {}).get("reason", ""),
            "cascaded": False,
            "success": success,
        }
        return {"task_id": task_id, "status": record.status, "recovery": dict(record.recovery)}

    # ----------------------------------------------------------- interruptions

    def record_interruption(
        self,
        task_id: str,
        *,
        interruption_class: str,
        reason: str,
        required_decision_or_action: str,
        phase: str,
        gate_kind: str | None = None,
        prepared_action: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one required Operator interruption on a single task."""
        if interruption_class not in INTERRUPTION_CLASSES:
            raise ParallelCoordinatorError(
                f"unclassified interruption {interruption_class!r} rejected; "
                f"allowed: {sorted(INTERRUPTION_CLASSES)}"
            )
        if not reason or not required_decision_or_action:
            raise ParallelCoordinatorError(
                "interruption requires reason and required_decision_or_action"
            )
        if not phase:
            raise ParallelCoordinatorError("interruption requires phase")

        payload: dict[str, Any] = {
            "class": interruption_class,
            "phase": phase,
            "reason": reason,
            "required_decision_or_action": required_decision_or_action,
            "required": True,
            "task_id": task_id,
            "open": True,
        }
        if interruption_class == MANDATORY_OPERATOR_GATE:
            if gate_kind not in EXISTING_OPERATOR_GATE_KINDS:
                raise ParallelCoordinatorError(
                    f"MANDATORY_OPERATOR_GATE requires an existing gate kind "
                    f"in {sorted(EXISTING_OPERATOR_GATE_KINDS)}, got {gate_kind!r}"
                )
            if not prepared_action:
                raise ParallelCoordinatorError(
                    "MANDATORY_OPERATOR_GATE requires prepared_action (exact action)"
                )
            payload["gate_kind"] = gate_kind
            payload["prepared_action"] = dict(prepared_action)

        record = self._get(task_id)
        record.interruptions.append(payload)
        return dict(payload)

    def batch_operator_interruptions(self) -> dict[str, Any]:
        """Batch all open required interruptions across tasks for one Operator surface.

        Ordering is deterministic: claim order, then append order within a task.
        """
        batched: list[dict[str, Any]] = []
        for task_id in self._claim_order:
            for item in self._tasks[task_id].interruptions:
                if item.get("open"):
                    batched.append(dict(item))
        by_class: dict[str, list[dict[str, Any]]] = {
            MANDATORY_OPERATOR_GATE: [],
            UNRESOLVABLE_BLOCKER: [],
            NEW_MATERIAL_PRODUCT_DECISION: [],
        }
        for item in batched:
            by_class[item["class"]].append(item)
        return {
            "count": len(batched),
            "interruptions": batched,
            "by_class": {key: list(value) for key, value in by_class.items()},
            "task_ids": tuple(dict.fromkeys(item["task_id"] for item in batched)),
        }

    def resolve_interruption(self, task_id: str, *, index: int = -1) -> dict[str, Any]:
        """Mark one interruption closed after Operator action (in-memory only)."""
        record = self._get(task_id)
        if not record.interruptions:
            raise ParallelCoordinatorError(f"{task_id} has no interruptions")
        item = record.interruptions[index]
        item["open"] = False
        return dict(item)

    # -------------------------------------------------------------- scheduling

    def detect_overlaps(self, task_id_a: str, task_id_b: str) -> tuple[str, ...]:
        a = self._get(task_id_a)
        b = self._get(task_id_b)
        return overlapping_paths(a.owned_paths, b.owned_paths)

    def schedule(self) -> dict[str, Any]:
        """Compute parallel waves under the active overlap policy.

        refuse: every claimed task is its own wave when no overlaps among
        non-terminal tasks; overlapping active claims are impossible (claim
        would have raised).

        serialize: overlapping tasks share a serialization group and appear
        in distinct sequential waves (claim order within the group); tasks
        with no overlaps may share a wave.
        """
        active = [
            self._tasks[tid]
            for tid in self._claim_order
            if self._tasks[tid].status in ACTIVE_CLAIM_STATUSES | {STATUS_PENDING}
        ]
        if self.overlap_policy == OVERLAP_REFUSE:
            waves = tuple((record.task_id,) for record in active)
            return {
                "overlap_policy": self.overlap_policy,
                "waves": waves,
                "parallel_eligible": True,
            }

        remaining = [record.task_id for record in active]
        waves_list: list[tuple[str, ...]] = []
        while remaining:
            wave: list[str] = []
            wave_paths: list[str] = []
            for tid in list(remaining):
                record = self._tasks[tid]
                if wave_paths and overlapping_paths(record.owned_paths, wave_paths):
                    continue
                wave.append(tid)
                wave_paths.extend(record.owned_paths)
            if not wave:
                # Fail closed rather than spin: force next remaining alone.
                wave = [remaining[0]]
            waves_list.append(tuple(wave))
            for tid in wave:
                remaining.remove(tid)
        return {
            "overlap_policy": self.overlap_policy,
            "waves": tuple(waves_list),
            "parallel_eligible": any(len(wave) > 1 for wave in waves_list) or len(waves_list) <= 1,
        }

    # -------------------------------------------------------------------- join

    def join(self, *, required: Sequence[str] | None = None) -> dict[str, Any]:
        """Join required tasks into JOINED / NOT_READY / BLOCKED metadata."""
        with self._lock:
            return self._join_locked(required=required)

    def _join_locked(self, *, required: Sequence[str] | None = None) -> dict[str, Any]:
        if required is None:
            ordered = tuple(self._claim_order)
        else:
            ordered = self._ordered_required(required)
        if not ordered:
            raise ParallelCoordinatorError("a join requires at least one required task_id")

        pending = tuple(
            tid for tid in ordered if self._tasks[tid].status not in TERMINAL_STATUSES
        )
        if pending:
            return {
                "verdict": JOIN_NOT_READY,
                "pending": pending,
                "blockers": (),
                "contributing": (),
            }

        failed = tuple(tid for tid in ordered if self._tasks[tid].status != STATUS_DONE)
        if failed:
            return {
                "verdict": JOIN_BLOCKED,
                "pending": (),
                "blockers": tuple(f"task:{tid}" for tid in failed),
                "contributing": (),
            }

        contributing = tuple(
            {
                "task_id": tid,
                "work_id": self._tasks[tid].work_id,
                "status": self._tasks[tid].status,
                "evidence_ref": self._tasks[tid].evidence_ref,
            }
            for tid in ordered
        )
        return {
            "verdict": JOIN_JOINED,
            "pending": (),
            "blockers": (),
            "contributing": contributing,
        }

    def task(self, task_id: str) -> dict[str, Any]:
        return self._get(task_id).as_dict()

    def tasks(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._tasks[tid].as_dict() for tid in self._claim_order)

    # ----------------------------------------------------------------- helpers

    def _assert_serialization_running_exclusive(self, record: TaskRecord) -> None:
        """Serialized overlapping tasks cannot both be RUNNING."""
        peers: list[TaskRecord] = []
        for other in self._tasks.values():
            if other.task_id == record.task_id:
                continue
            related = False
            if record.serialization_group and other.task_id == record.serialization_group:
                related = True
            if other.serialization_group and other.serialization_group == record.task_id:
                related = True
            if (
                record.serialization_group
                and other.serialization_group
                and record.serialization_group == other.serialization_group
            ):
                related = True
            if related:
                peers.append(other)
        for other in peers:
            if other.status == STATUS_RUNNING:
                raise ParallelCoordinatorError(
                    f"serialization group already has RUNNING task "
                    f"{other.task_id}; cannot start {record.task_id}"
                )

    def _require_task_id(self, task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ParallelCoordinatorError(f"task_id must be a non-empty string, got {task_id!r}")
        return task_id.strip()

    def _get(self, task_id: str) -> TaskRecord:
        task_id = self._require_task_id(task_id)
        if task_id not in self._tasks:
            raise ParallelCoordinatorError(f"unknown task_id: {task_id}")
        return self._tasks[task_id]

    def _ordered_required(self, required: Sequence[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        for tid in required:
            tid = self._require_task_id(tid)
            if tid not in self._tasks:
                raise ParallelCoordinatorError(f"join references unknown task_id: {tid}")
            if tid in seen:
                raise ParallelCoordinatorError(f"duplicate task_id in join requirement: {tid}")
            seen.add(tid)
        return tuple(tid for tid in self._claim_order if tid in seen)

    def _active_overlaps(
        self, *, task_id: str, owned_paths: Sequence[str]
    ) -> tuple[dict[str, Any], ...]:
        conflicts: list[dict[str, Any]] = []
        for other_id in self._claim_order:
            other = self._tasks[other_id]
            if other.task_id == task_id:
                continue
            if other.status not in ACTIVE_CLAIM_STATUSES:
                continue
            hits = overlapping_paths(owned_paths, other.owned_paths)
            if hits:
                conflicts.append(
                    {
                        "task_id": other.task_id,
                        "paths": hits,
                        "serialization_group": other.serialization_group,
                    }
                )
        return tuple(conflicts)

    def _serialization_group_for(self, conflicts: Sequence[Mapping[str, Any]]) -> str:
        for conflict in conflicts:
            group = conflict.get("serialization_group")
            if group:
                return str(group)
        # Earliest conflicting claim becomes the group identity.
        return str(conflicts[0]["task_id"])

    @staticmethod
    def _coerce_worktree(worktree: WorktreeIdentity | Mapping[str, str]) -> WorktreeIdentity:
        if isinstance(worktree, WorktreeIdentity):
            return worktree
        try:
            return WorktreeIdentity(
                path=str(worktree["path"]),
                branch=str(worktree["branch"]),
                head=str(worktree["head"]),
                index_path=str(worktree["index_path"]),
            )
        except KeyError as exc:
            raise ParallelCoordinatorError(
                f"worktree identity requires path/branch/head/index_path; missing {exc}"
            ) from exc


# Admission dispositions. A limited or gated item never rejects the batch.
ADMISSION_ADMITTED = "ADMITTED"
ADMISSION_ALREADY = "ALREADY_ADMITTED"
ADMISSION_WAITING = "WAITING_ON_DEPENDENCY"
ADMISSION_GATED = "GATED"
ADMISSION_RATE_LIMITED = "RATE_LIMITED"
ADMISSION_DEFERRED = "DEFERRED_CAPACITY"
ADMISSION_REFUSED = "REFUSED_OVERLAP"
ADMISSION_GAP = "GAP"
USAGE_LIMIT = "usage_limit"


def _as_str_list(value: Any, *, field_name: str, work_id: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ParallelCoordinatorError(
            f"{work_id} {field_name} must be a list of strings"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ParallelCoordinatorError(
                f"{work_id} {field_name} entries must be non-empty strings"
            )
        out.append(item.strip())
    return out



def _admission_targets(raw_targets: Any, providers: Sequence[str], work_id: str) -> list[dict[str, Any]]:
    """Provider-only targets when model capacity is not described.

    A target model is an opaque caller string. Admission does not know vendors.
    """
    if raw_targets is None:
        return [{"provider": provider, "model": None} for provider in providers]
    if isinstance(raw_targets, (str, bytes)) or not isinstance(raw_targets, Sequence):
        raise ParallelCoordinatorError(f"{work_id} targets must be a list")
    targets: list[dict[str, Any]] = []
    for item in raw_targets:
        if isinstance(item, str) and item.strip():
            targets.append({"provider": item.strip(), "model": None})
            continue
        if not isinstance(item, Mapping):
            raise ParallelCoordinatorError(f"{work_id} target must be a provider string or mapping")
        provider = item.get("provider")
        model = item.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise ParallelCoordinatorError(f"{work_id} target provider must be a non-empty string")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ParallelCoordinatorError(f"{work_id} target model must be a non-empty string when set")
        targets.append({"provider": provider.strip(), "model": None if model is None else model.strip()})
    if not targets:
        raise ParallelCoordinatorError(f"{work_id} targets must not be empty")
    return targets


def _target_health(health: Mapping[str, Any], provider: str, model: str | None) -> tuple[str, str]:
    """Return (state, kind). kind is unsupported when the provider has no model map."""
    raw = health.get(provider, "ok")
    if isinstance(raw, str):
        return raw, "unsupported"
    if isinstance(raw, Mapping):
        if model is None:
            return "unavailable", "missing_model"
        state = raw.get(model, "unavailable")
        if not isinstance(state, str) or not state:
            return "unavailable", "model"
        return state, "model"
    return "unavailable", "unsupported"


def _unselected_disposition(attempts: Sequence[Mapping[str, Any]]) -> str:
    """A usage limit stays rate-limited. Unsupported model capacity is GAP."""
    if any(attempt.get("state") == USAGE_LIMIT for attempt in attempts):
        return ADMISSION_RATE_LIMITED
    if attempts and all(
        attempt.get("model_capacity") in {"unsupported", "missing_model"}
        for attempt in attempts
    ):
        return ADMISSION_GAP
    return ADMISSION_RATE_LIMITED


def admit_workstreams(
    items: Sequence[Mapping[str, Any]],
    *,
    capacity: int,
    provider_health: Mapping[str, Any] | None = None,
    already_admitted: Mapping[str, Mapping[str, Any]] | None = None,
    completed: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Admit independent work without letting one blocked item stop the turn.

    ``capacity`` bounds new admissions only. An already-admitted work_id keeps
    its provider and task_id and is not admitted again. A usage-limited
    provider is skipped in favor of the next eligible provider. If every
    eligible provider is limited, that work_id stays RATE_LIMITED and does not
    consume capacity. Gated and dependency-waiting items likewise leave
    capacity for unrelated work.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        raise ParallelCoordinatorError(f"capacity must be a non-negative int, got {capacity!r}")
    health = dict(provider_health or {})
    prior = dict(already_admitted or {})
    done = set(completed or [])
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ParallelCoordinatorError("items must be a sequence of work records")

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ParallelCoordinatorError("each work item must be a mapping")
        work_id = raw.get("work_id")
        if not isinstance(work_id, str) or not work_id.strip():
            raise ParallelCoordinatorError(f"work_id must be a non-empty string, got {work_id!r}")
        work_id = work_id.strip()
        if work_id in seen:
            raise ParallelCoordinatorError(f"duplicate work_id in admission batch: {work_id}")
        seen.add(work_id)
        owned = tuple(normalize_owned_path(path) for path in _as_str_list(
            raw.get("owned_paths"), field_name="owned_paths", work_id=work_id,
        ))
        providers = _as_str_list(raw.get("providers"), field_name="providers", work_id=work_id) or ["default"]
        records.append({
            "work_id": work_id,
            "dependencies": _as_str_list(raw.get("dependencies"), field_name="dependencies", work_id=work_id),
            "owned_paths": owned,
            "gated": bool(raw.get("gated") or raw.get("operator_gate_required")),
            "providers": providers,
            "targets": _admission_targets(raw.get("targets"), providers, work_id),
        })
    records.sort(key=lambda record: record["work_id"])

    occupied: dict[str, str] = {}
    for work_id, previous in prior.items():
        if not isinstance(previous, Mapping):
            raise ParallelCoordinatorError(f"already_admitted[{work_id!r}] must be a mapping")
        for path in previous.get("owned_paths") or []:
            occupied[normalize_owned_path(path)] = str(work_id)

    remaining = capacity
    results: list[dict[str, Any]] = []
    for record in records:
        work_id = record["work_id"]
        previous = prior.get(work_id)
        if previous is not None:
            results.append({
                "work_id": work_id,
                "disposition": ADMISSION_ALREADY,
                "provider": previous.get("provider"),
                "model": previous.get("model"),
                "model_capacity": previous.get("model_capacity"),
                "task_id": previous.get("task_id") or f"admit:{work_id}",
                "fallback": bool(previous.get("fallback")),
                "attempts": list(previous.get("attempts") or []),
                "owned_paths": list(record["owned_paths"]),
            })
            continue
        if record["gated"]:
            results.append({
                "work_id": work_id,
                "disposition": ADMISSION_GATED,
                "provider": None,
                "task_id": f"admit:{work_id}",
                "fallback": False,
                "attempts": [],
                "owned_paths": list(record["owned_paths"]),
            })
            continue
        waiting = [dep for dep in record["dependencies"] if dep not in done]
        if waiting:
            results.append({
                "work_id": work_id,
                "disposition": ADMISSION_WAITING,
                "provider": None,
                "task_id": f"admit:{work_id}",
                "fallback": False,
                "attempts": [],
                "waiting_on": waiting,
                "owned_paths": list(record["owned_paths"]),
            })
            continue
        conflicts = []
        for path in record["owned_paths"]:
            if any(paths_overlap(path, taken) for taken in occupied):
                conflicts.append(path)
        if conflicts:
            results.append({
                "work_id": work_id,
                "disposition": ADMISSION_REFUSED,
                "provider": None,
                "task_id": f"admit:{work_id}",
                "fallback": False,
                "attempts": [],
                "conflicts": conflicts,
                "owned_paths": list(record["owned_paths"]),
            })
            continue
        attempts: list[dict[str, Any]] = []
        selected_provider: str | None = None
        selected_model: str | None = None
        model_capacity = "unsupported"
        for target in record["targets"]:
            provider = target["provider"]
            model = target["model"]
            state, kind = _target_health(health, provider, model)
            attempt = {"provider": provider, "state": state}
            if model is not None:
                attempt["model"] = model
                attempt["model_capacity"] = kind
            attempts.append(attempt)
            if kind == "unsupported" and model is not None:
                continue
            if kind == "missing_model":
                continue
            if state == USAGE_LIMIT or state == "unavailable":
                continue
            selected_provider = provider
            selected_model = model
            model_capacity = "selected" if kind == "model" else "unsupported"
            break
        first = record["targets"][0]
        fallback = (selected_provider, selected_model) != (first["provider"], first["model"])
        if selected_provider is None:
            results.append({
                "work_id": work_id,
                "disposition": _unselected_disposition(attempts),
                "provider": None,
                "model": None,
                "model_capacity": model_capacity,
                "task_id": f"admit:{work_id}",
                "fallback": False,
                "attempts": attempts,
                "owned_paths": list(record["owned_paths"]),
            })
            continue
        if remaining <= 0:
            results.append({
                "work_id": work_id,
                "disposition": ADMISSION_DEFERRED,
                "provider": None,
                "model": None,
                "model_capacity": model_capacity,
                "task_id": f"admit:{work_id}",
                "fallback": False,
                "attempts": attempts,
                "owned_paths": list(record["owned_paths"]),
            })
            continue
        remaining -= 1
        for path in record["owned_paths"]:
            occupied[path] = work_id
        results.append({
            "work_id": work_id,
            "disposition": ADMISSION_ADMITTED,
            "provider": selected_provider,
            "model": selected_model,
            "model_capacity": model_capacity,
            "task_id": f"admit:{work_id}",
            "fallback": fallback,
            "attempts": attempts,
            "owned_paths": list(record["owned_paths"]),
        })

    return {
        "capacity": capacity,
        "admitted_count": sum(1 for result in results if result["disposition"] == ADMISSION_ADMITTED),
        "results": results,
    }


def join_admissions(
    *,
    required: Sequence[str],
    statuses: Mapping[str, str],
) -> dict[str, Any]:
    """Join only the required work ids. Unrelated gated work does not block them."""
    if isinstance(required, str) or not isinstance(required, Sequence):
        raise ParallelCoordinatorError("required must be a sequence of work ids")
    ordered = tuple(required)
    missing = [work_id for work_id in ordered if work_id not in statuses]
    failed = [work_id for work_id in ordered if statuses.get(work_id) == STATUS_FAILED]
    if failed:
        verdict = JOIN_BLOCKED
    elif missing or any(statuses.get(work_id) != STATUS_DONE for work_id in ordered):
        verdict = JOIN_NOT_READY
    else:
        verdict = JOIN_JOINED
    return {
        "verdict": verdict,
        "required": list(ordered),
        "failed": failed,
        "not_done": [
            work_id for work_id in ordered
            if statuses.get(work_id) != STATUS_DONE
        ],
    }
