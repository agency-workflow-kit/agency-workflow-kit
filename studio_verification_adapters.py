#!/usr/bin/env python3
"""Provider-neutral VERIFYING adapter contracts.

Optional adapters that shape typed evidence envelopes for:
  - blast-radius (pstack-style impact/regression probe)
  - Canary browser runtime verification
  - interrogate-style adversarial findings under Critic

This module is NOT a second planner, Critic, or task database. It does not
install providers, spawn browsers, or emit Critic verdicts. Missing providers
are explicit gaps — never automatic passes. Factory VERIFYING imports this
module and bridges envelopes via studio_factory_run (INTEGRATION complete for
the shared wiring slice); adapters remain non-AgentProducer callables.

Standing orders honored here:
  - no wholesale pstack install
  - Canary sensitive/authenticated flows default har_enabled=false
  - HAR requires explicit enable AND a redaction note
  - interrogate never replaces Critic and must not emit critic_verdict
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


# Phase kinds — same literals as studio_factory_run.EvidenceEnvelope.
PHASE_KIND_CODE = "CODE"
PHASE_KIND_AGENT = "AGENT"
PHASE_KIND_TOOL = "TOOL"
PHASE_KINDS = frozenset({PHASE_KIND_CODE, PHASE_KIND_AGENT, PHASE_KIND_TOOL})

# Envelope status vocabulary (adapter-local; aligns with factory PASSED/FAILED).
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_GAP = "GAP"
STATUS_REFUSED = "REFUSED"
ENVELOPE_STATUSES = frozenset({
    STATUS_PASSED,
    STATUS_FAILED,
    STATUS_GAP,
    STATUS_REFUSED,
})

# Producer ids (provider-neutral contract names; not capability catalog entries).
PRODUCER_BLAST_RADIUS = "pstack-blast-radius"
PRODUCER_CANARY = "gstack-canary"
PRODUCER_INTERROGATE = "interrogate-adversarial"

# Critic verdict literals that adapters must never emit as authority.
CRITIC_VERDICT_VALUES = frozenset({
    "PASS",
    "FAIL",
    "BLOCKED_INCONCLUSIVE",
    "APPROVE",
})

AUTH_MODES_SENSITIVE = frozenset({
    "authenticated",
    "sensitive",
    "cookie-import",
    "handoff",
})

# Operator-gate evidence class — same literal as autonomous execution.
MANDATORY_OPERATOR_GATE = "MANDATORY_OPERATOR_GATE"

# Accepted Critic results inside provenance-bearing critic_evidence.
ACCEPTED_CRITIC_RESULTS = frozenset({"PASS", "APPROVE"})


class VerificationAdapterError(RuntimeError):
    """Fail-closed adapter contract violation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationAdapterError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_phase_kind(phase_kind: str) -> str:
    kind = _require_nonempty_str(phase_kind, "phase_kind")
    if kind not in PHASE_KINDS:
        raise VerificationAdapterError(
            f"phase_kind must be one of {sorted(PHASE_KINDS)}, got {phase_kind!r}"
        )
    return kind


@dataclass
class EvidenceEnvelope:
    """Typed evidence record — field-compatible with factory EvidenceEnvelope."""

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
        }


def build_envelope(
    *,
    task_id: str,
    work_order_id: str | None,
    phase_id: str,
    phase_kind: str,
    producer: str,
    status: str,
    claims: Sequence[Mapping[str, Any]] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    checks: Sequence[Mapping[str, Any]] | None = None,
    provenance: Mapping[str, Any] | None = None,
    notes_for_next_phase: Sequence[str] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> EvidenceEnvelope:
    """Construct a typed envelope. Never invents a PASS from missing evidence."""
    status_text = _require_nonempty_str(status, "status")
    if status_text not in ENVELOPE_STATUSES:
        raise VerificationAdapterError(
            f"status must be one of {sorted(ENVELOPE_STATUSES)}, got {status!r}"
        )
    started = started_at or _utc_now()
    ended = ended_at or started
    return EvidenceEnvelope(
        task_id=_require_nonempty_str(task_id, "task_id"),
        work_order_id=work_order_id,
        phase_id=_require_nonempty_str(phase_id, "phase_id"),
        phase_kind=_require_phase_kind(phase_kind),
        producer=_require_nonempty_str(producer, "producer"),
        status=status_text,
        claims=[dict(c) for c in (claims or ())],
        artifacts=[dict(a) for a in (artifacts or ())],
        checks=[dict(c) for c in (checks or ())],
        provenance=dict(provenance or {}),
        timestamps={"started_at": started, "ended_at": ended},
        notes_for_next_phase=[str(n) for n in (notes_for_next_phase or ())],
    )


def _identity_fields(
    *,
    task_id: str,
    work_order_id: str | None,
    phase_id: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "work_order_id": work_order_id,
        "phase_id": phase_id,
    }


# ---------------------------------------------------------------------------
# 1) blast-radius
# ---------------------------------------------------------------------------


def blast_radius_adapter(
    *,
    task_id: str,
    work_order_id: str | None,
    phase_id: str,
    provider_present: bool,
    wholesale_pstack_install_requested: bool = False,
    proof_command: Sequence[str] | None = None,
    proof_exit_code: int | None = None,
    proof_stdout_path: str | None = None,
    safety_fact: str | None = None,
    proven: bool | None = None,
    risks: Sequence[str] | None = None,
    cleared: Sequence[str] | None = None,
    risk_judgment: Mapping[str, Any] | None = None,
    provider: str | None = None,
    runtime: str | None = None,
    session: str | None = None,
    cwd: str | None = None,
) -> EvidenceEnvelope:
    """Shape blast-radius VERIFYING evidence.

    Deterministic proof commands are CODE-shaped checks. Risk judgment is
    AGENT/TOOL evidence. Unproven is never PASSED. Wholesale pstack install
    is refused. A missing provider is an explicit GAP.
    """
    identity = _identity_fields(
        task_id=task_id, work_order_id=work_order_id, phase_id=phase_id
    )
    notes: list[str] = []

    if wholesale_pstack_install_requested:
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_TOOL,
            producer=PRODUCER_BLAST_RADIUS,
            status=STATUS_REFUSED,
            claims=[{
                "claim_id": "wholesale_pstack_install",
                "refused": True,
                "reason": "no wholesale pstack install",
            }],
            checks=[{
                "check_id": "wholesale_install_policy",
                "passed": False,
                "detail": "wholesale pstack install is not allowed",
            }],
            provenance={
                "tool": PRODUCER_BLAST_RADIUS,
                "provider": provider or "pstack",
                "policy": "no_wholesale_pstack_install",
            },
            notes_for_next_phase=[
                "Invoke blast-radius only when Operator-local provider is PRESENT; "
                "do not install the whole pstack suite into the kit."
            ],
        )

    if not provider_present:
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_TOOL,
            producer=PRODUCER_BLAST_RADIUS,
            status=STATUS_GAP,
            claims=[{
                "claim_id": "provider_availability",
                "provider_present": False,
                "proven": False,
            }],
            checks=[{
                "check_id": "provider_present",
                "passed": False,
                "detail": "blast-radius provider absent; explicit gap, not a pass",
            }],
            provenance={
                "tool": PRODUCER_BLAST_RADIUS,
                "provider": provider or "pstack",
                "explicit_gap": (
                    "blast-radius provider not present; residual uncertainty remains"
                ),
            },
            notes_for_next_phase=[
                "Missing blast-radius provider is an explicit gap — never treat as PASS."
            ],
        )

    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    provenance: dict[str, Any] = {
        "tool": PRODUCER_BLAST_RADIUS,
        "provider": provider or "pstack",
    }
    if runtime:
        provenance["runtime"] = runtime
    if session:
        provenance["session"] = session
    if cwd:
        provenance["cwd"] = cwd

    # Deterministic proof → CODE-shaped check (may sit inside a TOOL phase
    # when the external provider ran the command; the check itself is CODE).
    code_proof_ok: bool | None = None
    if proof_command is not None:
        if proof_exit_code is None:
            raise VerificationAdapterError(
                "proof_exit_code is required when proof_command is supplied"
            )
        cmd = [str(x) for x in proof_command]
        if not cmd:
            raise VerificationAdapterError("proof_command must be non-empty")
        code_proof_ok = int(proof_exit_code) == 0
        provenance["command"] = cmd
        provenance["exit_code"] = int(proof_exit_code)
        checks.append({
            "check_id": "proof_command",
            "phase_kind_shape": PHASE_KIND_CODE,
            "passed": code_proof_ok,
            "command": cmd,
            "exit_code": int(proof_exit_code),
        })
        if proof_stdout_path:
            artifacts.append({
                "kind": "proof_stdout",
                "path": proof_stdout_path,
            })

    is_proven = bool(proven) if proven is not None else False
    if code_proof_ok is True and proven is None:
        # A successful deterministic proof may establish "proven" only when
        # the caller did not explicitly mark unproven.
        is_proven = True
    if code_proof_ok is False:
        is_proven = False

    claim: dict[str, Any] = {
        "claim_id": "blast_radius_safety_fact",
        "safety_fact": safety_fact,
        "proven": is_proven,
        "proof_level": (
            "ran" if code_proof_ok else ("asserted" if safety_fact else "unproven")
        ),
    }
    if risks is not None:
        claim["risks"] = list(risks)
    if cleared is not None:
        claim["cleared"] = list(cleared)
    claims.append(claim)

    if risk_judgment is not None:
        # Risk judgment is AGENT/TOOL evidence — never alone a PASS.
        claims.append({
            "claim_id": "risk_judgment",
            "phase_kind_shape": PHASE_KIND_AGENT,
            "judgment": dict(risk_judgment),
        })
        notes.append(
            "Risk judgment is AGENT/TOOL evidence; Critic still owns ship verdict."
        )

    if not is_proven:
        notes.append("Unproven blast-radius result is not PASS.")
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_TOOL,
            producer=PRODUCER_BLAST_RADIUS,
            status=STATUS_FAILED if code_proof_ok is False else STATUS_GAP,
            claims=claims,
            artifacts=artifacts,
            checks=checks or [{
                "check_id": "proven_safety_fact",
                "passed": False,
                "detail": "unproven is not PASS",
            }],
            provenance=provenance,
            notes_for_next_phase=notes,
        )

    # Proven only when a safety fact exists AND proof succeeded / proven=True.
    if not safety_fact:
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_TOOL,
            producer=PRODUCER_BLAST_RADIUS,
            status=STATUS_GAP,
            claims=claims,
            artifacts=artifacts,
            checks=checks + [{
                "check_id": "safety_fact_present",
                "passed": False,
                "detail": "proven flag without safety_fact is incomplete",
            }],
            provenance=provenance,
            notes_for_next_phase=[
                "Provide one concrete safety_fact before treating as proven."
            ],
        )

    return build_envelope(
        **identity,
        phase_kind=PHASE_KIND_TOOL,
        producer=PRODUCER_BLAST_RADIUS,
        status=STATUS_PASSED,
        claims=claims,
        artifacts=artifacts,
        checks=checks or [{
            "check_id": "proven_safety_fact",
            "passed": True,
        }],
        provenance=provenance,
        notes_for_next_phase=notes or [
            "Cite this envelope from regression_risk_evidence; Critic still decides."
        ],
    )


# ---------------------------------------------------------------------------
# 2) Canary browser verification
# ---------------------------------------------------------------------------


def canary_browser_adapter(
    *,
    task_id: str,
    work_order_id: str | None,
    phase_id: str,
    provider_present: bool,
    auth_mode: str = "public",
    har_enabled: bool | None = None,
    redaction_note: str | None = None,
    base_url: str | None = None,
    page_results: Sequence[Mapping[str, Any]] | None = None,
    screenshot_paths: Sequence[str] | None = None,
    report_path: str | None = None,
    har_path: str | None = None,
    provider: str | None = None,
    runtime: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    session: str | None = None,
) -> EvidenceEnvelope:
    """Shape optional Canary browser VERIFYING evidence.

    Sensitive/authenticated flows default har_enabled=false. HAR is refused
    unless the caller explicitly enables it AND supplies a redaction note.
    Missing provider is an explicit GAP, never an automated pass.
    """
    identity = _identity_fields(
        task_id=task_id, work_order_id=work_order_id, phase_id=phase_id
    )
    mode = (auth_mode or "public").strip().lower()
    sensitive = mode in AUTH_MODES_SENSITIVE

    # Default HAR off for sensitive/authenticated; public may still default off.
    if har_enabled is None:
        effective_har = False
    else:
        effective_har = bool(har_enabled)

    notes: list[str] = []
    if sensitive and har_enabled is None:
        notes.append(
            "Sensitive/authenticated Canary flow defaults har_enabled=false."
        )

    # Refuse HAR without explicit enable + redaction note.
    wants_har_artifact = bool(har_path) or effective_har
    if wants_har_artifact:
        if not effective_har:
            return build_envelope(
                **identity,
                phase_kind=PHASE_KIND_TOOL,
                producer=PRODUCER_CANARY,
                status=STATUS_REFUSED,
                claims=[{
                    "claim_id": "har_policy",
                    "har_enabled": False,
                    "refused": True,
                    "reason": "HAR requested while har_enabled is false",
                }],
                checks=[{
                    "check_id": "har_policy",
                    "passed": False,
                    "detail": "HAR artifact refused when har_enabled is false",
                }],
                provenance={
                    "tool": PRODUCER_CANARY,
                    "provider": provider or PRODUCER_CANARY,
                    "har_enabled": False,
                    "auth_mode": mode,
                },
                notes_for_next_phase=[
                    "Enable har_enabled explicitly and supply redaction_note to capture HAR."
                ],
            )
        if not (isinstance(redaction_note, str) and redaction_note.strip()):
            return build_envelope(
                **identity,
                phase_kind=PHASE_KIND_TOOL,
                producer=PRODUCER_CANARY,
                status=STATUS_REFUSED,
                claims=[{
                    "claim_id": "har_policy",
                    "har_enabled": True,
                    "refused": True,
                    "reason": "HAR requires an explicit redaction note",
                }],
                checks=[{
                    "check_id": "har_redaction_note",
                    "passed": False,
                    "detail": "redaction_note required when har_enabled=true",
                }],
                provenance={
                    "tool": PRODUCER_CANARY,
                    "provider": provider or PRODUCER_CANARY,
                    "har_enabled": True,
                    "auth_mode": mode,
                    "redaction_note_present": False,
                },
                notes_for_next_phase=[
                    "Refuse HAR until a non-empty redaction note is supplied."
                ],
            )

    if not provider_present:
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_TOOL,
            producer=PRODUCER_CANARY,
            status=STATUS_GAP,
            claims=[{
                "claim_id": "provider_availability",
                "provider_present": False,
                "capability": "browser_runtime_verification",
            }],
            checks=[{
                "check_id": "provider_present",
                "passed": False,
                "detail": (
                    "Canary provider absent; manual check is not an automated pass"
                ),
            }],
            provenance={
                "tool": PRODUCER_CANARY,
                "provider": provider or PRODUCER_CANARY,
                "har_enabled": effective_har,
                "auth_mode": mode,
                "explicit_gap": (
                    "no automated browser runtime verification available; "
                    "manual check required, do not treat as equivalent to an "
                    "automated pass."
                ),
            },
            notes_for_next_phase=notes + [
                "Missing Canary provider is an explicit gap — never treat as PASS."
            ],
        )

    claims: list[dict[str, Any]] = []
    for page in page_results or ():
        claims.append(dict(page))

    artifacts: list[dict[str, Any]] = []
    for path in screenshot_paths or ():
        artifacts.append({"kind": "screenshot", "path": path})
    if report_path:
        artifacts.append({"kind": "report", "path": report_path})

    checks: list[dict[str, Any]] = []
    # HAR absent-by-default for sensitive/auth is a passing security check.
    if sensitive and not effective_har:
        checks.append({
            "check_id": "har_default_off_sensitive",
            "passed": True,
            "detail": "HAR off by default for sensitive/authenticated flow",
        })

    if effective_har and redaction_note and redaction_note.strip():
        if har_path:
            artifacts.append({
                "kind": "har",
                "path": har_path,
                "redaction_note": redaction_note.strip(),
            })
        checks.append({
            "check_id": "har_redaction_note",
            "passed": True,
            "detail": "HAR enabled with explicit redaction note",
        })

    has_runtime_artifacts = bool(screenshot_paths) or bool(report_path)
    if not has_runtime_artifacts:
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_TOOL,
            producer=PRODUCER_CANARY,
            status=STATUS_GAP,
            claims=claims or [{
                "claim_id": "runtime_artifacts",
                "present": False,
            }],
            artifacts=artifacts,
            checks=checks + [{
                "check_id": "runtime_artifacts",
                "passed": False,
                "detail": "provider present but no screenshots/report — not a pass",
            }],
            provenance={
                "tool": PRODUCER_CANARY,
                "provider": provider or PRODUCER_CANARY,
                "runtime": runtime,
                "model": model,
                "effort": effort,
                "session": session,
                "har_enabled": effective_har,
                "auth_mode": mode,
                "base_url": base_url,
            },
            notes_for_next_phase=notes + [
                "Presence alone is not verification; require real run artifacts."
            ],
        )

    failed_pages = [c for c in claims if c.get("passed") is False]
    status = STATUS_FAILED if failed_pages else STATUS_PASSED
    if status == STATUS_PASSED:
        checks.append({
            "check_id": "runtime_artifacts",
            "passed": True,
            "detail": "screenshots/report present from Canary run",
        })

    provenance: dict[str, Any] = {
        "tool": PRODUCER_CANARY,
        "provider": provider or PRODUCER_CANARY,
        "har_enabled": effective_har,
        "auth_mode": mode,
        "base_url": base_url,
    }
    if runtime:
        provenance["runtime"] = runtime
    if model:
        provenance["model"] = model
    if effort:
        provenance["effort"] = effort
    if session:
        provenance["session"] = session
    if effective_har and redaction_note:
        provenance["redaction_note"] = redaction_note.strip()

    return build_envelope(
        **identity,
        phase_kind=PHASE_KIND_TOOL,
        producer=PRODUCER_CANARY,
        status=status,
        claims=claims,
        artifacts=artifacts,
        checks=checks,
        provenance=provenance,
        notes_for_next_phase=notes + [
            "Maps to artifact_runtime_verified only when status is PASSED with artifacts."
        ],
    )


# ---------------------------------------------------------------------------
# 3) interrogate-style adversarial evidence under Critic
# ---------------------------------------------------------------------------


def interrogate_adversarial_adapter(
    *,
    task_id: str,
    work_order_id: str | None,
    phase_id: str,
    provider_present: bool,
    findings: Sequence[Mapping[str, Any]] | None = None,
    required_code_checks: Sequence[Mapping[str, Any]] | None = None,
    report_path: str | None = None,
    provider: str | None = None,
    models: Sequence[str] | None = None,
    runtime: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    session: str | None = None,
    critic_verdict: Any = None,
) -> EvidenceEnvelope:
    """Shape interrogate findings for Critic — never a Critic verdict.

    Emits findings-only AGENT evidence. Refuses any attempt to set
    critic_verdict PASS/FAIL/BLOCKED_INCONCLUSIVE (or APPROVE). Does not
    replace Critic. Missing provider is an explicit GAP.
    """
    identity = _identity_fields(
        task_id=task_id, work_order_id=work_order_id, phase_id=phase_id
    )

    if critic_verdict is not None:
        verdict_text = str(critic_verdict).strip().upper()
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_AGENT,
            producer=PRODUCER_INTERROGATE,
            status=STATUS_REFUSED,
            claims=[{
                "claim_id": "critic_verdict_forbidden",
                "refused": True,
                "attempted_verdict": verdict_text,
                "reason": (
                    "interrogate must not emit critic_verdict; Critic alone decides"
                ),
            }],
            checks=[{
                "check_id": "no_critic_verdict",
                "passed": False,
                "detail": "adapter refused to carry critic_verdict",
            }],
            provenance={
                "tool": "interrogate",
                "provider": provider or PRODUCER_INTERROGATE,
                "critic_verdict": None,
                "replaces_critic": False,
            },
            notes_for_next_phase=[
                "Interrogate never replaces Critic. Strip critic_verdict and retry "
                "with findings only."
            ],
        )

    if not provider_present:
        return build_envelope(
            **identity,
            phase_kind=PHASE_KIND_AGENT,
            producer=PRODUCER_INTERROGATE,
            status=STATUS_GAP,
            claims=[{
                "claim_id": "provider_availability",
                "provider_present": False,
            }],
            checks=[{
                "check_id": "provider_present",
                "passed": False,
                "detail": "interrogate provider absent; Critic proceeds without it",
            }],
            provenance={
                "tool": "interrogate",
                "provider": provider or PRODUCER_INTERROGATE,
                "explicit_gap": (
                    "adversarial interrogate aid unavailable; not a Critic pass"
                ),
                "critic_verdict": None,
                "replaces_critic": False,
            },
            notes_for_next_phase=[
                "Missing interrogate aid is a gap for Critic inputs, not a PASS."
            ],
        )

    claims: list[dict[str, Any]] = []
    for raw in findings or ():
        finding = dict(raw)
        # Strip any smuggled critic verdict keys from findings.
        finding.pop("critic_verdict", None)
        finding.pop("verdict", None)
        if "status" in finding and str(finding["status"]).upper() in CRITIC_VERDICT_VALUES:
            # Panel labels must not reuse Critic vocabulary as authority.
            finding["status"] = "open"
            finding["notes"] = (
                str(finding.get("notes") or "")
                + " (normalized: Critic verdict literals are not allowed on findings)"
            ).strip()
        claims.append(finding)

    checks: list[dict[str, Any]] = []
    for raw in required_code_checks or ():
        check = dict(raw)
        check.setdefault("phase_kind_shape", PHASE_KIND_CODE)
        checks.append(check)

    # Panel votes alone never gate PASS — only deterministic CODE checks may.
    code_failures = [
        c for c in checks
        if c.get("phase_kind_shape") == PHASE_KIND_CODE and c.get("passed") is False
    ]
    if not claims and not checks:
        status = STATUS_GAP
        checks.append({
            "check_id": "findings_present",
            "passed": False,
            "detail": "no findings and no CODE checks — not evidence of PASS",
        })
    elif code_failures:
        status = STATUS_FAILED
    else:
        # Findings delivered successfully as Critic inputs — not a ship verdict.
        status = STATUS_PASSED
        checks.append({
            "check_id": "findings_for_critic",
            "passed": True,
            "detail": (
                "findings recorded for Critic; envelope status is not critic_verdict"
            ),
        })

    artifacts: list[dict[str, Any]] = []
    if report_path:
        artifacts.append({"kind": "interrogate_report", "path": report_path})

    provenance: dict[str, Any] = {
        "tool": "interrogate",
        "provider": provider or PRODUCER_INTERROGATE,
        "critic_verdict": None,
        "replaces_critic": False,
    }
    if models:
        provenance["models"] = list(models)
    if runtime:
        provenance["runtime"] = runtime
    if model:
        provenance["model"] = model
    if effort:
        provenance["effort"] = effort
    if session:
        provenance["session"] = session

    envelope = build_envelope(
        **identity,
        phase_kind=PHASE_KIND_AGENT,
        producer=PRODUCER_INTERROGATE,
        status=status,
        claims=claims,
        artifacts=artifacts,
        checks=checks,
        provenance=provenance,
        notes_for_next_phase=[
            "Findings only — Critic maps these into acceptance_results / "
            "blocking_findings. Never treat this envelope as critic_verdict.",
        ],
    )
    # Hard invariant: serialized form must not carry critic_verdict authority.
    serialized = envelope.as_dict()
    if "critic_verdict" in serialized:
        raise VerificationAdapterError("envelope must not contain critic_verdict key")
    if serialized["provenance"].get("critic_verdict") not in (None,):
        raise VerificationAdapterError(
            "provenance.critic_verdict must remain null"
        )
    return envelope


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _git_output(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def capture_worktree_file_index(cwd: Path | str) -> dict[str, str]:
    """Map relative paths -> content sha256 for tracked + untracked files."""
    root = Path(cwd).resolve()
    index: dict[str, str] = {}
    tracked = _git_output(root, "ls-files", "-z")
    if tracked.returncode == 0 and tracked.stdout:
        for rel in tracked.stdout.split("\0"):
            if not rel:
                continue
            path = root / rel
            if path.is_file():
                index[rel.replace("\\", "/")] = _sha256_bytes(path.read_bytes())
    untracked = _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode == 0 and untracked.stdout:
        for rel in untracked.stdout.split("\0"):
            if not rel:
                continue
            path = root / rel
            if path.is_file():
                index[rel.replace("\\", "/")] = _sha256_bytes(path.read_bytes())
    return index


def compute_worktree_verification_fingerprint(
    cwd: Path | str,
    *,
    artifact_paths: Sequence[str] | None = None,
) -> str:
    """Verification fingerprint over HEAD, tracked diff, untracked content, artifacts.

    Includes:
      - HEAD SHA
      - tracked diff / content state (`git diff HEAD`)
      - untracked file names AND content hashes
      - optional relevant artifact paths + content hashes
    A new commit after verification changes HEAD and invalidates the fingerprint.
    Editing an untracked artifact changes its content hash and invalidates it.
    """
    root = Path(cwd).resolve()
    head = _git_output(root, "rev-parse", "HEAD")
    head_sha = (head.stdout or "").strip() if head.returncode == 0 else ""
    if not head_sha:
        err = (head.stderr or head.stdout or "").strip()
        if "unknown revision" in err.lower() or "bad revision" in err.lower() or "does not have any commits" in err.lower():
            head_sha = "NO_HEAD"
        elif "not a git repository" in err.lower():
            raise VerificationAdapterError(
                f"verification fingerprint requires a git worktree at {root}"
            )
        else:
            # Fall through with empty marker; factory may raise more specifically.
            head_sha = "NO_HEAD"

    diff = _git_output(root, "diff", "HEAD")
    if diff.returncode not in (0, 1):
        err = (diff.stderr or diff.stdout or "").strip()
        if "not a git repository" in err.lower():
            raise VerificationAdapterError(
                f"verification fingerprint requires a git worktree at {root}"
            )
        if "unknown revision" in err.lower() or "bad revision" in err.lower() or "does not have any commits" in err.lower():
            diff_text = ""
        else:
            raise VerificationAdapterError(f"git diff failed for fingerprint: {err[:300]}")
    else:
        diff_text = diff.stdout or ""

    untracked = _git_output(root, "ls-files", "--others", "--exclude-standard")
    untracked_lines: list[str] = []
    for rel in (untracked.stdout or "").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = root / rel
        if path.is_file():
            digest = _sha256_bytes(path.read_bytes())
            untracked_lines.append(f"{rel}:{digest}")
        else:
            untracked_lines.append(f"{rel}:")
    untracked_lines.sort()

    artifact_lines: list[str] = []
    for raw in artifact_paths or ():
        rel = str(raw).replace("\\", "/")
        path = Path(rel) if Path(rel).is_absolute() else root / rel
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            artifact_lines.append(f"{rel}:OUTSIDE")
            continue
        if resolved.is_file():
            artifact_lines.append(f"{rel}:{_sha256_bytes(resolved.read_bytes())}")
        else:
            artifact_lines.append(f"{rel}:MISSING")
    artifact_lines.sort()

    payload = (
        f"HEAD:{head_sha}\n"
        f"--diff--\n{diff_text}\n"
        f"--untracked--\n" + "\n".join(untracked_lines) + "\n"
        f"--artifacts--\n" + "\n".join(artifact_lines) + "\n"
    )
    return _sha256_text(payload)


def validate_critic_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    task_id: str,
    content_fingerprint: str | None,
    work_order_id: str | None = None,
) -> dict[str, Any]:
    """Require provenance-bearing Critic evidence; reject bare verdict strings."""
    if not isinstance(evidence, Mapping):
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence must be a provenance-bearing mapping "
            "(caller-supplied critic_verdict strings are rejected)"
        )
    result = evidence.get("result", evidence.get("verdict"))
    if result not in ACCEPTED_CRITIC_RESULTS:
        raise VerificationAdapterError(
            f"DONE fails closed: Critic result {result!r} not accepted"
        )
    ev_task = evidence.get("task_id")
    if not isinstance(ev_task, str) or ev_task != task_id:
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence.task_id must bind to the task"
        )
    node = evidence.get("work_order_id", evidence.get("node_id"))
    if not isinstance(node, str) or not node.strip():
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence must bind work_order_id/node_id"
        )
    if work_order_id is not None and node != work_order_id:
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence work_order/node does not match"
        )
    verified_rev = evidence.get("verified_revision")
    verified_fp = evidence.get("content_fingerprint") or evidence.get("verification_fingerprint")
    if not isinstance(verified_rev, str) or not verified_rev.strip():
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence.verified_revision required"
        )
    if not isinstance(verified_fp, str) or not verified_fp.strip():
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence must include content/verification fingerprint"
        )
    if content_fingerprint is not None and verified_fp != content_fingerprint:
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence fingerprint does not match verified content"
        )
    session = evidence.get("session") or evidence.get("session_id") or evidence.get("source")
    if not isinstance(session, str) or not session.strip():
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence requires Critic session/source"
        )
    timestamp = evidence.get("timestamp") or evidence.get("ended_at")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise VerificationAdapterError(
            "DONE fails closed: critic_evidence.timestamp required"
        )
    return {
        "result": result,
        "task_id": ev_task,
        "work_order_id": node,
        "verified_revision": verified_rev.strip(),
        "content_fingerprint": verified_fp,
        "session": session.strip(),
        "timestamp": timestamp.strip(),
        "source": evidence.get("source") or session.strip(),
    }


def validate_operator_gate_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    prepared_gate_action: Mapping[str, Any] | None,
    operator_gate_required: bool,
) -> dict[str, Any] | None:
    """Require MANDATORY_OPERATOR_GATE evidence bound to prepared_gate_action."""
    if not operator_gate_required:
        if evidence:
            raise VerificationAdapterError(
                "DONE fails closed: non-gated work must not invent operator gate evidence"
            )
        return None
    if prepared_gate_action is None or not isinstance(prepared_gate_action, Mapping):
        raise VerificationAdapterError(
            "DONE fails closed: operator_gate_required needs prepared_gate_action"
        )
    if not isinstance(evidence, Mapping):
        raise VerificationAdapterError(
            "DONE fails closed: MANDATORY_OPERATOR_GATE evidence required for gated work"
        )
    kind = (
        evidence.get("interruption_class")
        or evidence.get("class")
        or evidence.get("kind")
        or evidence.get("gate_kind")
    )
    if kind != MANDATORY_OPERATOR_GATE:
        raise VerificationAdapterError(
            "DONE fails closed: operator authorization must be MANDATORY_OPERATOR_GATE evidence"
        )
    if evidence.get("approved") is not True and evidence.get("authorized") is not True:
        raise VerificationAdapterError(
            "DONE fails closed: operator gate evidence must record approved/authorized=True"
        )
    bound = evidence.get("prepared_gate_action") or evidence.get("bound_action")
    if not isinstance(bound, Mapping):
        raise VerificationAdapterError(
            "DONE fails closed: operator gate evidence must bind prepared_gate_action"
        )
    # Exact action binding — stale/mismatched authorization fails closed.
    if json.dumps(bound, sort_keys=True, default=str) != json.dumps(
        dict(prepared_gate_action), sort_keys=True, default=str
    ):
        raise VerificationAdapterError(
            "DONE fails closed: operator gate authorization is stale or unbound "
            "to the exact prepared_gate_action"
        )
    timestamp = evidence.get("timestamp") or evidence.get("authorized_at")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise VerificationAdapterError(
            "DONE fails closed: operator gate evidence requires timestamp"
        )
    return {
        "interruption_class": MANDATORY_OPERATOR_GATE,
        "approved": True,
        "prepared_gate_action": dict(prepared_gate_action),
        "timestamp": timestamp.strip(),
        "authorized_by": evidence.get("authorized_by"),
    }



__all__ = [
    "ACCEPTED_CRITIC_RESULTS",
    "AUTH_MODES_SENSITIVE",
    "CRITIC_VERDICT_VALUES",
    "ENVELOPE_STATUSES",
    "EvidenceEnvelope",
    "MANDATORY_OPERATOR_GATE",
    "PHASE_KIND_AGENT",
    "PHASE_KIND_CODE",
    "PHASE_KIND_TOOL",
    "PHASE_KINDS",
    "PRODUCER_BLAST_RADIUS",
    "PRODUCER_CANARY",
    "PRODUCER_INTERROGATE",
    "STATUS_FAILED",
    "STATUS_GAP",
    "STATUS_PASSED",
    "STATUS_REFUSED",
    "VerificationAdapterError",
    "capture_worktree_file_index",
    "compute_worktree_verification_fingerprint",
    "validate_critic_evidence",
    "validate_operator_gate_evidence",
    "blast_radius_adapter",
    "build_envelope",
    "canary_browser_adapter",
    "interrogate_adversarial_adapter",
]
