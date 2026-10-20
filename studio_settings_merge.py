#!/usr/bin/env python3
"""Additive, conflict-reporting composition of the proven Director
worktree-isolation recipe into an Operator's existing .claude/settings.json.

This module's own pure composition logic is deliberately independent of
StudioInstaller's journal/collision/rollback machinery: settings.json is an
Operator-owned, freely hand-edited file, not a Studio-generated asset that
can be journaled/replaced wholesale, so it is composed key-by-key here
rather than treated as a managed asset. StudioInstaller.apply() calls
into this module after the asset transaction commits, unless the caller
passes compose_settings=False. studio_update.update() passes False:
project-local update must not mutate settings.json. Activation's
global-root step passes False too: the recipe is project scope. Project
activation and the project-local direct installer CLI keep the default,
so the recipe lands only in a project's own .claude/settings.json. This
module is not part of the asset pipeline itself.

Per ADR 0004 (Front Door entry routing), `agent` is no longer a Workflow
Kit-owned required setting: activation must not force a plain, unaddressed
Claude Code session to resolve to `agency-director` by default -- entry-path
routing is now the Front Door's job (`studio_front_door.py`), not a
settings.json default. Composition therefore composes: `worktree.bgIsolation`,
`worktree.baseRef`, the `PreToolUse:Bash`/`PreToolUse:Agent` gate hooks, and
(#138) Claude Code's native `attribution` with `commit` and `pr` hidden. An Operator's own pre-existing
`agent` key, of any value EXCEPT the exact legacy default the Workflow Kit
itself used to write (`"agent": "agency-director"`, see LEGACY_DEFAULT_AGENT),
is passed through by `_clone(existing)` untouched and is never inspected,
rewritten, deleted, or reported as a conflict -- it simply isn't this
module's concern anymore. The one exception is a one-time migration, in the
same spirit as LEGACY_GATE_HOOKS below: an already-activated project whose
settings.json still carries that exact stale value is left broken forever
unless composition itself removes it, since nothing else ever revisits an
already-activated project's settings.json. That removal is reported back in
a separate `migrations` list rather than folded into `conflicts` (a
conflict means "a required setting was NOT applied"; this is the opposite --
a stale value WAS removed) or applied silently.

For each of the three Workflow Kit-owned required settings, composition is
strictly additive:

- absent -> install the required value;
- present and already equivalent -> leave unchanged;
- present with an incompatible explicit value -> preserve the Operator's
  value untouched and report a conflict, so a caller can never silently
  claim a successful production-ready install over a real contract gap.
"""

from __future__ import annotations

import json
from pathlib import Path

from studio_installer import _atomic_write

REQUIRED_BG_ISOLATION = "worktree"
REQUIRED_BASE_REF = "head"
GATE_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/studio_worktree_gate.py"'
GATE_MARKER = "studio_worktree_gate.py"
# (matcher, command) entries composition requires: the worktree Bash gate and,
# on the same script, the #116 model-pin gate for Agent delegations.
REQUIRED_GATE_HOOKS = (("Bash", GATE_COMMAND), ("Agent", GATE_COMMAND))
# Claude Code's native attribution setting with every text hidden: the
# canonical policy (CONTRIBUTING "Attribution policy", #138) forbids AI
# co-author/"Generated with" attribution in Kit and project commits and PRs.
# `false` is Claude Code's own shorthand for the same thing.
REQUIRED_ATTRIBUTION = {"commit": "", "pr": ""}

# The exact top-level `agent` value Workflow Kit itself used to compose into
# settings.json before ADR 0004 superseded it. Only this exact string is
# auto-migrated (removed) here -- an Operator's own "agent" value, of any
# other kind (including a different string, None, or a non-string value),
# is left alone untouched and unreported, exactly as documented above.
LEGACY_DEFAULT_AGENT = "agency-director"

# Exact (matcher, command) pairs Workflow Kit has itself shipped in the past
# for this gate and later superseded. Only an exact match is auto-migrated
# (removed) here -- an unrecognized command that merely mentions
# studio_worktree_gate.py is left alone and reported as a conflict, since it
# might be a deliberate Operator customization rather than our own stale
# artifact. The one entry below is the PR #43 vintage root-level path,
# superseded by GATE_COMMAND when the script moved to .claude/hooks/.
LEGACY_GATE_HOOKS = {
    ("Bash", 'python3 "$CLAUDE_PROJECT_DIR/studio_worktree_gate.py"'),
}


class SettingsMergeError(RuntimeError):
    pass


def _clone(value):
    return json.loads(json.dumps(value))


def compose_worktree_isolation_settings(existing: dict) -> dict:
    """Return {"settings": <composed dict>, "conflicts": [...], "migrations": [...]}.

    A non-empty `conflicts` list means at least one required setting was
    NOT installed because an incompatible explicit value already existed;
    the caller must surface this rather than report success.

    A non-empty `migrations` list means a stale artifact the Workflow Kit
    itself previously shipped (and has since superseded) was found and
    removed -- see LEGACY_DEFAULT_AGENT and LEGACY_GATE_HOOKS. This is
    distinct from a conflict: a migration means composition DID change
    something (removed a superseded value), not that it declined to apply a
    required one.
    """
    if not isinstance(existing, dict):
        raise SettingsMergeError("existing settings must be a JSON object")

    settings = _clone(existing)
    conflicts: list[dict] = []
    migrations: list[dict] = []

    if settings.get("agent") == LEGACY_DEFAULT_AGENT:
        del settings["agent"]
        migrations.append({"field": "agent", "removed": LEGACY_DEFAULT_AGENT})

    worktree = settings.get("worktree")
    if worktree is None:
        settings["worktree"] = {"bgIsolation": REQUIRED_BG_ISOLATION, "baseRef": REQUIRED_BASE_REF}
    elif not isinstance(worktree, dict):
        conflicts.append({
            "field": "worktree",
            "required": {"bgIsolation": REQUIRED_BG_ISOLATION, "baseRef": REQUIRED_BASE_REF},
            "existing": worktree,
        })
    else:
        worktree = dict(worktree)
        if "bgIsolation" not in worktree:
            worktree["bgIsolation"] = REQUIRED_BG_ISOLATION
        elif worktree["bgIsolation"] != REQUIRED_BG_ISOLATION:
            conflicts.append({
                "field": "worktree.bgIsolation",
                "required": REQUIRED_BG_ISOLATION,
                "existing": worktree["bgIsolation"],
            })
        if "baseRef" not in worktree:
            worktree["baseRef"] = REQUIRED_BASE_REF
        elif worktree["baseRef"] != REQUIRED_BASE_REF:
            conflicts.append({
                "field": "worktree.baseRef",
                "required": REQUIRED_BASE_REF,
                "existing": worktree["baseRef"],
            })
        settings["worktree"] = worktree

    attribution = settings.get("attribution")
    if attribution is None:
        settings["attribution"] = dict(REQUIRED_ATTRIBUTION)
    elif attribution is False:
        pass
    elif not isinstance(attribution, dict):
        conflicts.append({"field": "attribution", "required": dict(REQUIRED_ATTRIBUTION), "existing": attribution})
    else:
        attribution = dict(attribution)
        for field, required in REQUIRED_ATTRIBUTION.items():
            if field not in attribution:
                attribution[field] = required
            elif attribution[field] != required:
                conflicts.append({
                    "field": f"attribution.{field}",
                    "required": required,
                    "existing": attribution[field],
                })
        settings["attribution"] = attribution

    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
    elif not isinstance(hooks, dict):
        conflicts.append({"field": "hooks", "required": "object", "existing": hooks})
        hooks = None

    if hooks is not None:
        pre_tool_use = hooks.get("PreToolUse")
        if pre_tool_use is None:
            pre_tool_use = []
        elif not isinstance(pre_tool_use, list):
            conflicts.append({"field": "hooks.PreToolUse", "required": "array", "existing": pre_tool_use})
            pre_tool_use = None

        if pre_tool_use is not None:
            migrated_pre_tool_use = []
            for entry in pre_tool_use:
                if not isinstance(entry, dict):
                    migrated_pre_tool_use.append(entry)
                    continue
                entry_hooks = entry.get("hooks")
                if not isinstance(entry_hooks, list):
                    migrated_pre_tool_use.append(entry)
                    continue
                matcher = entry.get("matcher")
                kept_hooks = []
                for hook in entry_hooks:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    # LEGACY_GATE_HOOKS only ever contains (str, str) pairs, so a
                    # non-string matcher/command can never legitimately match --
                    # requiring both be strings here also keeps this membership
                    # test from ever attempting to hash an unhashable JSON value
                    # (a list/dict matcher or command) and crashing with a raw
                    # TypeError instead of going through this module's normal,
                    # never-raises-on-a-bad-value contract.
                    if (isinstance(matcher, str) and isinstance(command, str)
                            and (matcher, command) in LEGACY_GATE_HOOKS):
                        migrations.append({
                            "field": "hooks.PreToolUse",
                            "removed": {"matcher": matcher, "command": command},
                        })
                        continue
                    kept_hooks.append(hook)
                if kept_hooks:
                    migrated_pre_tool_use.append({**entry, "hooks": kept_hooks})
                # else: entry contained only a recognized obsolete gate hook -- drop it.
            pre_tool_use = migrated_pre_tool_use

            gate_present = False
            gate_matches = False
            required_present: set = set()
            for entry in pre_tool_use:
                if not isinstance(entry, dict):
                    continue
                entry_hooks = entry.get("hooks")
                if not isinstance(entry_hooks, list):
                    continue
                for hook in entry_hooks:
                    if isinstance(hook, dict) and GATE_MARKER in str(hook.get("command", "")):
                        gate_present = True
                        if entry.get("matcher") == "Bash" and hook.get("command") == GATE_COMMAND:
                            gate_matches = True
                        for required in REQUIRED_GATE_HOOKS:
                            if (entry.get("matcher"), hook.get("command")) == required:
                                required_present.add(required)

            if gate_present and not gate_matches:
                conflicts.append({
                    "field": "hooks.PreToolUse[worktree gate]",
                    "required": GATE_COMMAND,
                    "existing": "an existing PreToolUse hook already references "
                                "studio_worktree_gate.py with a different matcher/command",
                })
            else:
                pre_tool_use = list(pre_tool_use) + [
                    {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
                    for matcher, command in REQUIRED_GATE_HOOKS
                    if (matcher, command) not in required_present
                ]

            hooks = dict(hooks)
            hooks["PreToolUse"] = pre_tool_use
            settings["hooks"] = hooks

    return {"settings": settings, "conflicts": conflicts, "migrations": migrations}


def apply_worktree_isolation_settings(project_root: Path) -> dict:
    """Read <project_root>/.claude/settings.json (or {} if absent), compose
    the required recipe into it, write the result back atomically, and
    return the compose_worktree_isolation_settings() result so the caller
    can detect and report conflicts and migrations instead of assuming
    success."""
    path = Path(project_root) / ".claude" / "settings.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SettingsMergeError(f"invalid existing settings.json: {exc}") from exc
    else:
        existing = {}

    result = compose_worktree_isolation_settings(existing)
    data = (json.dumps(result["settings"], indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, data)
    return result
