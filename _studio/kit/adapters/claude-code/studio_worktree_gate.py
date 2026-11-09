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

Finish gate (A2), wired as a third PreToolUse entry with matcher
"SubagentHandback" on this same script. Spiked on Claude Code 2.1.319: a
subagent hands its report back through the SubagentHandback tool, and a
SubagentStop block fires only after that report has been delivered, so it
cannot hold back unfinished work; frontmatter `hooks:` never ran at all.
Denying the SubagentHandback call does return control to the subagent with
the reason. For an agency-coder/agency-resolver whose brief carries the
`studio-work-order-id:` marker, the handback must contain the specialist-
result JSON for that work_id with status `completed`, one `done` claim with
evidence per `- [id]` criterion in the brief, and a clean `git status`;
status `blocked`/`failed` with a summary passes at once. Mode comes from
STUDIO_FINISH_GATE (`shadow` by default: record, never block; `enforce`;
`off`); in enforce mode a delegation is blocked at most three times. Every
decision is appended to a per-session JSONL ledger. Contract:
docs/release/certification/dogfood/finish-gate/CONTRACT.md.

Open-work-order Stop gate, wired as `Stop` and `SubagentStop` entries on
this same script. A role rule is advice the Director can drift past; this
makes ending a Director turn with delegated work still unaccounted for a
blocked action instead. Scope: the Director's own session only -- a
SubagentStop whose `agent_type` is agency-director (the Front Door's
normal Agent(agency-director, ...) route), or a Stop whose session root is
agency-director (`claude --agent agency-director`). Every work order the
Director delegated to a write-class specialist with a
`studio-work-order-id: <id>` line is open until the Director's final
message carries `studio-work-order-status: <id> <STATUS>` for it, with
STATUS one of WORK_ORDER_TERMINAL_STATUSES. The work order's own state
file is not consulted: on the native Agent() path nothing ever records a
work order as closed, so the declaration is the closure record. Payload
fields used here (`stop_hook_active`, `last_assistant_message`,
`agent_type`, `agent_transcript_path`) were captured from a live Claude
Code 2.1.318 session. Fails open on anything unreadable, and gives up
after MAX_STOP_BLOCKS consecutive blocks so a confused Director is never
trapped in an endless continuation loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

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
STOP_EVENTS = frozenset({"Stop", "SubagentStop"})
# The marker line studio_scope_context.WORK_ORDER_ID_MARKER defines; same
# stdlib-only duplication, pinned by tests/test_work_order_stop_gate.py.
WORK_ORDER_ID_LINE = re.compile(
    r"(?m)^studio-work-order-id:[ \t]+([A-Za-z0-9][A-Za-z0-9._-]{0,80})[ \t]*$"
)
# Column 0 only, after _strip_fenced_blocks has removed what it detects as
# fenced blocks (its own fence rules, not a full CommonMark parser).
WORK_ORDER_STATUS_LINE = re.compile(
    r"(?m)^studio-work-order-status:[ \t]+([A-Za-z0-9][A-Za-z0-9._-]{0,80})[ \t]+(\S+)[ \t]*$"
)
# CommonMark indentation: a fence line is indented at most 3 spaces; a tab or
# a 4th space makes it indented-code text, not a fence.
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
CLOSING_LINE_RULE = "at the start of a line and outside any code block"


def _strip_fenced_blocks(text: str, *, keep_unclosed: bool = False) -> str:
    """Drop fenced code blocks: fence lines are indented at most 3 spaces, a
    backtick opener's info string may not contain a backtick (so an inline
    ```x``` span is not an opener), and a closer is a fence of the same
    character, at least as long, with nothing else on its line. By default
    an unclosed fence runs to the end, so no status line after it counts; a
    missing line then blocks, but a hidden line can also hide a conflict.
    keep_unclosed treats an opener that never closes as plain text instead,
    for briefs, so only a fence that closes can hide a real marker."""
    lines = text.split("\n")
    closers = [FENCE_CLOSE.match(line) for line in lines]
    # Longest closer of each fence character strictly after each line.
    longest_after: list[dict[str, int]] = [{}] * len(lines)
    if keep_unclosed:
        best = {"`": 0, "~": 0}
        for index in range(len(lines) - 1, -1, -1):
            longest_after[index] = dict(best)
            if closers[index]:
                fence_text = closers[index].group(1)
                best[fence_text[0]] = max(best[fence_text[0]], len(fence_text))
    kept: list[str] = []
    fence = None
    for index, line in enumerate(lines):
        if fence is None:
            opener = FENCE_OPEN.match(line)
            if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
                candidate = opener.group(1)
                if not keep_unclosed or longest_after[index][candidate[0]] >= len(candidate):
                    fence = candidate
                    continue
            kept.append(line)
            continue
        closer = closers[index]
        if closer and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= len(fence):
            fence = None
    return "\n".join(kept)


WORK_ORDER_TERMINAL_STATUSES = ("DONE", "BLOCKED", "OPERATOR_INPUT_REQUIRED")
MAX_STOP_BLOCKS = 3
TRANSCRIPT_SCAN_LINES = 50
MAX_LINEAGE_HOPS = 20
GIT_PROBE_TIMEOUT_SECONDS = 5
HANDBACK_TOOL = "SubagentHandback"
FINISH_GATE_ENV = "STUDIO_FINISH_GATE"
FINISH_GATE_LEDGER_ENV = "STUDIO_FINISH_GATE_LEDGER_DIR"
FINISH_GATE_MAX_DENIES = 3
_CRITERION_ID_RE = re.compile(r"(?m)^[ \t]*-[ \t]*\[([A-Za-z0-9][A-Za-z0-9._-]{0,40})\][ \t]+\S")
_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.S)


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


def _subagent_brief(payload: dict) -> str | None:
    """The delegated brief: the first user record of this subagent's own transcript."""
    transcript, agent_id = payload.get("transcript_path"), payload.get("agent_id")
    if not isinstance(transcript, str) or not transcript.endswith(".jsonl"):
        return None
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", agent_id):
        return None
    root = os.path.join(transcript[: -len(".jsonl")], "subagents")
    name = f"agent-{agent_id}.jsonl"
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        path = next((os.path.join(d, name) for d, _, files in os.walk(root) if name in files), "")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "user":
                    continue
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        block["text"] for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    )
                return None
    except OSError:
        return None
    return None


def _specialist_result(message: object) -> dict | None:
    """The whole message as one JSON object, else its last ```json block."""
    if not isinstance(message, str):
        return None
    try:
        parsed = json.loads(message)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        blocks = _JSON_FENCE_RE.findall(message)
        if not blocks:
            return None
        try:
            parsed = json.loads(blocks[-1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _claim_problems(claims: object, criterion_ids: list[str]) -> list[str]:
    if not isinstance(claims, list):
        return [f"acceptance_claims must list one claim per criterion id {criterion_ids}"]
    problems, seen = [], set()
    for claim in claims:
        cid = claim.get("id") if isinstance(claim, dict) else None
        cid = cid.strip() if isinstance(cid, str) else ""
        if cid not in criterion_ids:
            problems.append(f"claim {cid or claim!r} names no criterion in the brief")
            continue
        if cid in seen:
            problems.append(f"criterion {cid} has more than one claim")
            continue
        seen.add(cid)
        status = claim.get("status")
        if status == "not_done":
            problems.append(f"criterion {cid} is not_done: finish it, or hand back with status blocked")
        elif status != "done":
            problems.append(f"criterion {cid} claim status is {status!r}, not 'done'")
        evidence = claim.get("evidence")
        if not (isinstance(evidence, list) and evidence
                and all(isinstance(e, str) and e.strip() for e in evidence)):
            problems.append(f"criterion {cid} claim has no evidence list")
    missing = [cid for cid in criterion_ids if cid not in seen]
    if missing:
        problems.append(f"no claim for criteria {missing}")
    return problems


def _dirty_tree(cwd: object) -> str | None:
    """None when `git status --porcelain` in cwd is empty, else why it is not clean."""
    path = _normalize_cwd(cwd)
    if path is None or not os.path.isdir(path):
        return "cannot verify the worktree is clean: cwd is missing or unusable"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        probe = subprocess.run(
            ["git", "status", "--porcelain"], cwd=path, capture_output=True,
            text=True, timeout=GIT_PROBE_TIMEOUT_SECONDS, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return "cannot verify the worktree is clean: git status did not run"
    if probe.returncode != 0:
        return "cannot verify the worktree is clean: git status failed"
    dirty = [line[3:] for line in probe.stdout.splitlines() if line.strip()]
    if dirty:
        shown = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        return f"the worktree is not clean (commit or remove: {shown})"
    return None


def _finish_verdict(result: dict | None, work_id: str, criterion_ids: list[str],
                    cwd: object) -> tuple[str, list[str]]:
    """("allow" | "escape" | "deny", reasons): every failing contract rule, in rule order.

    One deny names all problems so a specialist can fix them in one round
    instead of spending the three-block budget on one rule at a time.
    """
    reasons: list[str] = []
    status = None
    if result is None:
        reasons.append("the handback must contain the specialist-result JSON object "
                       "(as the whole message or a ```json block)")
    else:
        if result.get("work_id") != work_id:
            reasons.append(f"result work_id {result.get('work_id')!r} is not this work order {work_id!r}")
        status = result.get("status")
        if status in ("blocked", "failed"):
            summary = result.get("summary")
            if not (isinstance(summary, str) and summary.strip()):
                return "deny", reasons + [f"status {status} needs a non-blank summary saying why"]
            if not reasons:
                return "escape", [f"status {status}: {summary.strip()[:200]}"]
            return "deny", reasons
        if status != "completed":
            reasons.append(f"result status {status!r} must be completed, blocked or failed")
        if criterion_ids:
            reasons.extend(_claim_problems(result.get("acceptance_claims"), criterion_ids))
    dirty = _dirty_tree(cwd)
    if dirty:
        reasons.append(dirty)
    return ("deny", reasons) if reasons else ("allow", [])


def _ledger_path(session_id: object) -> str:
    base = os.environ.get(FINISH_GATE_LEDGER_ENV) or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"),
        "agency-workflow-kit", "finish-gate",
    )
    name = re.sub(r"[^A-Za-z0-9._-]", "", session_id if isinstance(session_id, str) else "")
    return os.path.join(base, f"{name or 'unknown-session'}.jsonl")


def _ledger_usable(path: str) -> bool:
    """A missing file or a regular file; anything else (a FIFO, a directory) would block or fail."""
    return not os.path.lexists(path) or os.path.isfile(path)


def _prior_denies(path: str, agent_id: object) -> int:
    if not _ledger_usable(path):
        return 0
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError):
        return 0
    return sum(1 for r in rows if isinstance(r, dict) and r.get("decision") == "deny"
               and r.get("agent_id") == agent_id)


def _append_ledger(path: str, record: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not _ledger_usable(path):
            return
        # O_NONBLOCK: a FIFO swapped in after the check fails fast instead of hanging.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass  # a ledger failure never changes the decision


def _finish_gate(payload: dict) -> int:
    mode = os.environ.get(FINISH_GATE_ENV)
    if mode == "off":
        return 0
    if mode != "enforce":
        mode = "shadow"
    agent_type = payload.get("agent_type")
    if agent_type not in WRITE_CLASS_AGENTS:
        return 0
    # Same marker reading as the Stop gate: a closed fence hides example ids and criteria.
    brief = _strip_fenced_blocks(_subagent_brief(payload) or "", keep_unclosed=True)
    markers = set(WORK_ORDER_ID_LINE.findall(brief))
    if len(markers) != 1:
        return 0
    work_id = markers.pop()
    criterion_ids = list(dict.fromkeys(_CRITERION_ID_RE.findall(brief)))
    tool_input = payload.get("tool_input")
    message = tool_input.get("message") if isinstance(tool_input, dict) else None
    verdict, reasons = _finish_verdict(_specialist_result(message), work_id, criterion_ids,
                                       payload.get("cwd"))
    ledger = _ledger_path(payload.get("session_id"))
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
        "agent_type": agent_type, "work_id": work_id, "mode": mode, "reasons": reasons,
    }
    if verdict != "deny":
        _append_ledger(ledger, {**record, "decision": verdict})
        return 0
    if mode == "shadow":
        _append_ledger(ledger, {**record, "decision": "would_deny"})
        return 0
    denies = _prior_denies(ledger, payload.get("agent_id"))
    if denies >= FINISH_GATE_MAX_DENIES:
        _append_ledger(ledger, {**record, "decision": "loop_guard_release"})
        return 0
    _append_ledger(ledger, {**record, "decision": "deny"})
    sys.stderr.write(
        f"FINISH GATE: {agent_type} cannot hand back work order {work_id} yet: "
        + "; ".join(reasons)
        + '. Fix this and hand back again, or hand back with status "blocked" and a '
        f"summary saying why (block {denies + 1} of {FINISH_GATE_MAX_DENIES}).\n"
    )
    return 2


def _delegated_work_orders(transcript_path: object) -> list[str] | None:
    """Work ids, in first-delegation order, that this transcript's own
    Agent calls handed to a write-class specialist with a marker line.
    None when the transcript cannot be read (callers fail open)."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    found: list[str] = []
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                if "studio-work-order-id:" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                content = (record.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not (isinstance(item, dict) and item.get("type") == "tool_use"
                            and item.get("name") in (AGENT_TOOL, "Task")):
                        continue
                    tool_input = item.get("input")
                    if not isinstance(tool_input, dict):
                        continue
                    if tool_input.get("subagent_type") not in WRITE_CLASS_AGENTS:
                        continue
                    # A fenced example marker in a brief is not a delegation.
                    brief = _strip_fenced_blocks(str(tool_input.get("prompt") or ""), keep_unclosed=True)
                    for work_id in WORK_ORDER_ID_LINE.findall(brief):
                        if work_id not in found:
                            found.append(work_id)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return found


def _last_assistant_text(transcript_path: object) -> str:
    """Fallback when a payload has no last_assistant_message."""
    text = ""
    if not isinstance(transcript_path, str) or not transcript_path:
        return text
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                if '"assistant"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                content = (record.get("message") or {}).get("content")
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    if any(parts):
                        text = "\n".join(parts)
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    return text


def _open_work_orders(work_ids: list[str], final_message: str) -> list[str]:
    """Problems that keep the turn open: a delegated work order with no
    terminal status line, conflicting status lines, or a status outside the
    allowed set."""
    declared: dict[str, set[str]] = {}
    for work_id, status in WORK_ORDER_STATUS_LINE.findall(_strip_fenced_blocks(final_message or "")):
        declared.setdefault(work_id, set()).add(status)
    problems = []
    for work_id in work_ids:
        statuses = declared.get(work_id)
        if not statuses:
            problems.append(f"{work_id}: no status line")
        elif len(statuses) > 1:
            problems.append(f"{work_id}: conflicting statuses {sorted(statuses)}")
        elif (status := next(iter(statuses))) not in WORK_ORDER_TERMINAL_STATUSES:
            problems.append(f"{work_id}: unknown status {status!r}")
    return problems


def _stop_counter_path(payload: dict) -> str:
    key = f"{payload.get('session_id')}:{payload.get('agent_id')}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(tempfile.gettempdir(), "agency-workflow-kit-stop-gate", digest)


def _check_stop(payload: dict) -> int:
    if payload.get("hook_event_name") == "SubagentStop":
        if payload.get("agent_type") != GATED_AGENT:
            return 0
        transcript = payload.get("agent_transcript_path")
    else:
        transcript = payload.get("transcript_path")
        if (payload.get("agent_type") != GATED_AGENT
                and _root_agent_type(transcript) != GATED_AGENT):
            return 0

    work_ids = _delegated_work_orders(transcript)
    if not work_ids:
        return 0
    final_message = payload.get("last_assistant_message")
    if not isinstance(final_message, str):
        final_message = _last_assistant_text(transcript)
    problems = _open_work_orders(work_ids, final_message)

    counter = _stop_counter_path(payload)
    blocks = 0
    if payload.get("stop_hook_active") is True:
        try:
            with open(counter, encoding="utf-8") as handle:
                blocks = int(handle.read().strip() or 0)
        except (OSError, ValueError):
            blocks = 0
    if not problems or blocks >= MAX_STOP_BLOCKS:
        try:
            os.remove(counter)
        except OSError:
            pass
        return 0
    try:
        os.makedirs(os.path.dirname(counter), exist_ok=True)
        with open(counter, "w", encoding="utf-8") as handle:
            handle.write(str(blocks + 1))
    except OSError:
        if payload.get("stop_hook_active") is True:
            return 0  # cannot count consecutive blocks; never risk an endless loop
    statuses = "|".join(WORK_ORDER_TERMINAL_STATUSES)
    sys.stderr.write(
        "WORK ORDER GATE: you delegated work orders that your final message does not "
        f"close ({'; '.join(problems)}). Finish the open work, or end your final message "
        f"with one line per work order: `studio-work-order-status: <work_id> <{statuses}>`, "
        f"{CLOSING_LINE_RULE}. "
        "Use DONE only when its acceptance criteria are met with evidence, BLOCKED with the "
        "blocker stated, OPERATOR_INPUT_REQUIRED with the exact decision needed.\n"
    )
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") in STOP_EVENTS:
        return _check_stop(payload)
    if payload.get("tool_name") == AGENT_TOOL:
        reason = _agent_model_override(payload)
        if reason is None:
            return 0
        sys.stderr.write(reason)
        return 2
    if payload.get("tool_name") == HANDBACK_TOOL:
        return _finish_gate(payload)
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
