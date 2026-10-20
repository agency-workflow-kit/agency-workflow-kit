#!/usr/bin/env python3
"""PreToolUse:Bash hook: enforce native worktree isolation for agency-director.

Empirical finding: agency-director's only repository-mutation path is
Bash, and Claude Code's native worktree-isolation latch
(worktree.bgIsolation) blocks Edit/Write in the shared checkout but not
Bash. Without this gate, EnterWorktree is only a prompted convention --
a Director that skips it can still write straight into the shared
checkout on the real branch.

Second empirical finding: a Bash call delegated to a specialist reports
the SPECIALIST's own agent_type (e.g. "agency-coder"), not
"agency-director" -- captured from a live hook payload. Gating on the
current call's agent_type alone therefore misses exactly the escape
route this hook exists to close: a Director that delegates before
isolating. The original fix read the session's own transcript_path for
its root `{"type": "agent-setting", "agentSetting": ...}` record -- the
one place a real top-level `claude -p --agent ...` session records its
own launch identity -- and gated on that root identity.

Third empirical finding, from a live nested-Director interactive-
resumption pilot: a subagent spawned through this harness's own
background Agent-tool mechanism never gets an inline agent-setting
record in its own .jsonl transcript at all -- its identity instead
lives in a sidecar `agent-<id>.meta.json` next to it (`agentType`), and
a delegated agent's meta.json carries `parentAgentId` pointing at
whichever agent spawned it. The agent-setting scan above never finds
anything for this shape, so a specialist delegated by a Director whose
own worktree binding was lost ran Bash completely unblocked in the
shared checkout -- reproduced live and confirmed against the real
transcript artifact this pilot produced. `_root_agent_type_via_sidecar()`
closes this by walking the sidecar meta.json parentAgentId chain
(bounded, cycle-safe) for whether any ancestor is agency-director, and
is checked in addition to (never instead of) the agent-setting scan
above, so neither session shape regresses the other. agent_type is
still checked first as a fast path (the Director's own direct Bash
calls never need either lookup), and a chain that never reaches
agency-director (a standalone specialist dispatched on its own, or one
delegated by a non-Director agent) is left untouched.

Fourth empirical finding, from a live dogfood this session: isolation
used to be detected purely by the path substring "/.claude/worktrees/".
A genuinely isolated `git worktree add <arbitrary path>` was therefore
rejected as "not isolated" and the Director was blocked inside a
worktree it had correctly created (forcing an awkward nested-worktree
workaround), while a plain directory that merely *looked* like
`.claude/worktrees/x` was waved through. Isolation is now asked of Git
itself: `git rev-parse --git-dir --git-common-dir` run in the cwd, with
both answers normalized against that cwd -- a linked worktree is
exactly the case where those two differ (measured on git 2.55.0:
shared checkout `.git`/`.git`; linked worktree
`<main>/.git/worktrees/<name>` vs `<main>/.git`; a worktree of a
worktree likewise differs; a non-repo exits 128). Normalizing both is
load-bearing, not cosmetic: from a SUBDIRECTORY of the shared checkout
git answers with an ABSOLUTE --git-dir and a CWD-RELATIVE --git-common-
dir, so comparing the raw strings would report "isolated" and fail the
gate open in exactly the shared checkout it exists to block. So is
running the probe with a SANITIZED, GIT_*-stripped environment:
rev-parse honours ambient GIT_DIR / GIT_COMMON_DIR, and an independent
review reproduced the shared checkout answering `.git` /
`<other-repo>/.git` -- "isolated", gate open -- purely because the hook
process had inherited a GIT_COMMON_DIR. The old pathname check had no
environment sensitivity at all, so stripping GIT_* is what keeps the
move to rev-parse from trading one fail-open for another.

Why rev-parse rather than the much cheaper "is `.git` a file?" signal:
a submodule's `.git` is a file too (`gitdir: ../.git/modules/<name>`),
byte-for-byte the same on-disk shape as a linked worktree, so that
heuristic FALSELY ACCEPTS a submodule working directory as isolated --
fail-open, the one direction this gate must never fail. rev-parse gets
it right: for a submodule both answers are the same
`<outer>/.git/modules/<name>` path, so it is correctly not isolated.
Unlike every other check here, the isolation probe fails CLOSED: git
missing, timing out, exiting non-zero or answering unexpectedly all
mean "not isolated", i.e. a director-rooted session gets blocked.

Canonical source: this file is rendered into every fresh install at
`.claude/hooks/studio_worktree_gate.py` (see studio_renderer.py /
studio_installer.py) and wired into `.claude/settings.json` by
studio_settings_merge.py, additively and only when absent -- an
Operator who has already configured this hook, or an incompatible
value at the same setting, is never silently overwritten.

Wiring reference (installed path, via $CLAUDE_PROJECT_DIR: a plain
relative path breaks once EnterWorktree has switched the session's cwd
into a worktree that doesn't contain this script -- confirmed by a
real pilot; Claude Code itself detects and reports this):

    "hooks": {
      "PreToolUse": [
        {"matcher": "Bash", "hooks": [
          {"type": "command",
           "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/studio_worktree_gate.py\""}
        ]}
      ]
    }

Guardrail, not a security boundary: repo-local config can disable it.
Fails open (exit 0) on any payload/transcript it cannot read or parse,
or that falls outside its narrow scope, so a broken or unrelated
invocation never blocks a tool, agent or session that isn't a
director-rooted background session. The isolation probe is the one
deliberate exception, and only once a session is already known to be
director-rooted: it fails closed (see _is_isolated).

Fifth finding (ADR 0004, Front Door entry routing): the Director-
ancestry checks above assume a Director is always somewhere in a
gated session's lineage, but that stopped being the only mutation
path once entry classification moved to the Front Door. A DELEGATE
decomposition spawns a write-class specialist (agency-coder,
agency-resolver) directly via Agent(..., isolation: "worktree") --
with no Director anywhere in its ancestry at all, by design: this is
the canonical NON-governed mutation path, and DELEGATE never creates
or delegates to a Director. Ancestry-only gating therefore misses it
completely: a standalone write-class specialist ran Bash unblocked in
the shared checkout with no Director in sight. Unlike a Director,
neither agency-coder nor agency-resolver has an EnterWorktree tool at
all (see studio_renderer.ROLE_TOOLS), so they cannot self-isolate --
they can only be BORN isolated by whoever spawns them. This gate
cannot make that happen; it can only fail closed and say so.
WRITE_CLASS_AGENTS closes this, purely additively: gate if ancestry
says director-rooted (unchanged, in full) OR if the current call's own
agent_type names a write-class agent -- nothing gated today becomes
ungated, and every existing Director-ancestry path (including a
Director-delegated specialist reached only through the transcript/
sidecar chain) is preserved exactly as before.

agency-researcher and agency-critic are deliberately NOT in
WRITE_CLASS_AGENTS: researcher's own role contract carries no Bash
tool at all (studio_renderer.ROLE_TOOLS), and critic is contractually
non-mutating and must remain free to run standalone verification
commands (Bash) without being forced into a worktree it will never
write to. route_entry() already assigns both isolation: None for
exactly this reason -- this gate agrees with that decision rather than
second-guessing it.

Model-pin gate (#116), wired as a second PreToolUse entry with matcher
"Agent" on this same script. Claude Code resolves a subagent's model
per-invocation `model` parameter first, frontmatter second, so a
caller's `Agent(subagent_type="agency-coder", model="sonnet")` silently
replaced the reviewed `claude-sonnet-5` pin with `claude-sonnet-5-5` in
the #112 D1 live row. P §23 / §25.7 allow a per-invocation override only
as a recorded escalation, and no such mechanism exists, so any `model`
on an Agent call to a Kit-pinned agent is blocked (exit 2) with the
reason; the caller retries without it and the frontmatter pin applies.
No alias is normalized and no value is stripped silently. Agents
outside MODEL_PINNED_AGENTS are left alone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

GATED_AGENT = "agency-director"
GATED_TOOL = "Bash"

# Write-class specialist identities: agents whose own role contract grants
# a mutation-capable tool (Edit/Write/NotebookEdit, per
# studio_renderer.ROLE_TOOLS) and
# therefore have no EnterWorktree tool of their own to self-isolate with.
# Duplicated here rather than imported because this file is rendered
# stdlib-only into external projects, where studio_renderer is not
# importable -- tests/test_worktree_gate.py pins this set against
# studio_renderer.ROLE_TOOLS so the duplicate cannot silently drift.
# agency-researcher (no Bash tool at all) and agency-critic (contractually
# non-mutating, must remain free to run standalone verification Bash) are
# deliberately excluded.
WRITE_CLASS_AGENTS = frozenset({"agency-coder", "agency-resolver"})
# Every Kit agent with a reviewed frontmatter model pin (_studio/kit/
# manifest.json); duplicated for the same stdlib-only reason and pinned
# against the manifest by tests/test_model_governance.py.
MODEL_PINNED_AGENTS = frozenset({
    "agency-director", "agency-researcher", "agency-coder",
    "agency-resolver", "agency-critic",
})
AGENT_TOOL = "Agent"
TRANSCRIPT_SCAN_LINES = 50
MAX_LINEAGE_HOPS = 20
GIT_PROBE_TIMEOUT_SECONDS = 5


def _is_isolated(cwd: object) -> bool:
    """Ask Git, not the pathname, whether `cwd` sits in a linked worktree.

    A linked worktree is exactly the case where --git-dir and
    --git-common-dir differ; the shared checkout, a bare repo and a
    submodule working directory all report the same path for both. The
    submodule case is why this beats the cheaper "is `.git` a file?"
    heuristic, which a submodule satisfies identically to a linked
    worktree and would therefore fail OPEN on.

    Both answers are normalized against `cwd` before comparison because
    git mixes forms: from a subdirectory of the shared checkout
    --git-dir comes back absolute while --git-common-dir comes back
    relative to cwd, and a raw string compare would call that isolated.

    The probe runs with a SANITIZED environment -- every inherited GIT_*
    variable stripped -- because rev-parse honours them and they are
    attacker- and accident-reachable through whatever environment the
    hook process happens to inherit. Measured: with GIT_COMMON_DIR
    pointing at another repository, rev-parse in the shared checkout
    answers `.git` / `<other>/.git`, i.e. "isolated", failing this gate
    OPEN in exactly the checkout it exists to block; stripped, the same
    call answers `.git` / `.git`. GIT_DIR and GIT_COMMON_DIR set to a
    mismatched pair do the same. The old pathname check had no
    environment sensitivity at all, so this sanitizing is what keeps the
    move to rev-parse from being a fail-open regression.

    Fails CLOSED: any failure to get a clean, two-line answer (git
    missing, timeout, non-zero exit, unexpected output, unusable cwd)
    returns False, which blocks a director-rooted session rather than
    letting it run in the shared checkout.
    """
    if not isinstance(cwd, str) or not cwd:
        return False
    probe_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
            env=probe_env,
        )
        if probe.returncode != 0:
            return False
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if len(lines) != 2:
            return False
        git_dir = os.path.realpath(os.path.join(cwd, lines[0]))
        common_dir = os.path.realpath(os.path.join(cwd, lines[1]))
        return git_dir != common_dir
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _root_agent_type(transcript_path: object) -> str | None:
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for _, line in zip(range(TRANSCRIPT_SCAN_LINES), handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("type") == "agent-setting":
                    return record.get("agentSetting")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return None


def _root_agent_type_via_sidecar(transcript_path: object) -> str | None:
    """Empirical finding: a background Agent-tool spawn's own
    .jsonl transcript never carries an inline {"type": "agent-setting"}
    record at all (confirmed by inspecting real transcript files) --
    its identity lives only in a sidecar agent-<id>.meta.json next to
    it ("agentType"), and a delegated agent's meta.json carries
    "parentAgentId" pointing at whichever agent spawned it. Without
    this, a specialist delegated by a Director whose own worktree
    binding was lost ran Bash completely unblocked in the shared
    checkout (reproduced live). Walk the parentAgentId chain, bounded,
    to find whether any ancestor is agency-director."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    if not transcript_path.endswith(".jsonl"):
        return None
    current = transcript_path
    seen: set[str] = set()
    last_agent_type: str | None = None
    for _ in range(MAX_LINEAGE_HOPS):
        if current in seen:
            return None
        seen.add(current)
        meta_path = current[: -len(".jsonl")] + ".meta.json"
        try:
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict):
            return None
        last_agent_type = meta.get("agentType")
        if last_agent_type == GATED_AGENT:
            return last_agent_type
        parent_id = meta.get("parentAgentId")
        if not isinstance(parent_id, str) or not parent_id:
            return last_agent_type
        current = os.path.join(os.path.dirname(meta_path), f"agent-{parent_id}.jsonl")
    return None



_GIT_C_RE = re.compile(r"(?:^|[\s;|&])git(?:\s+-[^C\s]+)*\s+-C\s+(\S+)")
_GIT_ENV_RE = re.compile(
    r"(?:^|[\s;|&])(?:GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_COMMON_DIR)="
)


def _normalize_cwd(cwd: object) -> str | None:
    """Normalize a hook cwd. Returns None when malformed/unusable."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    text = cwd.strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    # Collapse . segments without resolving symlinks (path may not exist yet).
    parts = text.split("/")
    if any(part == ".." for part in parts):
        return None
    rebuilt: list[str] = []
    absolute = text.startswith("/")
    for part in parts:
        if part in {"", "."}:
            continue
        rebuilt.append(part)
    if absolute:
        return "/" + "/".join(rebuilt) if rebuilt else "/"
    if not rebuilt:
        return None
    return "/".join(rebuilt)


def _extract_bash_command(payload: dict) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    for key in ("command", "cmd"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _command_retargets_scm(command: str, cwd: str) -> str | None:
    """Return a reason if the Bash command overrides SCM targeting.

    Detects `git -C <path>`, GIT_DIR=/GIT_WORK_TREE=/GIT_INDEX_FILE=
    assignments, and `-C` targeting the linked worktree's common/main root.
    """
    if not command or not isinstance(command, str):
        return None
    if _GIT_ENV_RE.search(command):
        return "GIT_* override in command"
    match = _GIT_C_RE.search(command)
    if not match:
        return None
    target = match.group(1).strip("'\"")
    # Resolve relative -C targets against cwd.
    if not target.startswith("/"):
        target_abs = os.path.realpath(os.path.join(cwd, target))
    else:
        target_abs = os.path.realpath(target)
    # Compare against this cwd's common dir (main root) when probeable.
    probe_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
            env=probe_env,
        )
        if probe.returncode == 0:
            lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
            if len(lines) >= 2:
                toplevel = os.path.realpath(lines[0])
                common = os.path.realpath(os.path.join(cwd, lines[1]))
                # common dir's parent is typically the main checkout root
                main_root = os.path.dirname(common) if common.endswith("/.git") or common.endswith(".git") else common
                if target_abs in {toplevel, main_root, common} or target_abs.startswith(main_root + os.sep):
                    # Allow -C that still points inside the linked worktree cwd tree.
                    cwd_real = os.path.realpath(cwd)
                    if target_abs == cwd_real or target_abs.startswith(cwd_real + os.sep):
                        return None
                    return "git -C targets main/common checkout"
    except (OSError, ValueError, subprocess.SubprocessError):
        # Fail closed for write-class: any unclean probe while -C is present.
        return "git -C present and main-root probe failed"
    return "git -C retarget"



def _agent_model_override(payload: dict) -> str | None:
    """Return a block reason when an Agent call overrides a Kit model pin."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    subagent = tool_input.get("subagent_type")
    if subagent not in MODEL_PINNED_AGENTS:
        return None
    if tool_input.get("model") is None:
        return None
    return (
        f"MODEL PIN GATE: Agent(subagent_type={subagent!r}, model="
        f"{tool_input['model']!r}) is blocked. {subagent} runs on its reviewed "
        "frontmatter model pin; a per-invocation model override needs a recorded "
        "escalation and none exists. Retry the same Agent call without `model`.\n"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(payload, dict):
        return 0
    if payload.get("tool_name") == AGENT_TOOL:
        reason = _agent_model_override(payload)
        if reason is None:
            return 0
        sys.stderr.write(reason)
        return 2
    if payload.get("tool_name") != GATED_TOOL:
        return 0

    agent_type = payload.get("agent_type")
    is_write_class = agent_type in WRITE_CLASS_AGENTS

    # Resolve director ancestry only when not already write-class (same
    # cost ordering as before). Write-class / director sessions are gated;
    # everything else still fails open.
    is_director = False
    if not is_write_class:
        is_director = agent_type == GATED_AGENT
        if not is_director:
            is_director = _root_agent_type(payload.get("transcript_path")) == GATED_AGENT
        if not is_director:
            is_director = _root_agent_type_via_sidecar(payload.get("transcript_path")) == GATED_AGENT
    if not is_write_class and not is_director:
        return 0

    raw_cwd = payload.get("cwd")
    cwd = _normalize_cwd(raw_cwd)
    if cwd is None:
        # Write-class: never fail-open on malformed/missing cwd.
        # Director: preserve historical fail-open only when cwd is absent/empty;
        # a present but unusable cwd (e.g. containing '..') is never treated as safe.
        if is_write_class:
            sys.stderr.write(
                f"ISOLATION GATE: Bash is blocked for {agent_type}: malformed or "
                "unusable cwd (refusing fail-open for write-class).\n"
            )
            return 2
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            sys.stderr.write(
                "ISOLATION GATE: Bash is blocked for agency-director: malformed "
                "cwd (refusing to treat unusable cwd as safe).\n"
            )
            return 2
        return 0

    command = _extract_bash_command(payload)
    retarget = _command_retargets_scm(command, cwd)
    if retarget is not None:
        who = agent_type if is_write_class else GATED_AGENT
        sys.stderr.write(
            f"ISOLATION GATE: Bash is blocked for {who}: {retarget}.\n"
        )
        return 2

    if _is_isolated(cwd):
        return 0

    if is_write_class:
        sys.stderr.write(
            f"ISOLATION GATE: Bash is blocked in the shared checkout for {agent_type}. "
            "This agent cannot self-isolate -- stop and report to whoever delegated you "
            'that you must be spawned with isolation: "worktree" (e.g. via '
            'Agent(..., isolation="worktree")).\n'
        )
        return 2

    sys.stderr.write(
        "ISOLATION GATE: Bash is blocked in the shared checkout for "
        "agency-director. Call the EnterWorktree tool before any shell "
        "command.\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
