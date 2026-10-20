#!/usr/bin/env python3
"""Deterministic Factory Run Engine with Typed Evidence Envelopes.

Provider-neutral phase runner used by the Autonomous Execution Contract
during EXECUTING / VERIFYING. This is NOT a second planner, task database,
Master Plan, or UI.

Phase kinds (exact literals):
  CODE  — deterministic operations (command / git_diff / artifact_check)
  AGENT — judgment/generative work (claims are manifests, never trusted proof)
  TOOL  — external provider/tool actions (claims gated like AGENT)

Every phase result is a typed evidence envelope. Gates verify claims against
real artifacts/checks before acceptance. DONE requires provenance-bearing
envelopes produced here — caller-asserted booleans alone cannot satisfy DONE.

AGENT/TOOL phases execute through a provider-neutral binding interface
(``studio_factory_provider_binding``) or a registered producer callable.
Bindings record real provider/runtime/session provenance; same-session
repair keeps the session id stable even when host resume is unsupported.

VERIFYING may also run blast-radius / Canary / interrogate adapters from
``studio_verification_adapters`` via an explicit envelope bridge (adapters
are not AgentProducers; GAP/REFUSED stay non-PASSED and never NEEDS_REPAIR).
Live provider bindings (``ClaudeCodeLiveBinding``) may be supplied by the
caller through ``provider_bindings``; this module does not hardcode a live
spawn. Parallel multi-task coordination lives in
``studio_parallel_coordinator`` (packaging import only — not inside the
factory).

Shipped in FRONT_DOOR_RUNTIME_MODULES because studio_autonomous_execution
imports it (transitive Front Door closure).
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

# Soft import: present in this kit worktree; packaged Front Door may omit until
# FRONT_DOOR_RUNTIME_MODULES ships studio_factory_provider_binding.
try:
    import studio_factory_provider_binding as _studio_factory_provider_binding
except ImportError:  # pragma: no cover
    _studio_factory_provider_binding = None

# VERIFYING adapters (functional) + packaging imports for live bindings /
# parallel coordinator so the Front Door runtime closure ships them.
import studio_verification_adapters as _verification_adapters
import studio_live_provider_runtime  # noqa: F401
import studio_parallel_coordinator  # noqa: F401
import studio_execution_routing
from studio_runtime_observability import (
    RuntimeObservabilityLedger,
    ingest_factory_ledger_entry,
)

if TYPE_CHECKING:
    from studio_factory_provider_binding import (
        BindingExecutionResult,
        ProviderBinding,
    )


PHASE_KIND_CODE = "CODE"
PHASE_KIND_AGENT = "AGENT"
PHASE_KIND_TOOL = "TOOL"
PHASE_KINDS = frozenset({PHASE_KIND_CODE, PHASE_KIND_AGENT, PHASE_KIND_TOOL})

STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_GATE_FAILED = "GATE_FAILED"
STATUS_NEEDS_REPAIR = "NEEDS_REPAIR"
# Adapter-local statuses preserved through the envelope bridge (never PASS /
# never NEEDS_REPAIR). run_phases fail-closes unless status is PASSED.
STATUS_GAP = _verification_adapters.STATUS_GAP
STATUS_REFUSED = _verification_adapters.STATUS_REFUSED

PRODUCER_BLAST_RADIUS = _verification_adapters.PRODUCER_BLAST_RADIUS
PRODUCER_CANARY = _verification_adapters.PRODUCER_CANARY
PRODUCER_INTERROGATE = _verification_adapters.PRODUCER_INTERROGATE

VERIFICATION_ADAPTER_PRODUCERS = frozenset({
    PRODUCER_BLAST_RADIUS,
    PRODUCER_CANARY,
    PRODUCER_INTERROGATE,
})

_ADAPTER_CALLABLES = {
    PRODUCER_BLAST_RADIUS: _verification_adapters.blast_radius_adapter,
    PRODUCER_CANARY: _verification_adapters.canary_browser_adapter,
    PRODUCER_INTERROGATE: _verification_adapters.interrogate_adversarial_adapter,
}

OP_COMMAND = "command"
OP_GIT_DIFF = "git_diff"
OP_ARTIFACT_CHECK = "artifact_check"
CODE_OPERATIONS = frozenset({OP_COMMAND, OP_GIT_DIFF, OP_ARTIFACT_CHECK})

DEFAULT_MAX_REPAIRS = 2


class FactoryRunError(RuntimeError):
    """Fail-closed factory-run contract violation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PhaseSpec:
    """Explicit phase classification for the runner."""

    phase_id: str
    phase_kind: str
    producer: str
    work_order_id: str | None = None
    operation: str | None = None  # CODE only
    command: Sequence[str] | None = None
    artifact_paths: Sequence[str] | None = None
    require_nonempty: bool = True
    notes: Sequence[str] | None = None
    # VERIFYING adapter kwargs (blast-radius / canary / interrogate). Not used
    # by CODE/AGENT/TOOL producer paths; adapters are not AgentProducers.
    adapter_input: Mapping[str, Any] | None = None
    # Optional execution-routing request for AGENT/TOOL model/effort selection.
    # When absent, bindings keep their own model/effort. Routing GAP/REFUSED
    # stay non-PASSED and are never remapped to NEEDS_REPAIR.
    routing_request: Mapping[str, Any] | None = None


@dataclass
class AgentClaimPayload:
    """Manifest of claims from an AGENT/TOOL producer — not trusted proof."""

    claims: list[dict[str, Any]]
    claimed_artifacts: list[str] = field(default_factory=list)
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    provider: str | None = None
    runtime: str | None = None
    continuation_supported: bool | None = None
    resumed_session: bool = False
    notes: list[str] = field(default_factory=list)
    # Optional provider outcome (FAILED/GAP/REFUSED/is_error). Never invent PASS.
    provider_status: str | None = None
    is_error: bool = False


# (phase, prior_envelope_or_None, repair_context_or_None) → AgentClaimPayload
AgentProducer = Callable[
    [PhaseSpec, "EvidenceEnvelope | None", Mapping[str, Any] | None],
    AgentClaimPayload,
]


@dataclass
class EvidenceEnvelope:
    """Typed evidence record produced by every phase."""

    task_id: str
    work_order_id: str | None
    phase_id: str
    phase_kind: str
    producer: str
    status: str
    claims: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    provenance: dict[str, Any]
    timestamps: dict[str, str]
    notes_for_next_phase: list[str] = field(default_factory=list)
    session_continuation: dict[str, Any] | None = None
    repair_target: str | None = None
    retry_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "work_order_id": self.work_order_id,
            "phase_id": self.phase_id,
            "phase_kind": self.phase_kind,
            "producer": self.producer,
            "status": self.status,
            "claims": list(self.claims),
            "artifacts": list(self.artifacts),
            "checks": list(self.checks),
            "provenance": dict(self.provenance),
            "timestamps": dict(self.timestamps),
            "notes_for_next_phase": list(self.notes_for_next_phase),
            "session_continuation": (
                dict(self.session_continuation) if self.session_continuation else None
            ),
            "repair_target": self.repair_target,
            "retry_count": self.retry_count,
        }


@dataclass
class LedgerEntry:
    phase_id: str
    event: str  # phase_start | phase_end | gate_pass | gate_fail | repair
    producer_kind: str
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    duration_ms: int | None = None
    gate_result: str | None = None
    retry_count: int = 0
    artifacts: list[str] = field(default_factory=list)
    interruptions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "event": self.event,
            "producer_kind": self.producer_kind,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "duration_ms": self.duration_ms,
            "gate_result": self.gate_result,
            "retry_count": self.retry_count,
            "artifacts": list(self.artifacts),
            "interruptions": list(self.interruptions),
        }


@dataclass
class FactoryRunResult:
    """In-memory outcome — not durable Notion or GitHub project/engineering truth."""

    envelopes: list[EvidenceEnvelope] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    content_fingerprint: str | None = None
    verification_fingerprint: str | None = None
    all_passed: bool = False
    unresolvable: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "envelopes": [e.as_dict() for e in self.envelopes],
            "ledger": [e.as_dict() for e in self.ledger],
            "content_fingerprint": self.content_fingerprint,
            "verification_fingerprint": self.verification_fingerprint,
            "all_passed": self.all_passed,
            "unresolvable": self.unresolvable,
        }


def capture_content_fingerprint(
    cwd: Path | str,
    *,
    artifact_paths: Sequence[str] | None = None,
) -> str:
    """Fingerprint HEAD SHA, tracked diff, untracked names+content, artifacts."""
    try:
        return _verification_adapters.compute_worktree_verification_fingerprint(
            cwd, artifact_paths=artifact_paths,
        )
    except _verification_adapters.VerificationAdapterError as exc:
        raise FactoryRunError(str(exc)) from exc


def produce_command(
    *,
    task_id: str,
    phase: PhaseSpec,
    cwd: Path,
    retry_count: int = 0,
) -> EvidenceEnvelope:
    if not phase.command:
        raise FactoryRunError(f"CODE command phase {phase.phase_id!r} requires command")
    started = _utc_now()
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            list(phase.command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        ended = _utc_now()
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, (bytes, bytearray)) else (exc.stderr or "")
        return EvidenceEnvelope(
            task_id=task_id,
            work_order_id=phase.work_order_id,
            phase_id=phase.phase_id,
            phase_kind=PHASE_KIND_CODE,
            producer=phase.producer,
            status=STATUS_FAILED,
            claims=[],
            artifacts=[
                {
                    "kind": "command_output",
                    "stdout_sha256": _sha256_text(stdout),
                    "stderr_sha256": _sha256_text(stderr),
                    "stdout_len": len(stdout),
                    "stderr_len": len(stderr),
                    "timeout": True,
                }
            ],
            checks=[{
                "name": "command_timeout",
                "passed": False,
                "timeout_seconds": 120,
                "command": list(phase.command),
            }],
            provenance={
                "command": list(phase.command),
                "tool": None,
                "provider": None,
                "runtime": "subprocess",
                "model": None,
                "effort": None,
                "session": None,
                "exit_code": None,
                "cwd": str(cwd),
                "duration_ms": duration_ms,
                "timeout": True,
                "stdout_preview": stdout[:500],
                "stderr_preview": stderr[:500],
            },
            timestamps={"started_at": started, "ended_at": ended},
            notes_for_next_phase=list(phase.notes or []) + ["CODE phase timed out"],
            retry_count=retry_count,
        )
    duration_ms = int((time.monotonic() - t0) * 1000)
    ended = _utc_now()
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    passed = proc.returncode == 0
    check = {
        "name": "command_exit_code",
        "passed": passed,
        "exit_code": proc.returncode,
        "command": list(phase.command),
    }
    return EvidenceEnvelope(
        task_id=task_id,
        work_order_id=phase.work_order_id,
        phase_id=phase.phase_id,
        phase_kind=PHASE_KIND_CODE,
        producer=phase.producer,
        status=STATUS_PASSED if passed else STATUS_FAILED,
        claims=[],
        artifacts=[
            {
                "kind": "command_output",
                "stdout_sha256": _sha256_text(stdout),
                "stderr_sha256": _sha256_text(stderr),
                "stdout_len": len(stdout),
                "stderr_len": len(stderr),
            }
        ],
        checks=[check],
        provenance={
            "command": list(phase.command),
            "tool": None,
            "provider": None,
            "runtime": "subprocess",
            "model": None,
            "effort": None,
            "session": None,
            "exit_code": proc.returncode,
            "cwd": str(cwd),
            "duration_ms": duration_ms,
            "stdout_preview": stdout[:500],
            "stderr_preview": stderr[:500],
        },
        timestamps={"started_at": started, "ended_at": ended},
        notes_for_next_phase=list(phase.notes or []),
        retry_count=retry_count,
    )


def produce_git_diff(
    *,
    task_id: str,
    phase: PhaseSpec,
    cwd: Path,
    retry_count: int = 0,
) -> EvidenceEnvelope:
    started = _utc_now()
    t0 = time.monotonic()
    proc = subprocess.run(
        ["git", "-C", str(cwd), "diff", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    ended = _utc_now()
    if proc.returncode not in (0, 1):
        err = (proc.stderr or "").strip()
        if "unknown revision" in err.lower() or "bad revision" in err.lower() or "does not have any commits" in err.lower():
            # Empty repo: treat as empty diff capture success
            fingerprint = _sha256_text("")
            return EvidenceEnvelope(
                task_id=task_id,
                work_order_id=phase.work_order_id,
                phase_id=phase.phase_id,
                phase_kind=PHASE_KIND_CODE,
                producer=phase.producer,
                status=STATUS_PASSED,
                claims=[],
                artifacts=[{
                    "kind": "git_diff",
                    "diff_sha256": fingerprint,
                    "diff_len": 0,
                    "empty": True,
                }],
                checks=[{
                    "name": "git_diff_captured",
                    "passed": True,
                    "diff_sha256": fingerprint,
                }],
                provenance={
                    "command": ["git", "diff", "HEAD"],
                    "tool": "git",
                    "provider": None,
                    "runtime": "subprocess",
                    "model": None,
                    "effort": None,
                    "session": None,
                    "exit_code": proc.returncode,
                    "cwd": str(cwd),
                    "duration_ms": duration_ms,
                    "content_fingerprint": fingerprint,
                    "artifact_paths": [],
                },
                timestamps={"started_at": started, "ended_at": ended},
                notes_for_next_phase=list(phase.notes or []) + [f"diff_sha256={fingerprint}"],
                retry_count=retry_count,
            )
        return EvidenceEnvelope(
            task_id=task_id,
            work_order_id=phase.work_order_id,
            phase_id=phase.phase_id,
            phase_kind=PHASE_KIND_CODE,
            producer=phase.producer,
            status=STATUS_FAILED,
            claims=[],
            artifacts=[],
            checks=[{
                "name": "git_diff",
                "passed": False,
                "exit_code": proc.returncode,
                "stderr": err[:500],
            }],
            provenance={
                "command": ["git", "diff", "HEAD"],
                "tool": "git",
                "provider": None,
                "runtime": "subprocess",
                "model": None,
                "effort": None,
                "session": None,
                "exit_code": proc.returncode,
                "cwd": str(cwd),
                "duration_ms": duration_ms,
            },
            timestamps={"started_at": started, "ended_at": ended},
            retry_count=retry_count,
        )
    diff_text = proc.stdout or ""
    fingerprint = _sha256_text(diff_text)
    return EvidenceEnvelope(
        task_id=task_id,
        work_order_id=phase.work_order_id,
        phase_id=phase.phase_id,
        phase_kind=PHASE_KIND_CODE,
        producer=phase.producer,
        status=STATUS_PASSED,
        claims=[],
        artifacts=[{
            "kind": "git_diff",
            "diff_sha256": fingerprint,
            "diff_len": len(diff_text),
            "empty": len(diff_text.strip()) == 0,
        }],
        checks=[{
            "name": "git_diff_captured",
            "passed": True,
            "diff_sha256": fingerprint,
        }],
        provenance={
            "command": ["git", "diff", "HEAD"],
            "tool": "git",
            "provider": None,
            "runtime": "subprocess",
            "model": None,
            "effort": None,
            "session": None,
            "exit_code": proc.returncode,
            "cwd": str(cwd),
            "duration_ms": duration_ms,
            "content_fingerprint": fingerprint,
            "artifact_paths": [],
        },
        timestamps={"started_at": started, "ended_at": ended},
        notes_for_next_phase=list(phase.notes or []) + [f"diff_sha256={fingerprint}"],
        retry_count=retry_count,
    )


def produce_artifact_check(
    *,
    task_id: str,
    phase: PhaseSpec,
    cwd: Path,
    retry_count: int = 0,
) -> EvidenceEnvelope:
    if not phase.artifact_paths:
        raise FactoryRunError(
            f"CODE artifact_check phase {phase.phase_id!r} requires artifact_paths"
        )
    started = _utc_now()
    artifacts: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    all_ok = True
    for rel in phase.artifact_paths:
        path = Path(rel) if Path(rel).is_absolute() else cwd / rel
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        nonempty = size > 0
        ok = exists and (nonempty if phase.require_nonempty else True)
        if not ok:
            all_ok = False
        artifacts.append({
            "kind": "filesystem",
            "path": str(path),
            "exists": exists,
            "size": size,
            "nonempty": nonempty,
        })
        checks.append({
            "name": "artifact_exists",
            "path": str(path),
            "passed": ok,
            "require_nonempty": phase.require_nonempty,
        })
    ended = _utc_now()
    return EvidenceEnvelope(
        task_id=task_id,
        work_order_id=phase.work_order_id,
        phase_id=phase.phase_id,
        phase_kind=PHASE_KIND_CODE,
        producer=phase.producer,
        status=STATUS_PASSED if all_ok else STATUS_FAILED,
        claims=[],
        artifacts=artifacts,
        checks=checks,
        provenance={
            "command": None,
            "tool": "filesystem",
            "provider": None,
            "runtime": "local",
            "model": None,
            "effort": None,
            "session": None,
            "exit_code": 0 if all_ok else 1,
            "cwd": str(cwd),
            "artifact_paths": [a["path"] for a in artifacts],
        },
        timestamps={"started_at": started, "ended_at": ended},
        notes_for_next_phase=list(phase.notes or []),
        retry_count=retry_count,
    )


def gate_agent_or_tool_envelope(
    envelope: EvidenceEnvelope,
    *,
    cwd: Path,
    before_file_index: Mapping[str, str] | None = None,
) -> EvidenceEnvelope:
    """Verify AGENT/TOOL claims against real phase-produced artifacts.

    Rejects absolute/escaping paths, pre-existing unchanged files, and claims
    not supported by the phase diff or untracked changes. Claims alone never pass.
    """
    if envelope.phase_kind not in {PHASE_KIND_AGENT, PHASE_KIND_TOOL}:
        return envelope

    root = Path(cwd).resolve()
    claimed_paths: list[str] = []
    for claim in envelope.claims:
        if not isinstance(claim, Mapping):
            continue
        for key in ("artifact", "artifact_path", "path"):
            if claim.get(key):
                claimed_paths.append(str(claim[key]))
        for path in claim.get("artifacts") or []:
            claimed_paths.append(str(path))
    for art in envelope.artifacts:
        if isinstance(art, Mapping) and art.get("path"):
            claimed_paths.append(str(art["path"]))

    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in claimed_paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    checks: list[dict[str, Any]] = list(envelope.checks)
    verified_artifacts: list[dict[str, Any]] = []
    gate_ok = True
    before = dict(before_file_index or {})

    # Paths touched by the phase (diff + untracked), used as claim support.
    porcelain = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    changed_rels: set[str] = set()
    if porcelain.returncode == 0:
        for line in (porcelain.stdout or "").splitlines():
            if len(line) < 4:
                continue
            entry = line[3:].strip()
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1].strip()
            if entry.startswith('"') and entry.endswith('"'):
                entry = entry[1:-1]
            changed_rels.add(entry.replace("\\", "/"))

    if not unique_paths:
        gate_ok = False
        checks.append({
            "name": "claim_artifact_gate",
            "passed": False,
            "reason": "agent/tool claim has no matching artifact or deterministic check",
        })
    else:
        for rel in unique_paths:
            raw = Path(rel)
            if raw.is_absolute():
                gate_ok = False
                checks.append({
                    "name": "claim_artifact_gate",
                    "path": rel,
                    "passed": False,
                    "reason": "absolute artifact paths are rejected",
                })
                continue
            candidate = (root / rel).resolve()
            try:
                rel_key = candidate.relative_to(root).as_posix()
            except ValueError:
                gate_ok = False
                checks.append({
                    "name": "claim_artifact_gate",
                    "path": rel,
                    "passed": False,
                    "reason": "artifact path escapes cwd/worktree",
                })
                continue

            exists = candidate.is_file() and candidate.stat().st_size > 0
            current_hash = (
                __import__("hashlib").sha256(candidate.read_bytes()).hexdigest()
                if exists else None
            )

            prior_hash = before.get(rel_key)
            unchanged_preexisting = (
                prior_hash is not None and current_hash is not None and prior_hash == current_hash
            )
            supported_by_phase = rel_key in changed_rels or (
                prior_hash is None and exists
            ) or (
                prior_hash is not None and current_hash is not None and prior_hash != current_hash
            )
            # Pre-existing unchanged files are never valid claims.
            if unchanged_preexisting:
                supported_by_phase = False

            passed = bool(exists and supported_by_phase and not unchanged_preexisting)
            reason = None
            if not exists:
                reason = "artifact missing or empty"
            elif unchanged_preexisting:
                reason = "artifact existed unchanged before the phase"
            elif not supported_by_phase:
                reason = "artifact claim not supported by phase diff or untracked changes"
            checks.append({
                "name": "claim_artifact_gate",
                "path": str(candidate),
                "relpath": rel_key,
                "passed": passed,
                **({"reason": reason} if reason else {}),
            })
            verified_artifacts.append({
                "kind": "gated_artifact",
                "path": str(candidate),
                "relpath": rel_key,
                "exists": exists,
                "size": candidate.stat().st_size if candidate.is_file() else 0,
                "content_sha256": current_hash,
                "changed_in_phase": bool(supported_by_phase and not unchanged_preexisting),
            })
            if not passed:
                gate_ok = False

    status = STATUS_PASSED if gate_ok else STATUS_GATE_FAILED
    return EvidenceEnvelope(
        task_id=envelope.task_id,
        work_order_id=envelope.work_order_id,
        phase_id=envelope.phase_id,
        phase_kind=envelope.phase_kind,
        producer=envelope.producer,
        status=status,
        claims=list(envelope.claims),
        artifacts=list(envelope.artifacts) + verified_artifacts,
        checks=checks,
        provenance=dict(envelope.provenance),
        timestamps=dict(envelope.timestamps),
        notes_for_next_phase=list(envelope.notes_for_next_phase),
        session_continuation=envelope.session_continuation,
        repair_target=envelope.repair_target,
        retry_count=envelope.retry_count,
    )


def _build_agent_envelope(
    *,
    task_id: str,
    phase: PhaseSpec,
    payload: AgentClaimPayload,
    retry_count: int,
    started: str,
    ended: str,
) -> EvidenceEnvelope:
    artifacts = [
        {"kind": "claimed", "path": p} for p in payload.claimed_artifacts
    ]
    return EvidenceEnvelope(
        task_id=task_id,
        work_order_id=phase.work_order_id,
        phase_id=phase.phase_id,
        phase_kind=phase.phase_kind,
        producer=phase.producer,
        status=STATUS_PASSED,  # provisional; gate decides
        claims=list(payload.claims),
        artifacts=artifacts,
        checks=[],
        provenance={
            "command": None,
            "tool": phase.producer if phase.phase_kind == PHASE_KIND_TOOL else None,
            "provider": payload.provider,
            "runtime": payload.runtime,
            "model": payload.model,
            "effort": payload.effort,
            "session": payload.session_id,
            "continuation_supported": payload.continuation_supported,
            "resumed_session": bool(payload.resumed_session),
            "exit_code": None,
            "artifact_paths": list(payload.claimed_artifacts),
            "provider_status": payload.provider_status,
            "is_error": bool(payload.is_error),
        },
        timestamps={"started_at": started, "ended_at": ended},
        notes_for_next_phase=list(payload.notes) + list(phase.notes or []),
        retry_count=retry_count,
    )


def _repair_context_from_envelope(envelope: EvidenceEnvelope) -> dict[str, Any]:
    session_id = None
    if envelope.provenance.get("session"):
        session_id = envelope.provenance["session"]
    if envelope.session_continuation and envelope.session_continuation.get("session_id"):
        session_id = envelope.session_continuation["session_id"]
    continuation_supported = envelope.provenance.get("continuation_supported")
    if envelope.session_continuation and "continuation_supported" in envelope.session_continuation:
        continuation_supported = envelope.session_continuation.get("continuation_supported")
    return {
        "session_id": session_id,
        "producer": envelope.producer,
        "repair_target": envelope.phase_id,
        "failed_checks": [c for c in envelope.checks if not c.get("passed")],
        "prior_status": envelope.status,
        "session_continuation": True,
        "continuation_supported": continuation_supported,
    }


def _provider_outcome_from_binding(result: "BindingExecutionResult") -> tuple[str | None, bool]:
    """Extract FAILED/GAP/REFUSED/is_error from binding result without live coupling."""
    status = getattr(result, "status", None) or getattr(result, "provider_status", None)
    is_error = bool(getattr(result, "is_error", False))
    notes = list(getattr(result, "notes", []) or [])
    for note in notes:
        if not isinstance(note, str):
            continue
        if note.startswith("status="):
            status = note.split("=", 1)[1].strip() or status
        if note in {"is_error=true", "is_error=True", "is_error=1"}:
            is_error = True
    if status is not None:
        status = str(status)
    return status, is_error


def _claim_payload_from_binding(result: "BindingExecutionResult") -> AgentClaimPayload:
    provider_status, is_error = _provider_outcome_from_binding(result)
    return AgentClaimPayload(
        claims=list(result.claims),
        claimed_artifacts=list(result.claimed_artifacts),
        session_id=result.session_id,
        model=result.model,
        effort=result.effort,
        provider=result.provider,
        runtime=result.runtime,
        continuation_supported=result.continuation_supported,
        resumed_session=bool(result.resumed_session),
        notes=list(result.notes),
        provider_status=provider_status,
        is_error=is_error,
    )


def _session_continuation_record(
    *,
    payload: AgentClaimPayload,
    phase: PhaseSpec,
    retry_count: int | None = None,
    retries_exhausted: bool = False,
    max_repairs: int | None = None,
    prior: EvidenceEnvelope | None = None,
) -> dict[str, Any]:
    session_id = payload.session_id or (
        (prior.provenance.get("session") if prior else None)
    )
    record: dict[str, Any] = {
        "session_id": session_id,
        "producer": phase.producer,
        "repair_target": phase.phase_id,
        "bounded": True,
        "continuation_supported": bool(payload.continuation_supported)
        if payload.continuation_supported is not None
        else False,
        "resumed_session": bool(payload.resumed_session),
    }
    if retry_count is not None:
        record["retry_count"] = retry_count
    if retries_exhausted:
        record["retries_exhausted"] = True
    if max_repairs is not None:
        record["max_repairs"] = max_repairs
    return record



def bridge_verification_adapter_envelope(adapter_envelope: Any) -> EvidenceEnvelope:
    """Bridge adapter-local envelopes into factory EvidenceEnvelope.

    Adapter envelopes lack ``session_continuation`` / ``repair_target`` /
    ``retry_count``; those default to None / None / 0. ``GAP`` and
    ``REFUSED`` are kept as literals — never rewritten to PASSED or
    NEEDS_REPAIR. Factory ``run_phases`` fail-closes unless status is PASSED.
    """
    if hasattr(adapter_envelope, "as_dict"):
        data = adapter_envelope.as_dict()
    elif isinstance(adapter_envelope, Mapping):
        data = dict(adapter_envelope)
    else:
        raise FactoryRunError("verification adapter envelope is not bridgeable")

    status = str(data.get("status") or "")
    if status == STATUS_NEEDS_REPAIR:
        raise FactoryRunError(
            "verification adapter envelope must not carry NEEDS_REPAIR"
        )
    if status not in {STATUS_PASSED, STATUS_FAILED, STATUS_GAP, STATUS_REFUSED}:
        raise FactoryRunError(
            f"verification adapter status {status!r} is not bridgeable"
        )

    return EvidenceEnvelope(
        task_id=str(data["task_id"]),
        work_order_id=data.get("work_order_id"),
        phase_id=str(data["phase_id"]),
        phase_kind=str(data["phase_kind"]),
        producer=str(data["producer"]),
        status=status,
        claims=[dict(c) for c in (data.get("claims") or [])],
        artifacts=[dict(a) for a in (data.get("artifacts") or [])],
        checks=[dict(c) for c in (data.get("checks") or [])],
        provenance=dict(data.get("provenance") or {}),
        timestamps=dict(data.get("timestamps") or {}),
        notes_for_next_phase=[str(n) for n in (data.get("notes_for_next_phase") or [])],
        session_continuation=None,
        repair_target=None,
        retry_count=0,
    )



def _routing_status_to_factory(status: str) -> str:
    """Map execution-routing outcomes onto factory statuses.

    SELECTED is not used as a factory envelope status (execution continues).
    GAP / AMBIGUOUS → STATUS_GAP; REFUSED → STATUS_REFUSED. Never NEEDS_REPAIR.
    """
    if status == studio_execution_routing.STATUS_REFUSED:
        return STATUS_REFUSED
    if status in {
        studio_execution_routing.STATUS_GAP,
        studio_execution_routing.STATUS_AMBIGUOUS,
    }:
        return STATUS_GAP
    raise FactoryRunError(
        f"execution routing status {status!r} cannot become a factory envelope"
    )


def _resolve_phase_routing(
    phase: PhaseSpec,
    *,
    catalog: studio_execution_routing.ExecutionCatalog | None = None,
) -> tuple[str | None, str | None, studio_execution_routing.RoutingResult | None]:
    """Optionally route AGENT/TOOL model/effort. Returns (model, effort, result).

    When ``phase.routing_request`` is absent, returns (None, None, None).
    """
    if phase.routing_request is None:
        return None, None, None
    try:
        request = studio_execution_routing.parse_routing_request(phase.routing_request)
        result = studio_execution_routing.select_execution_route(request, catalog=catalog)
        if (
            result.status == studio_execution_routing.STATUS_AMBIGUOUS
            and result.decision_request
        ):
            from studio_decision_provider import resolve_decision
            decision = resolve_decision(result.decision_request)
            chosen = None
            if decision.passed and decision.selected_candidate_id and catalog is not None:
                for option in getattr(catalog, "options", ()):
                    if option.option_id == decision.selected_candidate_id:
                        chosen = option
                        break
            if chosen is not None:
                result = studio_execution_routing._selected_result(
                    chosen,
                    reason=f"bounded decision selected {chosen.option_id}",
                    eliminated=list(result.eliminated),
                )
    except studio_execution_routing.ExecutionRoutingError as exc:
        refused = studio_execution_routing.RoutingResult(
            passed=False,
            status=studio_execution_routing.STATUS_GAP,
            selected_provider=None,
            selected_runtime=None,
            selected_model=None,
            selected_effort=None,
            selected_option_id=None,
            selected_capabilities=[],
            routing_reason=str(exc),
            notes=["Malformed routing request; fail closed."],
        )
        return None, None, refused
    if result.status == studio_execution_routing.STATUS_SELECTED and result.passed:
        return result.selected_model, result.selected_effort, result
    return None, None, result


class FactoryRunEngine:
    """Owns phase sequencing, bounded retries, gates, and the run ledger."""

    def __init__(
        self,
        *,
        task_id: str,
        cwd: Path | str,
        max_repairs: int = DEFAULT_MAX_REPAIRS,
        agent_producers: Mapping[str, AgentProducer] | None = None,
        provider_bindings: Mapping[str, "ProviderBinding"] | None = None,
        observability_ledger: RuntimeObservabilityLedger | None = None,
        execution_catalog: studio_execution_routing.ExecutionCatalog | None = None,
    ) -> None:
        self.task_id = task_id
        self.cwd = Path(cwd)
        self.max_repairs = max_repairs
        self.agent_producers = dict(agent_producers or {})
        self.provider_bindings = dict(provider_bindings or {})
        self.observability_ledger = observability_ledger
        self.execution_catalog = execution_catalog
        self.envelopes: list[EvidenceEnvelope] = []
        self.ledger: list[LedgerEntry] = []
        self.verification_fingerprint: str | None = None
        self.last_content_fingerprint: str | None = None
        self.verification_phase_ids: list[str] = []
        self._relevant_artifact_paths: list[str] = []

    def _record_ledger(self, entry: LedgerEntry) -> None:
        """Append to the factory ledger and optionally bridge into observability.

        Telemetry is never the source of truth — the factory ledger remains
        authoritative for phase outcomes; observability stores refs only.
        """
        self.ledger.append(entry)
        if self.observability_ledger is None:
            return
        ingest_factory_ledger_entry(
            self.observability_ledger,
            task_id=self.task_id,
            entry=entry.as_dict(),
            evidence_references=[
                f"factory-ledger:{entry.phase_id}:{entry.event}"
            ],
        )

    def run_phases(
        self,
        phases: Sequence[PhaseSpec],
        *,
        mark_verification: bool = False,
    ) -> FactoryRunResult:
        phase_list = list(phases)
        if mark_verification and not phase_list:
            raise FactoryRunError(
                "DONE fails closed: empty VERIFYING phase list cannot satisfy completion "
                "when verification is required"
            )
        result = FactoryRunResult()
        for phase in phase_list:
            envelope = self._run_one_phase(phase)
            result.envelopes.append(envelope)
            self.envelopes.append(envelope)
            if mark_verification:
                self.verification_phase_ids.append(phase.phase_id)
            # Collect artifact paths for fingerprint relevance.
            if phase.artifact_paths:
                self._relevant_artifact_paths.extend(str(p) for p in phase.artifact_paths)
            for art in envelope.artifacts:
                if isinstance(art, Mapping) and art.get("path"):
                    self._relevant_artifact_paths.append(str(art["path"]))
            if envelope.status not in {STATUS_PASSED}:
                if envelope.status == STATUS_NEEDS_REPAIR:
                    result.unresolvable = {
                        "reason": (
                            f"repair bound exceeded for phase {phase.phase_id!r} "
                            f"(max_repairs={self.max_repairs})"
                        ),
                        "phase_id": phase.phase_id,
                        "producer": phase.producer,
                        "interruption_class": "UNRESOLVABLE_BLOCKER",
                    }
                    self._record_ledger(LedgerEntry(
                        phase_id=phase.phase_id,
                        event="phase_end",
                        producer_kind=phase.phase_kind,
                        retry_count=envelope.retry_count,
                        interruptions=[result.unresolvable],
                    ))
                    result.ledger = list(self.ledger)
                    result.all_passed = False
                    return result
                result.unresolvable = {
                    "reason": (
                        f"phase {phase.phase_id!r} ended with status "
                        f"{envelope.status}"
                    ),
                    "phase_id": phase.phase_id,
                    "producer": phase.producer,
                    "interruption_class": "UNRESOLVABLE_BLOCKER",
                }
                result.ledger = list(self.ledger)
                result.all_passed = False
                return result

        fp = capture_content_fingerprint(
            self.cwd,
            artifact_paths=list(dict.fromkeys(self._relevant_artifact_paths)),
        )
        self.last_content_fingerprint = fp
        result.content_fingerprint = fp
        if mark_verification:
            if not self.verification_phase_ids:
                raise FactoryRunError(
                    "DONE fails closed: VERIFYING produced no verification phases"
                )
            self.verification_fingerprint = fp
            result.verification_fingerprint = fp
        result.ledger = list(self.ledger)
        result.all_passed = True
        return result

    def verification_is_fresh(self) -> bool:
        if self.verification_fingerprint is None:
            return False
        current = capture_content_fingerprint(
            self.cwd,
            artifact_paths=list(dict.fromkeys(self._relevant_artifact_paths)),
        )
        self.last_content_fingerprint = current
        return current == self.verification_fingerprint

    def _run_one_phase(self, phase: PhaseSpec) -> EvidenceEnvelope:
        if phase.phase_kind not in PHASE_KINDS:
            raise FactoryRunError(
                f"unknown phase_kind {phase.phase_kind!r}; "
                f"allowed: {sorted(PHASE_KINDS)}"
            )
        self._record_ledger(LedgerEntry(
            phase_id=phase.phase_id,
            event="phase_start",
            producer_kind=phase.phase_kind,
        ))
        t0 = time.monotonic()

        if phase.phase_kind == PHASE_KIND_CODE:
            envelope = self._run_code_phase(phase, retry_count=0)
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record_ledger(LedgerEntry(
                phase_id=phase.phase_id,
                event="phase_end",
                producer_kind=PHASE_KIND_CODE,
                duration_ms=duration_ms,
                gate_result="pass" if envelope.status == STATUS_PASSED else "fail",
                retry_count=envelope.retry_count,
                artifacts=[
                    a.get("path", a.get("kind", ""))
                    for a in envelope.artifacts
                    if isinstance(a, dict)
                ],
            ))
            return envelope

        # Verification adapters are not AgentProducers and never enter the
        # AGENT/TOOL repair loop (GAP/REFUSED must not become NEEDS_REPAIR).
        if phase.producer in VERIFICATION_ADAPTER_PRODUCERS:
            return self._run_verification_adapter_phase(phase, t0=t0)

        return self._run_agent_or_tool_with_repair(phase, t0=t0)

    def _run_verification_adapter_phase(
        self,
        phase: PhaseSpec,
        *,
        t0: float,
    ) -> EvidenceEnvelope:
        adapter_fn = _ADAPTER_CALLABLES.get(phase.producer)
        if adapter_fn is None:
            raise FactoryRunError(
                f"no verification adapter registered for producer={phase.producer!r}"
            )
        kwargs = dict(phase.adapter_input or {})
        kwargs["task_id"] = self.task_id
        kwargs["work_order_id"] = phase.work_order_id
        kwargs["phase_id"] = phase.phase_id
        if "provider_present" not in kwargs:
            raise FactoryRunError(
                f"verification adapter phase {phase.phase_id!r} requires "
                "adapter_input['provider_present']"
            )
        try:
            adapter_env = adapter_fn(**kwargs)
        except TypeError as exc:
            raise FactoryRunError(
                f"verification adapter {phase.producer!r} rejected input: {exc}"
            ) from exc
        except _verification_adapters.VerificationAdapterError as exc:
            raise FactoryRunError(str(exc)) from exc

        envelope = bridge_verification_adapter_envelope(adapter_env)
        # Invariant: adapters never promote into repair vocabulary.
        if envelope.status == STATUS_NEEDS_REPAIR:
            raise FactoryRunError(
                "verification adapter bridge produced NEEDS_REPAIR (forbidden)"
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        gate_result = "pass" if envelope.status == STATUS_PASSED else "fail"
        self._record_ledger(LedgerEntry(
            phase_id=phase.phase_id,
            event="phase_end",
            producer_kind=envelope.phase_kind,
            provider=(envelope.provenance or {}).get("provider")
            or (envelope.provenance or {}).get("tool"),
            duration_ms=duration_ms,
            gate_result=gate_result,
            retry_count=0,
            artifacts=[
                a.get("path", a.get("kind", ""))
                for a in envelope.artifacts
                if isinstance(a, dict)
            ],
        ))
        return envelope

    def _run_code_phase(self, phase: PhaseSpec, *, retry_count: int) -> EvidenceEnvelope:
        if phase.operation not in CODE_OPERATIONS:
            raise FactoryRunError(
                f"CODE phase {phase.phase_id!r} requires operation in "
                f"{sorted(CODE_OPERATIONS)}, got {phase.operation!r}"
            )
        if phase.operation == OP_COMMAND:
            return produce_command(
                task_id=self.task_id, phase=phase, cwd=self.cwd, retry_count=retry_count,
            )
        if phase.operation == OP_GIT_DIFF:
            return produce_git_diff(
                task_id=self.task_id, phase=phase, cwd=self.cwd, retry_count=retry_count,
            )
        return produce_artifact_check(
            task_id=self.task_id, phase=phase, cwd=self.cwd, retry_count=retry_count,
        )

    def _run_agent_or_tool_with_repair(
        self,
        phase: PhaseSpec,
        *,
        t0: float,
    ) -> EvidenceEnvelope:
        binding = self.provider_bindings.get(phase.producer)
        producer_fn = self.agent_producers.get(phase.producer)
        if binding is None and producer_fn is None:
            raise FactoryRunError(
                f"no provider binding or producer registered for {phase.phase_kind} "
                f"producer={phase.producer!r}"
            )

        prior: EvidenceEnvelope | None = None
        repair_ctx: Mapping[str, Any] | None = None
        retry = 0

        selected_model, selected_effort, routing_result = _resolve_phase_routing(
            phase, catalog=self.execution_catalog
        )
        if routing_result is not None and not (
            routing_result.status == studio_execution_routing.STATUS_SELECTED
            and routing_result.passed
        ):
            factory_status = _routing_status_to_factory(routing_result.status)
            started = _utc_now()
            ended = _utc_now()
            envelope = EvidenceEnvelope(
                task_id=self.task_id,
                work_order_id=phase.work_order_id,
                phase_id=phase.phase_id,
                phase_kind=phase.phase_kind,
                producer=phase.producer,
                status=factory_status,
                claims=[{
                    "statement": routing_result.routing_reason,
                    "passed": False,
                }],
                artifacts=[],
                checks=[{
                    "name": "execution_routing",
                    "passed": False,
                    "detail": routing_result.status,
                }],
                provenance={
                    "routing": routing_result.as_dict(),
                    "provider": routing_result.selected_provider,
                    "runtime": routing_result.selected_runtime,
                    "model": routing_result.selected_model,
                    "effort": routing_result.selected_effort,
                },
                timestamps={"started_at": started, "ended_at": ended},
                notes_for_next_phase=[
                    "execution routing did not SELECT; not remapped to NEEDS_REPAIR"
                ],
                session_continuation=None,
                repair_target=None,
                retry_count=0,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record_ledger(LedgerEntry(
                phase_id=phase.phase_id,
                event="phase_end",
                producer_kind=phase.phase_kind,
                provider=routing_result.selected_provider,
                model=routing_result.selected_model,
                effort=routing_result.selected_effort,
                duration_ms=duration_ms,
                gate_result="fail",
                retry_count=0,
            ))
            return envelope

        if (
            binding is None
            and routing_result is not None
            and routing_result.selected_provider in {"claude-code", "cursor-agent"}
        ):
            from studio_live_provider_runtime import build_live_provider_binding
            binding = build_live_provider_binding(routing_result.selected_provider)
        while True:
            started = _utc_now()
            before_index = _verification_adapters.capture_worktree_file_index(self.cwd)
            if binding is not None:
                from studio_factory_provider_binding import BindingRequest

                session_id = None
                if repair_ctx and repair_ctx.get("session_id"):
                    session_id = str(repair_ctx["session_id"])
                elif prior is not None and prior.provenance.get("session"):
                    session_id = str(prior.provenance["session"])
                exec_result = binding.execute(
                    BindingRequest(
                        phase=phase,
                        cwd=self.cwd,
                        session_id=session_id,
                        repair_context=repair_ctx,
                        prior_envelope=prior,
                        selected_model=selected_model,
                        selected_effort=selected_effort,
                    )
                )
                payload = _claim_payload_from_binding(exec_result)
            else:
                assert producer_fn is not None
                payload = producer_fn(phase, prior, repair_ctx)
            ended = _utc_now()
            provisional = _build_agent_envelope(
                task_id=self.task_id,
                phase=phase,
                payload=payload,
                retry_count=retry,
                started=started,
                ended=ended,
            )

            # Provider FAILED / is_error cannot become PASSED. GAP/REFUSED stay
            # non-pass and must never enter the NEEDS_REPAIR repair loop.
            provider_status = payload.provider_status
            if payload.is_error and provider_status not in {
                STATUS_GAP, STATUS_REFUSED, STATUS_FAILED,
            }:
                provider_status = STATUS_FAILED
            if provider_status in {STATUS_FAILED, STATUS_GAP, STATUS_REFUSED} or payload.is_error:
                fail_status = provider_status if provider_status in {
                    STATUS_FAILED, STATUS_GAP, STATUS_REFUSED,
                } else STATUS_FAILED
                failed = EvidenceEnvelope(
                    task_id=provisional.task_id,
                    work_order_id=provisional.work_order_id,
                    phase_id=provisional.phase_id,
                    phase_kind=provisional.phase_kind,
                    producer=provisional.producer,
                    status=fail_status,
                    claims=list(provisional.claims),
                    artifacts=list(provisional.artifacts),
                    checks=list(provisional.checks) + [{
                        "name": "provider_status",
                        "passed": False,
                        "provider_status": fail_status,
                        "is_error": bool(payload.is_error),
                    }],
                    provenance=dict(provisional.provenance),
                    timestamps=dict(provisional.timestamps),
                    notes_for_next_phase=list(provisional.notes_for_next_phase) + [
                        f"provider status {fail_status}; not remapped to NEEDS_REPAIR"
                    ],
                    session_continuation=None,
                    repair_target=None,
                    retry_count=retry,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                self._record_ledger(LedgerEntry(
                    phase_id=phase.phase_id,
                    event="phase_end",
                    producer_kind=phase.phase_kind,
                    provider=payload.provider,
                    model=payload.model,
                    effort=payload.effort,
                    duration_ms=duration_ms,
                    gate_result="fail",
                    retry_count=retry,
                ))
                return failed

            gated = gate_agent_or_tool_envelope(
                provisional, cwd=self.cwd, before_file_index=before_index,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)

            if gated.status == STATUS_PASSED:
                self._record_ledger(LedgerEntry(
                    phase_id=phase.phase_id,
                    event="gate_pass",
                    producer_kind=phase.phase_kind,
                    provider=payload.provider,
                    model=payload.model,
                    effort=payload.effort,
                    duration_ms=duration_ms,
                    gate_result="pass",
                    retry_count=retry,
                    artifacts=[a.get("path", "") for a in gated.artifacts if a.get("path")],
                ))
                self._record_ledger(LedgerEntry(
                    phase_id=phase.phase_id,
                    event="phase_end",
                    producer_kind=phase.phase_kind,
                    provider=payload.provider,
                    model=payload.model,
                    effort=payload.effort,
                    duration_ms=duration_ms,
                    gate_result="pass",
                    retry_count=retry,
                ))
                return gated

            self._record_ledger(LedgerEntry(
                phase_id=phase.phase_id,
                event="gate_fail",
                producer_kind=phase.phase_kind,
                provider=payload.provider,
                model=payload.model,
                effort=payload.effort,
                duration_ms=duration_ms,
                gate_result="fail",
                retry_count=retry,
            ))

            if retry >= self.max_repairs:
                gated.status = STATUS_NEEDS_REPAIR
                gated.session_continuation = _session_continuation_record(
                    payload=payload,
                    phase=phase,
                    retries_exhausted=True,
                    max_repairs=self.max_repairs,
                    prior=prior,
                )
                gated.repair_target = phase.phase_id
                self._record_ledger(LedgerEntry(
                    phase_id=phase.phase_id,
                    event="phase_end",
                    producer_kind=phase.phase_kind,
                    retry_count=retry,
                    gate_result="fail",
                    interruptions=[{
                        "interruption_class": "UNRESOLVABLE_BLOCKER",
                        "reason": "repair bound exceeded",
                    }],
                ))
                return gated

            retry += 1
            repair_ctx = _repair_context_from_envelope(gated)
            gated.session_continuation = _session_continuation_record(
                payload=payload,
                phase=phase,
                retry_count=retry,
                prior=prior,
            )
            # Prefer the stable session id from the payload/prior for the next turn.
            if not gated.session_continuation.get("session_id") and repair_ctx.get("session_id"):
                gated.session_continuation["session_id"] = repair_ctx["session_id"]
            gated.repair_target = phase.phase_id
            prior = gated
            self._record_ledger(LedgerEntry(
                phase_id=phase.phase_id,
                event="repair",
                producer_kind=phase.phase_kind,
                provider=payload.provider,
                model=payload.model,
                effort=payload.effort,
                retry_count=retry,
                artifacts=[a.get("path", "") for a in gated.artifacts if a.get("path")],
            ))



def envelopes_have_provenance(envelopes: Sequence[EvidenceEnvelope | Mapping[str, Any]]) -> bool:
    """True when every envelope carries non-empty provenance from a producer."""
    if not envelopes:
        return False
    for env in envelopes:
        prov = env.provenance if isinstance(env, EvidenceEnvelope) else env.get("provenance")
        if not isinstance(prov, Mapping) or not prov:
            return False
        if not any(prov.get(k) for k in ("command", "tool", "provider", "runtime", "session")):
            return False
    return True


def boolean_only_done_attempt(evidence: Mapping[str, Any]) -> bool:
    """Detect legacy self-asserted boolean DONE payloads without factory envelopes."""
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("factory_envelopes") or evidence.get("factory_run"):
        return False
    bool_keys = {
        "acceptance_criteria_satisfied",
        "tests_checks_passed",
        "no_hidden_unresolved_blocker",
    }
    return bool_keys.issubset(evidence.keys())


def build_done_evidence_from_factory(
    *,
    engine: FactoryRunEngine,
    critic_required: bool,
    supplemental: Mapping[str, Any] | None = None,
    operator_gate_required: bool | None = None,
    prepared_gate_action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble DONE evidence from runner envelopes. Fail closed if incomplete/stale.

    Critic and Operator gate authority must be provenance-bearing evidence — never
    caller-supplied ``critic_verdict="PASS"`` strings alone. Gated work requires
    ``MANDATORY_OPERATOR_GATE`` evidence bound to the exact ``prepared_gate_action``.
    """
    supplemental = dict(supplemental or {})

    if not engine.envelopes:
        raise FactoryRunError(
            "DONE fails closed: no factory-run envelopes; boolean-only self-report rejected"
        )
    if not envelopes_have_provenance(engine.envelopes):
        raise FactoryRunError(
            "DONE fails closed: envelopes lack provenance-bearing producer evidence"
        )
    if not all(e.status == STATUS_PASSED for e in engine.envelopes):
        failed = [e.phase_id for e in engine.envelopes if e.status != STATUS_PASSED]
        raise FactoryRunError(
            f"DONE fails closed: non-passed phases remain: {failed}"
        )
    if engine.verification_fingerprint is None or not engine.verification_phase_ids:
        raise FactoryRunError(
            "DONE fails closed: verification fingerprint missing or VERIFYING phases empty "
            "(VERIFYING must run mark_verification=True with at least one phase)"
        )
    if not engine.verification_is_fresh():
        raise FactoryRunError(
            "DONE fails closed: verification evidence is stale; "
            "content fingerprint changed after verification — re-verify required"
        )

    code_checks = [
        c for e in engine.envelopes if e.phase_kind == PHASE_KIND_CODE for c in e.checks
    ]
    if not code_checks or not all(c.get("passed") for c in code_checks):
        raise FactoryRunError(
            "DONE fails closed: deterministic CODE checks did not all pass"
        )

    residual = supplemental.get("residual_uncertainty", [])
    if not isinstance(residual, list):
        raise FactoryRunError("residual_uncertainty must be a list")

    art = supplemental.get("artifact_runtime_verified", "not_applicable")
    if art is not True and art != "not_applicable":
        raise FactoryRunError(
            "artifact_runtime_verified must be True or 'not_applicable'"
        )

    regression = supplemental.get(
        "regression_risk_evidence",
        "deterministic factory checks passed; see factory_envelopes provenance",
    )
    if not regression:
        raise FactoryRunError("regression_risk_evidence must be non-empty")

    # --- Critic: reject bare caller-supplied strings; require provenance ---
    if "critic_verdict" in supplemental and "critic_evidence" not in supplemental:
        raise FactoryRunError(
            "DONE fails closed: caller-supplied critic_verdict strings are rejected; "
            "provenance-bearing critic_evidence is required"
        )

    critic_verdict = None
    critic_not_required = False
    critic_evidence_record = None

    if critic_required:
        try:
            critic_evidence_record = _verification_adapters.validate_critic_evidence(
                supplemental.get("critic_evidence"),
                task_id=engine.task_id,
                content_fingerprint=engine.verification_fingerprint,
                work_order_id=None,
            )
        except _verification_adapters.VerificationAdapterError as exc:
            raise FactoryRunError(str(exc)) from exc
        critic_verdict = critic_evidence_record["result"]
    else:
        if "critic_evidence" in supplemental:
            try:
                critic_evidence_record = _verification_adapters.validate_critic_evidence(
                    supplemental.get("critic_evidence"),
                    task_id=engine.task_id,
                    content_fingerprint=engine.verification_fingerprint,
                    work_order_id=None,
                )
            except _verification_adapters.VerificationAdapterError as exc:
                raise FactoryRunError(str(exc)) from exc
            critic_verdict = critic_evidence_record["result"]
            critic_not_required = False
        elif supplemental.get("critic_not_required") is True:
            critic_not_required = True
        else:
            # Preserve prior convenience path only when Critic is not required and
            # caller explicitly opts out; never accept a bare verdict string.
            raise FactoryRunError(
                "DONE fails closed: when Critic is not required, set "
                "critic_not_required=True (or supply provenance-bearing critic_evidence)"
            )

    # --- Operator gate ---
    gate_required = bool(operator_gate_required) if operator_gate_required is not None else False
    if supplemental.get("operator_gate_required") is True:
        gate_required = True
    # When autonomous conflates critic_required with operator_gate_required,
    # callers may also pass operator_gate_required via kwargs.
    gate_action = prepared_gate_action
    if gate_action is None and isinstance(supplemental.get("prepared_gate_action"), Mapping):
        gate_action = supplemental.get("prepared_gate_action")
    gate_evidence = (
        supplemental.get("operator_authorization")
        or supplemental.get("mandatory_operator_gate_evidence")
        or supplemental.get("operator_gate_evidence")
    )
    try:
        operator_gate_record = _verification_adapters.validate_operator_gate_evidence(
            gate_evidence,
            prepared_gate_action=gate_action,
            operator_gate_required=gate_required,
        )
    except _verification_adapters.VerificationAdapterError as exc:
        raise FactoryRunError(str(exc)) from exc

    return {
        "acceptance_criteria_satisfied": True,
        "tests_checks_passed": True,
        "artifact_runtime_verified": art,
        "regression_risk_evidence": regression,
        "no_hidden_unresolved_blocker": True,
        "residual_uncertainty": list(residual),
        "critic_required": critic_required,
        "critic_not_required": critic_not_required,
        "critic_verdict": critic_verdict,
        "critic_evidence": critic_evidence_record,
        "operator_gate_required": gate_required,
        "operator_gate_evidence": operator_gate_record,
        "factory_envelopes": [e.as_dict() for e in engine.envelopes],
        "factory_ledger": [e.as_dict() for e in engine.ledger],
        "verification_fingerprint": engine.verification_fingerprint,
        "content_fingerprint": engine.last_content_fingerprint,
        "verification_phase_ids": list(engine.verification_phase_ids),
        "provenance_bearing": True,
    }
