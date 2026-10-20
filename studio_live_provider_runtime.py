#!/usr/bin/env python3
"""Live runtime/provider execution layer for Factory AGENT/TOOL phases.

Implements ``ProviderBinding`` against real host CLIs (Claude Code today)
while preserving the landed factory binding contract in
``studio_factory_provider_binding``. Claims remain manifests until the
factory gate verifies artifacts.

Honesty rules:
  * ``live_spawn`` is True only when a real provider CLI session was opened.
  * Local / fake / probe-only paths never claim a live model call.
  * Unsupported providers return an explicit GAP capability result with
    ``passed=False`` — never success.
  * Same-session resume uses the host ``--resume`` API when available.
  * No new Operator approval category; permission prompts stay ``none``
    (fail-closed denials), never forged bypass authorization.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from studio_factory_provider_binding import (
    BindingExecutionResult,
    BindingRequest,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_CLAUDE_CODE = "claude-code"
PROVIDER_CURSOR_AGENT = "cursor-agent"
PROVIDER_LOCAL_DETERMINISTIC = "local-deterministic"

RUNTIME_CLAUDE_CODE_CLI = "claude-code-cli"
RUNTIME_CURSOR_AGENT_CLI = "cursor-agent-cli"
RUNTIME_UNSUPPORTED = "unsupported"
RUNTIME_LOCAL_FAKE = "local-fake"

CAPABILITY_OK = "OK"
CAPABILITY_GAP = "GAP"
CAPABILITY_ENVIRONMENT_GAP = "ENVIRONMENT_GAP"

STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_GAP = "GAP"

DEFAULT_MAX_TURNS = 5
DEFAULT_CLI_TIMEOUT_S = 180

# #116 explicit-model contract. A native ``--agent`` spawn takes its model
# from the agent frontmatter and never receives ``--model``; a non-agent
# spawn must name its model explicitly. Neither may fall back to the host
# default, and no recorded-escalation mechanism for a per-invocation
# override exists (P §23 / §25.7), so none is accepted.
# #124 applies the same rule to effort: the frontmatter ``effort:`` pin owns a
# native ``--agent`` spawn's effort (P §25 / §28.2 / §29.3), so ``--effort``
# is never passed there and any per-invocation effort is refused. Non-agent
# effort is unchanged. ``claude -p`` reports no served effort.
SPAWN_KIND_AGENT = "agent"
SPAWN_KIND_NON_AGENT = "non_agent"
# The manifest studio_renderer emits agent frontmatter ``model:`` from; the
# installed runtime carries it at this same relative location.
_KIT_MANIFEST_PATH = Path(__file__).resolve().parent / "_studio" / "kit" / "manifest.json"

# Permission posture: match studio_specialist_invocation — deny prompts,
# never invent a new approval category or forge bypass authorization.
_PERMISSION_PROMPTS = "none"

_NOTE_LIVE_SPAWN_TRUE = "live_spawn=true"
_NOTE_LIVE_SPAWN_FALSE = "live_spawn=false"
_NOTE_CAPABILITY_GAP = "capability=GAP"
_NOTE_CAPABILITY_OK = "capability=OK"
_NOTE_CAPABILITY_ENV_GAP = "capability=ENVIRONMENT_GAP"

CliRunner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


_SYNTHESIZED_SESSION_PREFIXES = (
    "err-sess-",
    "fail-sess-",
    "badjson-sess-",
    "nosess-",
    "gap-sess-",
)


def is_synthesized_session_id(session_id: str | None) -> bool:
    """True when the session id was locally synthesized (never provider-issued)."""
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    sid = session_id.strip()
    return sid.startswith(_SYNTHESIZED_SESSION_PREFIXES)


def _mark_synthesized_notes(notes: list[str], *, synthesized: bool) -> list[str]:
    out = list(notes)
    flag = "session_id_synthesized=true" if synthesized else "session_id_synthesized=false"
    if flag not in out:
        out.append(flag)
    if synthesized and "session_id_resumable=false" not in out:
        out.append("session_id_resumable=false")
    return out



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _note_flag(notes: Sequence[str], key: str) -> str | None:
    prefix = f"{key}="
    for note in notes:
        if isinstance(note, str) and note.startswith(prefix):
            return note[len(prefix) :]
    return None


def live_spawn_claimed(result: BindingExecutionResult | Mapping[str, Any]) -> bool:
    """True only when evidence explicitly records a live provider spawn."""
    if isinstance(result, BindingExecutionResult):
        notes = result.notes
    else:
        notes = list(result.get("notes") or [])
        if "live_spawn" in result:
            return bool(result["live_spawn"])
    return _note_flag(notes, "live_spawn") == "true"


# ---------------------------------------------------------------------------
# Environment probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilityProbe:
    """Concrete host probe for one provider — never invents availability."""

    provider_id: str
    runtime_id: str
    binary_path: str | None
    version: str | None
    binary_exists: bool
    can_open_noninteractive_session: bool
    continuation_supported: bool
    capability: str  # OK | GAP | ENVIRONMENT_GAP
    error: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "runtime_id": self.runtime_id,
            "binary_path": self.binary_path,
            "version": self.version,
            "binary_exists": self.binary_exists,
            "can_open_noninteractive_session": self.can_open_noninteractive_session,
            "continuation_supported": self.continuation_supported,
            "capability": self.capability,
            "error": self.error,
            "detail": dict(self.detail),
        }


def _which(name: str) -> str | None:
    path = shutil.which(name)
    return path


def _run_cli(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
    runner: CliRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    if runner is not None:
        return runner(argv, cwd)
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def probe_claude_code(
    *,
    runner: CliRunner | None = None,
    skip_session_probe: bool = False,
) -> ProviderCapabilityProbe:
    """Probe Claude Code CLI presence and optional non-interactive open."""
    binary = _which("claude")
    if not binary:
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CLAUDE_CODE,
            runtime_id=RUNTIME_CLAUDE_CODE_CLI,
            binary_path=None,
            version=None,
            binary_exists=False,
            can_open_noninteractive_session=False,
            continuation_supported=True,  # API exists; host just lacks binary
            capability=CAPABILITY_ENVIRONMENT_GAP,
            error="claude binary not found on PATH",
            detail={"path_searched": os.environ.get("PATH", "")},
        )

    version: str | None = None
    version_err: str | None = None
    try:
        ver_proc = _run_cli([binary, "--version"], timeout=30, runner=runner)
        if ver_proc.returncode == 0:
            version = (ver_proc.stdout or ver_proc.stderr or "").strip().splitlines()[0][:200]
        else:
            version_err = (ver_proc.stderr or ver_proc.stdout or "").strip()[:300]
    except Exception as exc:  # pragma: no cover - defensive
        version_err = f"version probe failed: {exc}"

    if skip_session_probe:
        ok = version is not None
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CLAUDE_CODE,
            runtime_id=RUNTIME_CLAUDE_CODE_CLI,
            binary_path=binary,
            version=version,
            binary_exists=True,
            can_open_noninteractive_session=ok,
            continuation_supported=True,
            capability=CAPABILITY_OK if ok else CAPABILITY_ENVIRONMENT_GAP,
            error=None if ok else version_err or "version probe failed",
            detail={"session_probe": "skipped"},
        )

    # Minimal non-interactive session — proves the host can open one.
    session_argv = [
        binary,
        "-p",
        "Reply with exactly: STUDIO_LIVE_PROBE_OK",
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--permission-prompts",
        _PERMISSION_PROMPTS,
    ]
    try:
        proc = _run_cli(session_argv, timeout=DEFAULT_CLI_TIMEOUT_S, runner=runner)
    except Exception as exc:
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CLAUDE_CODE,
            runtime_id=RUNTIME_CLAUDE_CODE_CLI,
            binary_path=binary,
            version=version,
            binary_exists=True,
            can_open_noninteractive_session=False,
            continuation_supported=True,
            capability=CAPABILITY_ENVIRONMENT_GAP,
            error=f"non-interactive session probe raised: {exc}",
            detail={"argv": session_argv[1:]},
        )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CLAUDE_CODE,
            runtime_id=RUNTIME_CLAUDE_CODE_CLI,
            binary_path=binary,
            version=version,
            binary_exists=True,
            can_open_noninteractive_session=False,
            continuation_supported=True,
            capability=CAPABILITY_ENVIRONMENT_GAP,
            error=f"non-interactive session probe exit {proc.returncode}: {err}",
            detail={"argv": session_argv[1:]},
        )

    session_id = None
    try:
        payload = json.loads(proc.stdout or "")
        session_id = payload.get("session_id")
        is_error = bool(payload.get("is_error"))
    except json.JSONDecodeError as exc:
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CLAUDE_CODE,
            runtime_id=RUNTIME_CLAUDE_CODE_CLI,
            binary_path=binary,
            version=version,
            binary_exists=True,
            can_open_noninteractive_session=False,
            continuation_supported=True,
            capability=CAPABILITY_ENVIRONMENT_GAP,
            error=f"session probe returned non-JSON: {exc}",
            detail={"stdout_preview": (proc.stdout or "")[:300]},
        )

    if is_error or not session_id:
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CLAUDE_CODE,
            runtime_id=RUNTIME_CLAUDE_CODE_CLI,
            binary_path=binary,
            version=version,
            binary_exists=True,
            can_open_noninteractive_session=False,
            continuation_supported=True,
            capability=CAPABILITY_ENVIRONMENT_GAP,
            error="session probe JSON missing session_id or is_error=true",
            detail={"payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
        )

    return ProviderCapabilityProbe(
        provider_id=PROVIDER_CLAUDE_CODE,
        runtime_id=RUNTIME_CLAUDE_CODE_CLI,
        binary_path=binary,
        version=version,
        binary_exists=True,
        can_open_noninteractive_session=True,
        continuation_supported=True,
        capability=CAPABILITY_OK,
        error=None,
        detail={"probe_session_id": session_id, "version_error": version_err},
    )


def probe_cursor_agent() -> ProviderCapabilityProbe:
    """Probe Cursor agent CLI. Print mode exists; treat as optional GAP until wired."""
    binary = _which("cursor")
    if not binary:
        return ProviderCapabilityProbe(
            provider_id=PROVIDER_CURSOR_AGENT,
            runtime_id=RUNTIME_CURSOR_AGENT_CLI,
            binary_path=None,
            version=None,
            binary_exists=False,
            can_open_noninteractive_session=False,
            continuation_supported=True,
            capability=CAPABILITY_GAP,
            error="cursor binary not found on PATH",
        )
    # Cursor agent requires API key / interactive auth in many hosts.
    # Do not claim live session capability without a proven non-interactive open.
    return ProviderCapabilityProbe(
        provider_id=PROVIDER_CURSOR_AGENT,
        runtime_id=RUNTIME_CURSOR_AGENT_CLI,
        binary_path=binary,
        version=None,
        binary_exists=True,
        can_open_noninteractive_session=False,
        continuation_supported=True,
        capability=CAPABILITY_GAP,
        error=(
            "cursor agent CLI present but live Factory binding not enabled "
            "in this slice (explicit GAP; no forged success)"
        ),
        detail={"help_hint": "cursor agent --print"},
    )


def probe_live_provider_environment(
    *,
    providers: Sequence[str] | None = None,
    runner: CliRunner | None = None,
    skip_session_probe: bool = False,
) -> dict[str, ProviderCapabilityProbe]:
    """Probe supported providers. Unknown ids are GAP."""
    wanted = list(providers) if providers is not None else [
        PROVIDER_CLAUDE_CODE,
        PROVIDER_CURSOR_AGENT,
    ]
    out: dict[str, ProviderCapabilityProbe] = {}
    for provider_id in wanted:
        if provider_id == PROVIDER_CLAUDE_CODE:
            out[provider_id] = probe_claude_code(
                runner=runner, skip_session_probe=skip_session_probe
            )
        elif provider_id == PROVIDER_CURSOR_AGENT:
            out[provider_id] = probe_cursor_agent()
        else:
            out[provider_id] = ProviderCapabilityProbe(
                provider_id=provider_id,
                runtime_id=RUNTIME_UNSUPPORTED,
                binary_path=None,
                version=None,
                binary_exists=False,
                can_open_noninteractive_session=False,
                continuation_supported=False,
                capability=CAPABILITY_GAP,
                error=f"unsupported provider: {provider_id}",
            )
    return out


# ---------------------------------------------------------------------------
# Live evidence envelope (factory-compatible)
# ---------------------------------------------------------------------------


@dataclass
class LiveProviderEvidence:
    """Typed live-execution evidence compatible with factory envelope fields."""

    status: str
    passed: bool
    live_spawn: bool
    capability: str
    claims: list[dict[str, Any]]
    claimed_artifacts: list[str]
    session_id: str
    provider: str
    runtime: str
    model: str | None = None
    effort: str | None = None
    continuation_supported: bool = False
    resumed_session: bool = False
    session_id_synthesized: bool = False
    duration_ms: int = 0
    started_at: str = ""
    ended_at: str = ""
    notes: list[str] = field(default_factory=list)
    result_provenance: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_binding_result(self) -> BindingExecutionResult:
        notes = _mark_synthesized_notes(list(self.notes), synthesized=self.session_id_synthesized)
        notes.append(_NOTE_LIVE_SPAWN_TRUE if self.live_spawn else _NOTE_LIVE_SPAWN_FALSE)
        if self.capability == CAPABILITY_GAP:
            notes.append(_NOTE_CAPABILITY_GAP)
        elif self.capability == CAPABILITY_ENVIRONMENT_GAP:
            notes.append(_NOTE_CAPABILITY_ENV_GAP)
        else:
            notes.append(_NOTE_CAPABILITY_OK)
        notes.append(f"duration_ms={self.duration_ms}")
        notes.append(f"status={self.status}")
        return BindingExecutionResult(
            claims=list(self.claims),
            claimed_artifacts=list(self.claimed_artifacts),
            session_id=self.session_id,
            provider=self.provider,
            runtime=self.runtime,
            model=self.model,
            effort=self.effort,
            continuation_supported=self.continuation_supported,
            resumed_session=self.resumed_session,
            notes=notes,
        )

    def to_factory_provenance(self, *, tool: str | None = None) -> dict[str, Any]:
        """Fields aligned with ``EvidenceEnvelope.provenance`` for AGENT/TOOL."""
        return {
            "command": None,
            "tool": tool,
            "provider": self.provider,
            "runtime": self.runtime,
            "model": self.model,
            "effort": self.effort,
            "session": self.session_id,
            "continuation_supported": self.continuation_supported,
            "resumed_session": bool(self.resumed_session),
            "live_spawn": bool(self.live_spawn),
            "exit_code": self.result_provenance.get("exit_code"),
            "duration_ms": self.duration_ms,
            "artifact_paths": list(self.claimed_artifacts),
            "capability": self.capability,
            "result": dict(self.result_provenance),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "live_spawn": self.live_spawn,
            "capability": self.capability,
            "claims": list(self.claims),
            "claimed_artifacts": list(self.claimed_artifacts),
            "session_id": self.session_id,
            "provider": self.provider,
            "runtime": self.runtime,
            "model": self.model,
            "effort": self.effort,
            "continuation_supported": self.continuation_supported,
            "resumed_session": self.resumed_session,
            "session_id_synthesized": self.session_id_synthesized,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "notes": list(self.notes),
            "result_provenance": dict(self.result_provenance),
            "checks": list(self.checks),
            "provenance": self.to_factory_provenance(),
            "timestamps": {"started_at": self.started_at, "ended_at": self.ended_at},
        }


def gap_evidence(
    *,
    provider_id: str,
    runtime_id: str = RUNTIME_UNSUPPORTED,
    reason: str,
    capability: str = CAPABILITY_GAP,
    session_id: str | None = None,
) -> LiveProviderEvidence:
    """Explicit unsupported / environment GAP — never success."""
    started = ended = _utc_now()
    synthesized = False
    if session_id and not is_synthesized_session_id(session_id):
        sid = session_id
    else:
        sid = session_id if (session_id and is_synthesized_session_id(session_id)) else (
            f"gap-sess-{uuid.uuid4().hex[:12]}"
        )
        synthesized = True
    claim = {
        "kind": "capability_gap",
        "capability": capability,
        "provider": provider_id,
        "passed": False,
        "reason": reason,
    }
    return LiveProviderEvidence(
        status=STATUS_GAP,
        passed=False,
        live_spawn=False,
        capability=capability,
        claims=[claim],
        claimed_artifacts=[],
        session_id=sid,
        provider=provider_id,
        runtime=runtime_id,
        model=None,
        effort=None,
        continuation_supported=False,
        resumed_session=False,
        session_id_synthesized=synthesized,
        duration_ms=0,
        started_at=started,
        ended_at=ended,
        notes=_mark_synthesized_notes([reason], synthesized=synthesized),
        result_provenance={"gap": True, "reason": reason},
        checks=[{
            "name": "live_provider_capability",
            "passed": False,
            "capability": capability,
            "reason": reason,
        }],
    )


# ---------------------------------------------------------------------------
# Prompt + result parsing
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.DOTALL)


def _strip_json_fence(text: str) -> str:
    match = _JSON_FENCE_RE.fullmatch(text.strip())
    return match.group(1) if match else text


def _phase_attr(phase: Any, name: str, default: Any = None) -> Any:
    if phase is None:
        return default
    if isinstance(phase, Mapping):
        return phase.get(name, default)
    return getattr(phase, name, default)


def build_agent_prompt(request: BindingRequest) -> str:
    """Build a bounded claim-manifest prompt for a live AGENT/TOOL turn."""
    phase = request.phase
    phase_id = _phase_attr(phase, "phase_id", "phase")
    phase_kind = _phase_attr(phase, "phase_kind", "AGENT")
    producer = _phase_attr(phase, "producer", "unknown")
    notes = list(_phase_attr(phase, "notes", None) or [])
    lines = [
        f"Factory {phase_kind} phase {phase_id!r} (producer={producer}).",
        "Produce the claimed work product under the current working directory.",
        "Return exactly one JSON object as your final answer (raw JSON, no fence):",
        '{"claims":[{"statement":"...","path":"..."}],'
        '"claimed_artifacts":["relative/path"],"notes":["..."]}',
        "claims are manifests only — the factory gate verifies artifacts.",
    ]
    if notes:
        lines.append("Phase notes:")
        lines.extend(f"- {n}" for n in notes)
    if request.repair_context:
        lines.append("This is a bounded repair continuation on the same session.")
        failed = request.repair_context.get("failed_checks") or []
        if failed:
            lines.append(f"Prior failed checks: {json.dumps(failed)[:1000]}")
        target = request.repair_context.get("repair_target")
        if target:
            lines.append(f"repair_target={target}")
    return "\n".join(lines)


def _parse_claim_payload(result_text: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    text = _strip_json_fence(result_text or "").strip()
    if not text:
        return (
            [{"statement": "empty_provider_result", "passed": False}],
            [],
            ["empty_result"],
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Provider returned prose — record as a single opaque claim (gate will fail
        # unless artifacts were still produced and listed elsewhere).
        return (
            [{"statement": "unstructured_provider_result", "preview": text[:500]}],
            [],
            ["unstructured_result"],
        )
    if not isinstance(data, dict):
        return (
            [{"statement": "non_object_provider_result", "preview": text[:200]}],
            [],
            ["non_object_result"],
        )
    claims = data.get("claims")
    if not isinstance(claims, list):
        claims = [{"statement": "missing_claims_field", "raw_keys": sorted(data.keys())}]
    artifacts = data.get("claimed_artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    artifacts = [str(a) for a in artifacts]
    notes = data.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes = [str(n) for n in notes]
    return claims, artifacts, notes


def _model_from_payload(payload: Mapping[str, Any]) -> str | None:
    usage = payload.get("modelUsage")
    if isinstance(usage, dict) and usage:
        # Prefer first model key; also check nested canonicalModel.
        first_key = next(iter(usage.keys()))
        entry = usage.get(first_key)
        if isinstance(entry, dict) and entry.get("canonicalModel"):
            return str(entry["canonicalModel"])
        return str(first_key)
    return None


def _served_models(payload: Mapping[str, Any]) -> list[str]:
    usage = payload.get("modelUsage")
    served: list[str] = []
    if isinstance(usage, dict):
        for key, entry in usage.items():
            for name in (entry.get("canonicalModel") if isinstance(entry, dict) else None, key):
                if isinstance(name, str) and name and name not in served:
                    served.append(name)
    return served


def _agent_manifest_value(agent: str, key: str, manifest_path: Path) -> str | None:
    try:
        agents = json.loads(manifest_path.read_text(encoding="utf-8")).get("agents", [])
    except (OSError, ValueError, AttributeError):
        return None
    for entry in agents if isinstance(agents, list) else []:
        if isinstance(entry, dict) and entry.get("name") == agent:
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def agent_model_pin(agent: str, *, manifest_path: Path = _KIT_MANIFEST_PATH) -> str | None:
    """Canonical frontmatter model pin of a Kit agent, or None if it has none."""
    model = _agent_manifest_value(agent, "model", manifest_path)
    return None if model == "inherit" else model


def agent_effort_pin(agent: str, *, manifest_path: Path = _KIT_MANIFEST_PATH) -> str | None:
    """Canonical frontmatter effort pin of a Kit agent, or None if it has none."""
    return _agent_manifest_value(agent, "effort", manifest_path)


def _model_contract_notes(contract: Mapping[str, Any]) -> list[str]:
    notes = [
        f"model_contract={'PASS' if contract['passed'] else 'FAIL'}",
        f"spawn_kind={contract['spawn_kind']}",
        f"agent_frontmatter_pin={contract['agent_frontmatter_pin']}",
        f"requested_model={contract['requested_model']}",
        f"served_model={contract['served_model']}",
        f"model_override={contract['model_override']}",
    ]
    if contract["reason"]:
        notes.append(f"model_contract_reason={contract['reason']}")
    return notes


def _model_contract_check(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": "model_contract",
        "passed": bool(contract["passed"]),
        "expected_model": contract["expected_model"],
        "served_model": contract["served_model"],
        "reason": contract["reason"],
    }


def _effort_contract_notes(contract: Mapping[str, Any]) -> list[str]:
    notes = [
        f"effort_contract={'PASS' if contract['passed'] else 'FAIL'}",
        f"agent_frontmatter_effort={contract['agent_frontmatter_effort']}",
        f"requested_effort={contract['requested_effort']}",
        f"expected_effort={contract['expected_effort']}",
        f"observed_effort={contract['observed_effort']}",
        f"effort_override={contract['effort_override']}",
    ]
    if contract["reason"]:
        notes.append(f"effort_contract_reason={contract['reason']}")
    return notes


def _effort_contract_check(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": "effort_contract",
        "passed": bool(contract["passed"]),
        "expected_effort": contract["expected_effort"],
        "observed_effort": contract["observed_effort"],
        "reason": contract["reason"],
    }


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


class UnsupportedLiveBinding:
    """Fail-closed binding for unknown / unsupported providers."""

    def __init__(self, provider_id: str, *, reason: str | None = None) -> None:
        self._provider_id = provider_id
        self._runtime_id = RUNTIME_UNSUPPORTED
        self._reason = reason or f"unsupported provider: {provider_id}"

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def continuation_supported(self) -> bool:
        return False

    def execute(self, request: BindingRequest) -> BindingExecutionResult:
        evidence = gap_evidence(
            provider_id=self._provider_id,
            runtime_id=self._runtime_id,
            reason=self._reason,
            capability=CAPABILITY_GAP,
            session_id=request.session_id,
        )
        return evidence.to_binding_result()

    def execute_evidence(self, request: BindingRequest) -> LiveProviderEvidence:
        return gap_evidence(
            provider_id=self._provider_id,
            runtime_id=self._runtime_id,
            reason=self._reason,
            capability=CAPABILITY_GAP,
            session_id=request.session_id,
        )


class ClaudeCodeLiveBinding:
    """Real Claude Code CLI binding for Factory AGENT/TOOL phases.

    Opens a non-interactive ``claude -p`` session. Same-session repair uses
    ``--resume <session_id>`` when the request carries a prior session id.
    ``live_spawn`` is True only after the CLI actually runs.
    """

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_s: float = DEFAULT_CLI_TIMEOUT_S,
        agent: str | None = None,
        runner: CliRunner | None = None,
        require_available: bool = True,
    ) -> None:
        self._cli_path = str(cli_path) if cli_path else (_which("claude") or "claude")
        self._model = model
        self._effort = effort
        self._max_turns = int(max_turns)
        if self._max_turns < 1:
            raise ValueError("max_turns must be a positive integer")
        self._timeout_s = float(timeout_s)
        self._agent = agent
        self._runner = runner
        self._require_available = require_available
        self._last_evidence: LiveProviderEvidence | None = None
        self.call_session_ids: list[str] = []
        self.call_count = 0

    @property
    def provider_id(self) -> str:
        return PROVIDER_CLAUDE_CODE

    @property
    def runtime_id(self) -> str:
        return RUNTIME_CLAUDE_CODE_CLI

    @property
    def continuation_supported(self) -> bool:
        return True

    @property
    def last_evidence(self) -> LiveProviderEvidence | None:
        return self._last_evidence

    def _build_argv(
        self, request: BindingRequest, *, resume: bool, model: str | None
    ) -> list[str]:
        if not self._agent and not model:
            raise ValueError("non-agent Claude spawn requires an explicit model")
        prompt = build_agent_prompt(request)
        argv = [
            self._cli_path,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(self._max_turns),
            "--permission-prompts",
            _PERMISSION_PROMPTS,
        ]
        # Native --agent: the frontmatter owns model and effort; --model or
        # --effort would be a per-invocation override. Non-agent: the model is
        # always explicit and an explicit effort is passed through.
        _, effort = self._effective_model_effort(request)
        if self._agent:
            if effort is not None:
                raise ValueError("native --agent spawn never takes a per-invocation effort")
            argv.extend(["--agent", self._agent])
        else:
            argv.extend(["--model", str(model)])
            if effort:
                argv.extend(["--effort", effort])
        if resume and request.session_id:
            argv.extend(["--resume", str(request.session_id)])
        return argv

    def _effective_model_effort(
        self, request: BindingRequest
    ) -> tuple[str | None, str | None]:
        model = (
            request.selected_model
            if request.selected_model is not None
            else self._model
        )
        effort = (
            request.selected_effort
            if request.selected_effort is not None
            else self._effort
        )
        return model, effort

    def _model_contract(self, request: BindingRequest) -> dict[str, Any]:
        """Resolve the expected model before spawn; ``reason`` set = refuse."""
        requested, _ = self._effective_model_effort(request)
        contract: dict[str, Any] = {
            "spawn_kind": SPAWN_KIND_AGENT if self._agent else SPAWN_KIND_NON_AGENT,
            "agent": self._agent,
            "agent_frontmatter_pin": None,
            "requested_model": requested,
            "expected_model": requested or None,
            "served_model": None,
            "served_models": [],
            "model_override": None,
            "escalation": "none",
            "passed": False,
            "reason": None,
        }
        if self._agent:
            pin = agent_model_pin(self._agent)
            contract.update(
                agent_frontmatter_pin=pin, expected_model=pin, model_override=requested
            )
            if pin is None:
                contract["reason"] = (
                    f"--agent {self._agent!r} has no canonical frontmatter model pin"
                )
            elif requested is not None:
                contract["reason"] = (
                    f"per-invocation model {requested!r} refused for --agent "
                    f"{self._agent} (frontmatter pin {pin}); no recorded escalation "
                    "exists (P §23 / §25.7)"
                )
        elif not requested:
            contract["reason"] = (
                "non-agent Claude spawn requires an explicit model; "
                "host default model selection is never acceptable"
            )
        return contract

    def _effort_contract(self, request: BindingRequest) -> dict[str, Any]:
        """Resolve the expected effort before spawn; ``reason`` set = refuse.

        ``observed_effort`` stays None: the CLI result reports no served effort.
        """
        _, requested = self._effective_model_effort(request)
        contract: dict[str, Any] = {
            "spawn_kind": SPAWN_KIND_AGENT if self._agent else SPAWN_KIND_NON_AGENT,
            "agent": self._agent,
            "agent_frontmatter_effort": None,
            "requested_effort": requested,
            "expected_effort": requested or None,
            "observed_effort": None,
            "effort_override": None,
            "escalation": "none",
            "passed": True,
            "reason": None,
        }
        if self._agent:
            pin = agent_effort_pin(self._agent)
            contract.update(
                agent_frontmatter_effort=pin, expected_effort=pin, effort_override=requested
            )
            if requested is not None:
                contract.update(passed=False, reason=(
                    f"per-invocation effort {requested!r} refused for --agent "
                    f"{self._agent} (frontmatter effort {pin}); no recorded escalation "
                    "exists (P §23 / §25.7)"
                ))
        return contract

    def _contract_refusal(
        self,
        request: BindingRequest,
        kind: str,
        contract: dict[str, Any],
        started: str,
        t0: float,
    ) -> LiveProviderEvidence:
        """Refuse before the CLI runs; nothing was spawned or served."""
        notes, check = (
            (_model_contract_notes, _model_contract_check)
            if kind == "model"
            else (_effort_contract_notes, _effort_contract_check)
        )
        real_sid = request.session_id and not is_synthesized_session_id(request.session_id)
        session_id = str(request.session_id) if real_sid else f"fail-sess-{uuid.uuid4().hex[:12]}"
        evidence = LiveProviderEvidence(
            status=STATUS_FAILED,
            passed=False,
            live_spawn=False,
            capability=CAPABILITY_OK,
            claims=[{
                "kind": f"{kind}_contract_violation",
                "reason": contract["reason"],
                "passed": False,
            }],
            claimed_artifacts=[],
            session_id=session_id,
            provider=self.provider_id,
            runtime=self.runtime_id,
            model=None,
            effort=None,
            continuation_supported=True,
            resumed_session=False,
            session_id_synthesized=not real_sid,
            duration_ms=int((time.monotonic() - t0) * 1000),
            started_at=started,
            ended_at=_utc_now(),
            notes=notes(contract),
            result_provenance={f"{kind}_contract": contract},
            checks=[check(contract)],
        )
        self._last_evidence = evidence
        self.call_count += 1
        self.call_session_ids.append(evidence.session_id)
        return evidence

    def execute_evidence(self, request: BindingRequest) -> LiveProviderEvidence:
        started = _utc_now()
        t0 = time.monotonic()
        effective_model, effective_effort = self._effective_model_effort(request)

        if self._require_available and self._runner is None:
            if not shutil.which(self._cli_path) and not Path(self._cli_path).is_file():
                evidence = gap_evidence(
                    provider_id=self.provider_id,
                    runtime_id=self.runtime_id,
                    reason=f"claude CLI not found: {self._cli_path}",
                    capability=CAPABILITY_ENVIRONMENT_GAP,
                    session_id=request.session_id,
                )
                self._last_evidence = evidence
                return evidence

        resume = bool(
            request.session_id
            and (
                request.repair_context is not None
                or request.prior_envelope is not None
            )
        )
        if resume and is_synthesized_session_id(request.session_id):
            evidence = gap_evidence(
                provider_id=self.provider_id,
                runtime_id=self.runtime_id,
                reason=(
                    f"refusing resume of synthesized session_id "
                    f"{request.session_id!r}; synthesized ids are never resumed"
                ),
                capability=CAPABILITY_GAP,
                session_id=request.session_id,
            )
            evidence.duration_ms = int((time.monotonic() - t0) * 1000)
            evidence.started_at = started
            evidence.ended_at = _utc_now()
            self._last_evidence = evidence
            self.call_count += 1
            self.call_session_ids.append(evidence.session_id)
            return evidence
        contract = self._model_contract(request)
        if contract["reason"]:
            return self._contract_refusal(request, "model", contract, started, t0)
        effort_contract = self._effort_contract(request)
        if effort_contract["reason"]:
            return self._contract_refusal(request, "effort", effort_contract, started, t0)
        argv = self._build_argv(request, resume=resume, model=contract["requested_model"])

        try:
            proc = _run_cli(
                argv,
                cwd=Path(request.cwd),
                timeout=self._timeout_s,
                runner=self._runner,
            )
        except FileNotFoundError as exc:
            evidence = gap_evidence(
                provider_id=self.provider_id,
                runtime_id=self.runtime_id,
                reason=f"claude CLI not startable: {exc}",
                capability=CAPABILITY_ENVIRONMENT_GAP,
                session_id=request.session_id,
            )
            evidence.duration_ms = int((time.monotonic() - t0) * 1000)
            evidence.started_at = started
            evidence.ended_at = _utc_now()
            self._last_evidence = evidence
            self.call_count += 1
            self.call_session_ids.append(evidence.session_id)
            return evidence
        except Exception as exc:
            ended = _utc_now()
            duration_ms = int((time.monotonic() - t0) * 1000)
            # CLI was invoked (or runner accepted argv) — record live_spawn.
            evidence = LiveProviderEvidence(
                status=STATUS_FAILED,
                passed=False,
                live_spawn=True,
                capability=CAPABILITY_OK,
                claims=[{"kind": "provider_error", "error": str(exc), "passed": False}],
                claimed_artifacts=[],
                session_id=(
                    request.session_id
                    if request.session_id and not is_synthesized_session_id(request.session_id)
                    else (request.session_id or f"err-sess-{uuid.uuid4().hex[:12]}")
                ),
                provider=self.provider_id,
                runtime=self.runtime_id,
                model=effective_model,
                effort=effective_effort,
                continuation_supported=True,
                resumed_session=resume,
                session_id_synthesized=(
                    not bool(request.session_id)
                    or is_synthesized_session_id(request.session_id)
                ),
                duration_ms=duration_ms,
                started_at=started,
                ended_at=ended,
                notes=[f"cli_exception={exc}"],
                result_provenance={"error": str(exc), "argv_tail": argv[1:8]},
                checks=[{
                    "name": "live_provider_session",
                    "passed": False,
                    "error": str(exc),
                }],
            )
            self._last_evidence = evidence
            self.call_count += 1
            self.call_session_ids.append(evidence.session_id)
            return evidence

        ended = _utc_now()
        duration_ms = int((time.monotonic() - t0) * 1000)
        live_spawn = True  # CLI process ran

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            if request.session_id and not is_synthesized_session_id(request.session_id):
                sid = request.session_id
                synthesized_fail = False
            else:
                sid = request.session_id or f"fail-sess-{uuid.uuid4().hex[:12]}"
                synthesized_fail = True if not request.session_id else is_synthesized_session_id(sid)
                if not request.session_id:
                    sid = f"fail-sess-{uuid.uuid4().hex[:12]}"
                    synthesized_fail = True
            evidence = LiveProviderEvidence(
                status=STATUS_FAILED,
                passed=False,
                live_spawn=live_spawn,
                capability=CAPABILITY_OK,
                claims=[{
                    "kind": "provider_cli_failure",
                    "exit_code": proc.returncode,
                    "stderr_preview": stderr[:500],
                    "passed": False,
                }],
                claimed_artifacts=[],
                session_id=sid,
                provider=self.provider_id,
                runtime=self.runtime_id,
                model=effective_model,
                effort=effective_effort,
                continuation_supported=True,
                resumed_session=resume,
                session_id_synthesized=synthesized_fail,
                duration_ms=duration_ms,
                started_at=started,
                ended_at=ended,
                notes=[f"exit_code={proc.returncode}"],
                result_provenance={
                    "exit_code": proc.returncode,
                    "stderr_preview": stderr[:500],
                    "stdout_preview": stdout[:500],
                },
                checks=[{
                    "name": "live_provider_session",
                    "passed": False,
                    "exit_code": proc.returncode,
                }],
            )
            self._last_evidence = evidence
            self.call_count += 1
            self.call_session_ids.append(sid)
            return evidence

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            if request.session_id and not is_synthesized_session_id(request.session_id):
                sid = request.session_id
                synthesized_bad = False
            else:
                sid = f"badjson-sess-{uuid.uuid4().hex[:12]}"
                synthesized_bad = True
            evidence = LiveProviderEvidence(
                status=STATUS_FAILED,
                passed=False,
                live_spawn=live_spawn,
                capability=CAPABILITY_OK,
                claims=[{"kind": "provider_json_error", "error": str(exc), "passed": False}],
                claimed_artifacts=[],
                session_id=sid,
                provider=self.provider_id,
                runtime=self.runtime_id,
                model=effective_model,
                effort=effective_effort,
                continuation_supported=True,
                resumed_session=resume,
                session_id_synthesized=synthesized_bad,
                duration_ms=duration_ms,
                started_at=started,
                ended_at=ended,
                notes=["invalid_cli_json"],
                result_provenance={
                    "exit_code": 0,
                    "json_error": str(exc),
                    "stdout_preview": stdout[:500],
                },
                checks=[{
                    "name": "live_provider_session",
                    "passed": False,
                    "error": "invalid_cli_json",
                }],
            )
            self._last_evidence = evidence
            self.call_count += 1
            self.call_session_ids.append(sid)
            return evidence

        provider_session = payload.get("session_id")
        synthesized = False
        if isinstance(provider_session, str) and provider_session.strip():
            session_id = provider_session.strip()
        elif request.session_id and not is_synthesized_session_id(request.session_id):
            # Keep caller-supplied provider-issued id only when provider omitted one.
            session_id = str(request.session_id)
            synthesized = False
        else:
            session_id = f"nosess-{uuid.uuid4().hex[:12]}"
            synthesized = True

        if resume and request.session_id:
            returned = str(provider_session).strip() if provider_session else ""
            if not returned or returned != str(request.session_id):
                ended = _utc_now()
                duration_ms = int((time.monotonic() - t0) * 1000)
                evidence = LiveProviderEvidence(
                    status=STATUS_FAILED,
                    passed=False,
                    live_spawn=live_spawn,
                    capability=CAPABILITY_OK,
                    claims=[{
                        "kind": "session_resume_mismatch",
                        "requested_session_id": request.session_id,
                        "returned_session_id": returned or None,
                        "passed": False,
                    }],
                    claimed_artifacts=[],
                    session_id=returned or str(request.session_id),
                    provider=self.provider_id,
                    runtime=self.runtime_id,
                    model=_model_from_payload(payload) or effective_model,
                    effort=effective_effort,
                    continuation_supported=True,
                    resumed_session=True,
                    session_id_synthesized=False,
                    duration_ms=duration_ms,
                    started_at=started,
                    ended_at=ended,
                    notes=[
                        "resume_session_mismatch",
                        f"requested_session_id={request.session_id}",
                        f"returned_session_id={returned or None}",
                    ],
                    result_provenance={
                        "exit_code": 0,
                        "resume_mismatch": True,
                        "requested_session_id": request.session_id,
                        "returned_session_id": returned or None,
                    },
                    checks=[{
                        "name": "live_provider_session_resume",
                        "passed": False,
                        "requested_session_id": request.session_id,
                        "returned_session_id": returned or None,
                    }],
                )
                self._last_evidence = evidence
                self.call_count += 1
                self.call_session_ids.append(evidence.session_id)
                return evidence

        # Served model is what the provider reports, never the requested
        # value standing in for it; the expected model must be among them.
        served_models = _served_models(payload)
        expected = contract["expected_model"]
        model = expected if expected in served_models else _model_from_payload(payload)
        contract.update(served_model=model, served_models=served_models)
        if not served_models:
            contract["reason"] = (
                f"served model not reported; cannot verify expected {expected!r}"
            )
        elif expected not in served_models:
            contract["reason"] = (
                f"served model {served_models} does not match expected {expected!r}; "
                "unexplained substitution or host default"
            )
        contract["passed"] = contract["reason"] is None
        is_error = bool(payload.get("is_error"))
        result_text = payload.get("result")
        if result_text is None:
            result_text = ""
        elif not isinstance(result_text, str):
            result_text = json.dumps(result_text)

        claims, artifacts, extra_notes = _parse_claim_payload(result_text)
        passed = (not is_error) and bool(claims)
        # Claims alone do not prove artifacts — factory gate decides PASS.
        # Here "passed" means the live session completed without CLI/provider error.
        session_ok = not is_error and payload.get("session_id")
        if not contract["passed"]:
            session_ok = False
            claims = [{
                "kind": "model_contract_violation",
                "reason": contract["reason"],
                "passed": False,
            }] + list(claims)
        status = STATUS_PASSED if session_ok else STATUS_FAILED

        evidence = LiveProviderEvidence(
            status=status,
            passed=bool(session_ok),
            live_spawn=live_spawn,
            capability=CAPABILITY_OK,
            claims=list(claims),
            claimed_artifacts=list(artifacts),
            session_id=session_id,
            provider=self.provider_id,
            runtime=self.runtime_id,
            model=model,
            effort=effective_effort,
            continuation_supported=True,
            resumed_session=resume and bool(request.session_id),
            duration_ms=duration_ms,
            started_at=started,
            ended_at=ended,
            notes=list(extra_notes) + [
                f"terminal_reason={payload.get('terminal_reason')}",
                f"num_turns={payload.get('num_turns')}",
                f"subtype={payload.get('subtype')}",
            ] + _model_contract_notes(contract) + _effort_contract_notes(effort_contract),
            result_provenance={
                "exit_code": 0,
                "session_id": session_id,
                "is_error": is_error,
                "terminal_reason": payload.get("terminal_reason"),
                "num_turns": payload.get("num_turns"),
                "subtype": payload.get("subtype"),
                "duration_ms_provider": payload.get("duration_ms"),
                "total_cost_usd": payload.get("total_cost_usd"),
                "uuid": payload.get("uuid"),
                "modelUsage": payload.get("modelUsage"),
                "model_contract": contract,
                "effort_contract": effort_contract,
            },
            checks=[{
                "name": "live_provider_session",
                "passed": bool(session_ok),
                "session_id": session_id,
                "is_error": is_error,
            }, _model_contract_check(contract), _effort_contract_check(effort_contract)],
        )
        self._last_evidence = evidence
        self.call_count += 1
        self.call_session_ids.append(session_id)
        return evidence

    def execute(self, request: BindingRequest) -> BindingExecutionResult:
        return self.execute_evidence(request).to_binding_result()


def build_live_provider_binding(
    provider_id: str,
    **kwargs: Any,
) -> UnsupportedLiveBinding | ClaudeCodeLiveBinding:
    """Construct a live binding or an explicit GAP binding."""
    if provider_id == PROVIDER_CLAUDE_CODE:
        return ClaudeCodeLiveBinding(**kwargs)
    if provider_id == PROVIDER_LOCAL_DETERMINISTIC:
        return UnsupportedLiveBinding(
            provider_id,
            reason=(
                "local-deterministic is not a live provider; use "
                "LocalDeterministicBinding from studio_factory_provider_binding "
                "(live_spawn stays false)"
            ),
        )
    if provider_id == PROVIDER_CURSOR_AGENT:
        return UnsupportedLiveBinding(
            provider_id,
            reason=(
                "cursor-agent live Factory binding not enabled in this slice "
                "(explicit GAP)"
            ),
        )
    return UnsupportedLiveBinding(provider_id)


def execute_live_provider(
    provider_id: str,
    request: BindingRequest,
    **kwargs: Any,
) -> LiveProviderEvidence:
    """Run one live (or GAP) execution and return typed evidence."""
    binding = build_live_provider_binding(provider_id, **kwargs)
    if hasattr(binding, "execute_evidence"):
        return binding.execute_evidence(request)  # type: ignore[no-any-return]
    return gap_evidence(
        provider_id=provider_id,
        reason=f"binding {type(binding).__name__} lacks execute_evidence",
    )


__all__ = [
    "PROVIDER_CLAUDE_CODE",
    "PROVIDER_CURSOR_AGENT",
    "PROVIDER_LOCAL_DETERMINISTIC",
    "CAPABILITY_OK",
    "CAPABILITY_GAP",
    "CAPABILITY_ENVIRONMENT_GAP",
    "STATUS_PASSED",
    "STATUS_FAILED",
    "STATUS_GAP",
    "ProviderCapabilityProbe",
    "LiveProviderEvidence",
    "UnsupportedLiveBinding",
    "ClaudeCodeLiveBinding",
    "probe_claude_code",
    "probe_cursor_agent",
    "probe_live_provider_environment",
    "build_live_provider_binding",
    "execute_live_provider",
    "gap_evidence",
    "build_agent_prompt",
    "live_spawn_claimed",
    "is_synthesized_session_id",
]
