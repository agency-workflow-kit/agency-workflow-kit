#!/usr/bin/env python3
"""M8 Live Execution Observability: a thin, append-only lifecycle event
log shared by both execution paths available to the Director --
studio-run-engine (slice 1) and native Claude Agent() delegation
(slice 2, studio_native_lifecycle.py).

A read-only side effect around existing state-transition points --
never a second execution state machine. Orchestration, routing, retry,
and governance never read from this log to make a decision; it exists
only so an Operator can later replay a run's real history. No progress
percentages or ETA are ever recorded. Cost/token usage/duration appear
only where a call site genuinely reports them (native Agent() usage
snapshots; specialist subprocess streaming for the engine path remains
out of scope until a later slice).

INVARIANT 2 (durable run evidence) adds a durable PATH for that same
log (durable_event_log_path, below) and nothing else: same class, same
append-only JSONL schema, same events, same non-authoritative status.
Orchestration/routing/retry/governance still never read it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

EVENT_TYPES = frozenset({
    "run_started",
    "node_ready",
    "node_started",
    "node_activity",
    "node_completed",
    "node_failed",
    "retry_started",
    "retry_exhausted",
    "join_evaluated",
    "run_terminal",
})


class RunEventLogError(RuntimeError):
    pass


def new_run_id() -> str:
    return uuid.uuid4().hex


PROJECT_LOCAL_RUNS_DIR = Path("_studio/.local/runs")


def default_event_log_path(run_id: str) -> Path:
    """The contracts.md-speced _studio/.local/runs/<id>/ layout, matching
    WorkOrderRun.default_state_path's/default_retry_ledger_path's own
    convention -- a run_id and a work_id are both just "runs" from an
    Operator's point of view.

    RELATIVE, and therefore resolved against whatever directory the
    orchestrator process happens to be running in. For a governed run
    that is the run's own DISPOSABLE worktree, so a log written here
    dies with it (INVARIANT 2 below). Kept unchanged -- it is still the
    correct project-local location for a process running in a durable
    checkout, and every pre-INVARIANT-2 log still lives here -- but it
    is no longer what the governed orchestrator's own `main()` uses."""
    return PROJECT_LOCAL_RUNS_DIR / run_id / "events.jsonl"


# --------------------------------------------------------------------
# INVARIANT 2 (durable run evidence)
#
# A governed run's entire evidence trail was destroyed when its
# disposable worktree disappeared: the log above is relative, so it was
# written INSIDE that worktree (and `.gitignore`'s `_studio/.local/`
# meant it was untracked too). The root cause of the disappearance is
# unconfirmed and explicitly out of scope; this is durability only.
#
# No second logger, schema, store, or evidence architecture is
# introduced. RunEventLog is already path-agnostic -- durability is a
# property of HOW it is constructed -- so this reuses its existing
# `path_for=` seam and only supplies a different, durable path.
#
# The anchor is this project's own established state convention, used
# identically three times already (studio_migration_apply.py:14-18
# purpose "migrations", and the two repo-only pilot harnesses), and
# documented in docs/development/testing.md and CONTRIBUTING.md):
#
#     $XDG_STATE_HOME (else ~/.local/state) / studio / <purpose>
#         / <project-key>            .resolve()d
#
# with purpose "runs" here. It survives worktree destruction for one
# reason: it is an absolute root outside every git checkout and every
# worktree, so removing a task worktree cannot reach it.
#
# THE TRAP: all three existing uses key on `Path(project).resolve()`.
# Inside a disposable worktree that resolves to the WORKTREE, which
# would give every run its own ephemeral key and scatter the evidence
# -- defeating the purpose. So the key here is derived from the STABLE
# repo root instead (see _repo_root below).
#
# Like every existing use, `state_base` is overridable so tests never
# write to real host state.
# --------------------------------------------------------------------

DURABLE_STATE_PURPOSE = "runs"

# Same bound studio_worktree_gate.py uses for its own rev-parse probe.
_GIT_PROBE_TIMEOUT_SECONDS = 5


def _git_binding(cwd: Path) -> dict | None:
    """Git's own `--git-dir`/`--git-common-dir` answer for `cwd`, both
    normalized to real absolute paths -- or None if git cannot give a
    clean answer.

    This is the same probe, with the same hardening, that
    studio_worktree_gate._is_isolated() (:196-217) already uses and that
    workspace_loss_reason() already depends on: one `git rev-parse
    --git-dir --git-common-dir` call, a GIT_*-stripped environment (an
    inherited GIT_DIR/GIT_COMMON_DIR silently retargets the answer), a
    timeout, and realpath-join of both answers against `cwd`.

    It is not called THROUGH that module: the gate exposes only the
    boolean `_is_isolated()` predicate, never the common dir this needs,
    and studio_run_events cannot import studio_specialist_invocation
    (which owns the gate loader) because that module already imports
    RunEventLog from here -- it would be circular.
    """
    probe_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT_SECONDS,
            env=probe_env,
        )
        if probe.returncode != 0:
            return None
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if len(lines) != 2:
            return None
        return {
            "git_dir": os.path.realpath(os.path.join(str(cwd), lines[0])),
            "git_common_dir": os.path.realpath(os.path.join(str(cwd), lines[1])),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _resolved(start) -> Path:
    return (Path(start) if start is not None else Path.cwd()).resolve()


def _repo_root(cwd: Path, binding: dict | None) -> tuple[Path, str]:
    """(stable repo root, how it was derived).

    Measured (git 2.x, this platform): from a linked worktree,
    `--git-common-dir` answers the SHARED checkout's absolute `.git`
    (e.g. /repo/.git) while `--git-dir` answers that worktree's own
    admin dir; from the shared checkout itself both answer the same
    `.git`. So the parent of the common dir is the same stable path
    from every worktree of a repository -- which is exactly the
    property the project key needs.

    Anything else degrades HONESTLY to the resolved path itself (the
    plain `Path(project).resolve()` the other three state-root uses
    already do): git unavailable/unclean, not a repository at all, or a
    common dir that is not named `.git` (a bare repository, a
    submodule's .git/modules/<name>, `git init --separate-git-dir`).
    The derivation used is reported as `repo_root_source` so an
    Operator can tell the two apart in the evidence rather than
    guessing.
    """
    if binding is not None:
        common = Path(binding["git_common_dir"])
        if common.name == ".git":
            return common.parent, "git_common_dir"
    return cwd, "resolved_path"


def stable_repo_root(project=None) -> Path:
    """The repository root that stays the same across every worktree of
    a repository -- the shared/main checkout -- for `project` (default:
    this process's own cwd)."""
    cwd = _resolved(project)
    return _repo_root(cwd, _git_binding(cwd))[0]


def orchestrator_binding(project=None) -> dict:
    """Where the orchestrator process ITSELF is running, in git's own
    terms. Recorded nowhere before INVARIANT 2, and exactly the fact
    needed to reason about a run whose workspace disappeared.

    `is_linked_worktree` is the same comparison
    studio_worktree_gate._is_isolated() (:215) makes -- git_dir !=
    git_common_dir -- and is None, not False, when git could not answer
    at all: unknown is reported as unknown, never as a fact.
    """
    cwd = _resolved(project)
    binding = _git_binding(cwd)
    root, source = _repo_root(cwd, binding)
    return {
        "cwd": str(cwd),
        "git_dir": binding["git_dir"] if binding is not None else None,
        "git_common_dir": binding["git_common_dir"] if binding is not None else None,
        "repo_root": str(root),
        "repo_root_source": source,
        "is_linked_worktree": (
            None if binding is None else binding["git_dir"] != binding["git_common_dir"]
        ),
    }


def durable_runs_root(*, project=None, state_base: Path | None = None,
                      purpose: str = DURABLE_STATE_PURPOSE) -> Path:
    """The absolute, out-of-checkout directory this project's run
    evidence lives under. Same construction as
    MigrationApplier.__init__ (studio_migration_apply.py:14-18),
    including the `state_base` test override, except that the project
    key is hashed from the STABLE repo root rather than from the
    possibly-disposable working directory. `purpose` lets other durable
    evidence (#167: Factory evidence) share this one key derivation."""
    base = (
        Path(state_base)
        if state_base is not None
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "studio"
        / purpose
    )
    project_key = hashlib.sha256(str(stable_repo_root(project)).encode()).hexdigest()[:20]
    return (base / project_key).resolve()


def durable_event_log_path(run_id: str, *, project=None, state_base: Path | None = None) -> Path:
    """The durable `<run_id>/events.jsonl` path, keeping the existing
    per-run layout unchanged so the same reader still works. Its
    signature is deliberately `Callable[[str], Path]`-compatible, so it
    can be handed straight to `RunEventLog(run_id, path_for=...)`
    without any factory or wrapper."""
    return durable_runs_root(project=project, state_base=state_base) / run_id / "events.jsonl"


def run_log_search_roots(*, project=None, state_base: Path | None = None) -> list[Path]:
    """Both directories a run's events.jsonl may live under, durable
    first: the durable root (every run started after INVARIANT 2) and
    the project-local one (every earlier run, and any caller still
    injecting the project-local default). Readers search both so
    durability is not a one-way break in the existing status tooling."""
    project_local = (
        Path(project) / PROJECT_LOCAL_RUNS_DIR if project is not None else PROJECT_LOCAL_RUNS_DIR
    )
    return [durable_runs_root(project=project, state_base=state_base), project_local]


def resolve_event_log_path(run_id: str, *, project=None, state_base: Path | None = None) -> Path:
    """Where `run_id`'s events actually are: the durable log when it
    exists, otherwise the unchanged project-local path. Read-side only
    -- nothing here decides anything about a run."""
    durable = durable_event_log_path(run_id, project=project, state_base=state_base)
    if durable.is_file():
        return durable
    project_local = default_event_log_path(run_id)
    return Path(project) / project_local if project is not None else project_local


class RunEventLog:
    """Append-only JSONL writer/reader for one run's lifecycle events."""

    def __init__(self, run_id: str, *, path_for: Callable[[str], Path] = default_event_log_path):
        self.run_id = run_id
        self.path = Path(path_for(run_id))

    def emit(self, event: str, *, work_id: str | None = None, role: str | None = None,
             detail: dict | None = None) -> dict:
        if event not in EVENT_TYPES:
            raise RunEventLogError(f"unknown lifecycle event: {event}")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "work_id": work_id,
            "role": role,
            "event": event,
            "detail": detail or {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def read_all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
