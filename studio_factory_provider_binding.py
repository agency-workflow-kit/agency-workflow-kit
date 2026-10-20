#!/usr/bin/env python3
"""Provider-neutral AGENT/TOOL execution binding for the Factory Run Engine.

Bindings execute judgment/generative (AGENT) or external-tool (TOOL) phases and
return claim manifests plus provenance. Claims remain untrusted until
``studio_factory_run.gate_agent_or_tool_envelope`` verifies artifacts.

This module is intentionally small: a request/result contract, a Protocol, and
one deterministic local binding for tests (no live model spawn). Host-specific
runtimes plug in via the same interface without the factory runner hardcoding a
single provider. Avoids importing studio_factory_run to prevent circular imports;
the runner adapts BindingExecutionResult into AgentClaimPayload.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class BindingRequest:
    """One AGENT/TOOL execution attempt (initial or bounded repair)."""

    phase: Any  # PhaseSpec-like; kept Any to avoid circular import
    cwd: Path
    session_id: str | None = None
    repair_context: Mapping[str, Any] | None = None
    prior_envelope: Any | None = None
    # When execution routing selected a model/effort, callers pass them here.
    # Bindings consume these when present; they do not invent a live spawn.
    selected_model: str | None = None
    selected_effort: str | None = None


@dataclass
class BindingExecutionResult:
    """Outcome of a binding execute call — claims are manifests, not proof."""

    claims: list[dict[str, Any]]
    claimed_artifacts: list[str]
    session_id: str
    provider: str
    runtime: str
    model: str | None = None
    effort: str | None = None
    continuation_supported: bool = False
    resumed_session: bool = False
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class ProviderBinding(Protocol):
    """Provider-neutral execution binding for AGENT/TOOL phases."""

    @property
    def provider_id(self) -> str:
        ...

    @property
    def runtime_id(self) -> str:
        ...

    @property
    def continuation_supported(self) -> bool:
        """True only when the host exposes a real same-session resume API."""
        ...

    def execute(self, request: BindingRequest) -> BindingExecutionResult:
        ...


@dataclass
class ScriptedTurn:
    """One scripted AGENT/TOOL turn for ``LocalDeterministicBinding``."""

    claims: list[dict[str, Any]]
    claimed_artifacts: list[str] = field(default_factory=list)
    # Relative path → file contents written under cwd before returning claims.
    write_artifacts: Mapping[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    model: str | None = "local-deterministic"
    effort: str | None = None


class LocalDeterministicBinding:
    """Deterministic fake/local binding used by tests — never spawns a live model.

    Host session resume is absent by default, so ``continuation_supported`` is
    False. The session id is still kept stable across bounded repairs (no
    cold-start of a new id). ``resumed_session`` stays False so evidence does
    not pretend a live spawn/resume occurred.
    """

    def __init__(
        self,
        turns: Sequence[ScriptedTurn],
        *,
        provider_id: str = "local-deterministic",
        runtime_id: str = "local-fake",
        session_id: str | None = None,
        continuation_supported: bool = False,
    ) -> None:
        if not turns:
            raise ValueError("LocalDeterministicBinding requires at least one ScriptedTurn")
        self._turns = list(turns)
        self._provider_id = provider_id
        self._runtime_id = runtime_id
        self._session_id = session_id or f"local-sess-{uuid.uuid4().hex[:12]}"
        self._continuation_supported = bool(continuation_supported)
        self._call_count = 0
        self.call_session_ids: list[str] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def continuation_supported(self) -> bool:
        return self._continuation_supported

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def call_count(self) -> int:
        return self._call_count

    def execute(self, request: BindingRequest) -> BindingExecutionResult:
        # Same-session continuation: reuse prior id when present; otherwise keep
        # the binding's stable id. Never invent a new id on repair.
        if request.session_id:
            session_id = request.session_id
            if session_id != self._session_id:
                self._session_id = session_id
        else:
            session_id = self._session_id

        turn_index = min(self._call_count, len(self._turns) - 1)
        turn = self._turns[turn_index]
        self._call_count += 1
        self.call_session_ids.append(session_id)

        for rel, content in (turn.write_artifacts or {}).items():
            path = Path(rel) if Path(rel).is_absolute() else request.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        # Only claim a real host resume when the binding advertises resume support
        # AND this call is a repair continuation. Local fake defaults to false.
        resumed = bool(
            self._continuation_supported
            and request.session_id is not None
            and request.repair_context is not None
        )

        notes = list(turn.notes)
        if request.repair_context is not None:
            notes.append("repair_continuation")
            notes.append(f"continuation_supported={self._continuation_supported}")

        model = (
            request.selected_model
            if request.selected_model is not None
            else turn.model
        )
        effort = (
            request.selected_effort
            if request.selected_effort is not None
            else turn.effort
        )
        if request.selected_model is not None:
            notes.append(f"routing_selected_model={request.selected_model}")
        if request.selected_effort is not None:
            notes.append(f"routing_selected_effort={request.selected_effort}")

        return BindingExecutionResult(
            claims=list(turn.claims),
            claimed_artifacts=list(turn.claimed_artifacts),
            session_id=session_id,
            provider=self._provider_id,
            runtime=self._runtime_id,
            model=model,
            effort=effort,
            continuation_supported=self._continuation_supported,
            resumed_session=resumed,
            notes=notes,
        )
