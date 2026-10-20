# Claude Code Adapter — Phase 2 Runtime Contract

Status: CURRENT IMPLEMENTED ADAPTER and candidate runtime target. The
install/activation/update lifecycle is the reference certified behavior;
adapter-mediated live-turn execution is not fully certified. Cursor and
Codex/ChatGPT are not certified by this adapter. Provider-neutral runtime
architecture is tracked separately in GitHub issue #75.

## Inputs

The renderer consumes only canonical kit inputs: `manifest.json`, policy/contracts, five neutral role contracts, frozen core catalog, activated profile wrappers, schemas and reviewed project profile. Legacy Section 15 material is hostile migration input and never executable input.

## Required user-scope outputs

```text
~/.claude/
├── agents/
│   ├── agency-director.md
│   ├── agency-researcher.md
│   ├── agency-coder.md
│   ├── agency-resolver.md
│   └── agency-critic.md
├── skills/
│   └── studio-*/SKILL.md
├── hooks/
│   └── studio_worktree_gate.py
└── runtime/
    ├── studio_front_door.py
    ├── studio_director_bridge.py
    ├── studio_capability_router.py
    ├── studio_renderer.py
    ├── studio_work_order_plan.py
    ├── studio_director_mcp_grants.py
    └── _studio/kit/manifest.json
```

Core profile must generate exactly five agents and exactly four Studio-owned skills (`studio-gate`, `studio-verify`, `studio-run-engine`, `studio-grill` -- the frozen minimum-sufficient catalog; `studio_renderer._validate_manifest` hard-fails on any other count). WordPress+Etch adds exactly `studio-etch-author` and `studio-wp-audit`. `hooks/` and `runtime/` are rendered and installed the same way at user scope; each external project's own `.claude/` only needs the `hooks/` and `runtime/` subtrees (see "Project scope" below), never a project-local copy of `agents/` or core `skills/`. Renderer-profile skills (the WordPress+Etch pair) are the exception: they install only at project scope, selected by that project's reviewed `.studio/project-profile-extension.json`, and never at user scope (see `docs/release/install-update-recovery.md`, "Renderer profile and readiness").

## Agent invariants

- Every generated agent has an explicit reviewed model ID. `inherit`, omission, silent fallback and unexplained per-invocation override fail rendering/validation.
- Director: `claude-opus-5`, high effort, spawned by the Front Door on the DIRECTOR path only (`studio_front_door.py`, [ADR 0004](../../../../docs/decisions/0004-front-door-entry-routing.md)) rather than being every session's default identity, only agent with bounded Agent delegation to the four approved specialists.
- Researcher: `claude-sonnet-5`, medium effort, no Agent, read/research oriented.
- Coder: `claude-sonnet-5`, high effort, no Agent, bounded project implementation.
- Resolver: `claude-sonnet-5`, high effort, no Agent, bounded repair and max two failed correction cycles before escalation.
- Critic: `claude-opus-5`, high effort, no Agent, independent/fresh and non-mutating with respect to reviewed implementation.
- No generated `agency-operator.md`.
- No legacy specialist agent names.

## Current Claude Code model precedence

For current Claude Code (revalidated 2026-09-15), subagent model selection resolves in this order:

1. per-invocation `model` parameter;
2. subagent definition `model` frontmatter;
3. `CLAUDE_CODE_SUBAGENT_MODEL` environment default;
4. main conversation model.

Therefore a plain `CLAUDE_CODE_SUBAGENT_MODEL` is recorded but does not override Studio's explicit frontmatter. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` is a certification blocker because it forces the environment/main model across subagents and defeats Studio routing. This precedence is version-sensitive and must be revalidated against official Claude Code documentation at release time.

Effective runtime model evidence must not come from agent prose, self-reporting, declared frontmatter, or `/status` text. Trusted sources are role-bound and narrow:

- **Delegated specialists** (`agency-researcher`, `agency-coder`, `agency-resolver`, `agency-critic`): Claude Code `/tasks` only (`claude-/tasks`), which displays the model used by a running subagent.
- **Parent Director** (`agency-director`): authenticated Claude Code session JSONL `message.model` only (`claude-session-jsonl-message-model`), and only when the session `agentSetting` is exactly `agency-director` on a parent (non-sidechain) assistant message. `/tasks` is not required for the Director and does not satisfy it; `message.model` does not satisfy any specialist.

Studio records a complete exact-roster observation — four specialist `/tasks` rows plus the Director JSONL source — before model routing can pass certification.

## Delegation semantics

When the Director definition runs as the main thread via `claude --agent agency-director`, `Agent(agent_type)` is an allowlist for the approved specialist types. Specialists intentionally omit `Agent` entirely. Current Claude Code can allow subagents to spawn further subagents when `Agent` is available and the runtime depth limit permits it, so Studio's no-recursive-specialist policy is enforced by the specialist tool definitions rather than assumed as a platform invariant.

Runtime delegation evidence must be an observed Agent tool-use event. Prose claiming that delegation occurred is not evidence.

## Concurrency isolation (empirical, pinned Claude Code 2.1.274)

Real concurrent-pilot finding: two `claude --bg --agent agency-director` sessions launched the same way Agent View (`claude agents`) launches a task shared one cwd and one branch and both wrote real files into the shared checkout. `agency-director`'s only mutation path is Bash, and Claude Code's native `worktree.bgIsolation` latch blocks Edit/Write in the shared checkout but does not cover Bash, so a Director that never calls `EnterWorktree` is not stopped by that latch alone.

Studio closes this with three native pieces, none of them custom worktree management. All three, plus the default-routing key below, are now delivered automatically by a fresh install rather than hand-assembled per checkout:

1. **Tool contract**: `agency-director`'s generated frontmatter grants the native `EnterWorktree` tool, and its role contract requires calling it before any Bash use. This is the *enabler*, not the guarantee — a Director gated below without this tool would deadlock, unable to satisfy the gate. Renderer-delivered like every other agent contract; no separate packaging step needed.
2. **`studio_worktree_gate.py`** — canonical source at `_studio/kit/adapters/claude-code/studio_worktree_gate.py`, rendered by `studio_renderer.py` into every rendered output tree at `hooks/studio_worktree_gate.py`, and installed by `StudioInstaller` (a fresh third managed asset root alongside `agents/` and `skills/`, with the same journal/collision/rollback guarantees) to `.claude/hooks/studio_worktree_gate.py`. It's a `PreToolUse:Bash` hook that turns "call `EnterWorktree` first" from a prompted convention into a hard, runtime-enforced block. It denies (exit 2) a Bash call whose `cwd` is not inside a linked `git worktree`, for a *director-rooted* session — isolation is decided by Git itself (`git rev-parse --git-dir --git-common-dir` in that `cwd`, run with a sanitized environment that strips every inherited `GIT_*` variable, both answers normalized against the `cwd`; a linked worktree is exactly where the two differ), not by the `.claude/worktrees/` pathname convention, so a real worktree at any path counts and a lookalike directory does not. Stripping `GIT_*` is load-bearing, not hygiene: `rev-parse` honours an ambient `GIT_COMMON_DIR`/`GIT_DIR`, which otherwise makes the *shared* checkout answer as though it were isolated. That probe alone fails *closed* (an unavailable, slow or unhappy `git` means "not isolated"). A real pilot found a delegated tool call reports the **specialist's own** `agent_type` (e.g. `agency-coder`), not the Director's — so the gate cannot rely on `agent_type` alone to catch a specialist a Director delegated to before isolating. Instead it resolves the session's root identity from the first `{"type": "agent-setting", "agentSetting": ...}` record in the payload's own `transcript_path` (`agent_type` is checked first only as a fast path for the Director's own direct Bash calls). A background Agent-tool spawn's own `.jsonl` transcript never carries that inline agent-setting record at all — its identity instead lives in a sidecar `agent-<id>.meta.json` (`agentType`, with `parentAgentId` pointing at whichever agent spawned it) — so `_root_agent_type_via_sidecar()` additionally walks that bounded, cycle-safe `parentAgentId` chain for whether any ancestor is `agency-director`, checked in addition to (never instead of) the transcript scan, closing a bypass where such a delegated specialist ran Bash unblocked in the shared checkout. Scope is therefore: any Bash call — the Director's own or a delegated specialist's — inside a session whose root agent is `agency-director`. A standalone `agency-coder`/`agency-resolver`/`agency-critic` dispatch (not delegated from a Director) and unrelated Bash use elsewhere in the same repository are untouched; an *interactive* `agency-director` session is gated exactly the same as a background one, since the gate has no notion of background vs. interactive. The gate constrains **where** the shell runs, not **what** it writes — a Bash command that mutates the shared checkout by absolute path or via `git -C <shared-checkout-path>` from inside an already-isolated worktree is not blocked.
3. **Settings composition** (`studio_settings_merge.py`) — run automatically by `StudioInstaller.apply()` as the last step of the same call that installs the agents/skills/hooks assets above, against the Operator's own `.claude/settings.json`. Still never force-applied wholesale, and no longer a separate hand-written step either: a genuine fresh install delivers the full recipe in one call.
   ```json
   {
     "worktree": { "bgIsolation": "worktree", "baseRef": "head" },
     "hooks": {
       "PreToolUse": [
         {"matcher": "Bash", "hooks": [
           {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/studio_worktree_gate.py\""}
         ]}
       ]
     }
   }
   ```
   Per [ADR 0004](../../../../docs/decisions/0004-front-door-entry-routing.md), composition no longer writes an `"agent"` key: a plain, unaddressed Agent View task resolves its own entry path via the Front Door (`studio_front_door.py`, Operator -> Front Door -> DIRECT | DELEGATE | DIRECTOR) instead of being pre-routed to the Director by settings default. An Operator's own pre-existing `"agent"` key, of any value EXCEPT the exact legacy default the Workflow Kit itself used to write (`"agent": "agency-director"`, see `LEGACY_DEFAULT_AGENT`), is passed through untouched by composition and never reported as a conflict; that one exact legacy value is instead auto-migrated away, see the paragraph below. `bgIsolation: "worktree"` is Claude Code's own default; pinning it explicitly stops a future default change or a stray `"none"` from silently disabling isolation. `baseRef: "head"` fixes a separate empirically-observed defect: both `--worktree` and `EnterWorktree` otherwise branch new worktrees from `origin/<default-branch>`, not from the dispatching session's own branch, which would strand a Director working on a milestone branch on a tree that doesn't contain that branch's work. The hook command references the installed gate script via Claude Code's own `$CLAUDE_PROJECT_DIR` — a real pilot found that a plain relative path breaks once `EnterWorktree` has switched the session's cwd into a worktree that doesn't contain the script, which Claude Code itself detects and reports. A worktree also only ever contains committed branch content, which `baseRef` does not change: if Workflow Kit itself was installed into the current checkout as uncommitted working-tree state, a further worktree created from that branch — whether via `EnterWorktree` or a specialist's own `isolation: "worktree"` — will not contain the installed `.claude/agents/`, `.claude/skills/`, or `.claude/hooks/` files at all (observed directly: a nested worktree of a checkout whose `.claude/` was untracked had no `.claude/` directory). A Director running from such a throwaway install therefore cannot assume that a delegated specialist's isolated worktree carries the same installed kit on disk.

   Composition is strictly additive per key, never a wholesale overwrite: `compose_worktree_isolation_settings()` installs each Workflow Kit-owned required value (`worktree.bgIsolation`, `worktree.baseRef`, the Bash and Agent gate hooks, and `attribution.commit` / `attribution.pr` set to `""` — Claude Code's native switch for the canonical no-AI-attribution policy, #138; `"attribution": false` already counts as matching) only when it is absent, leaves it untouched when it already matches, and — if the Operator has explicitly set an incompatible value at that exact key (e.g. `"worktree.bgIsolation": "none"`, or an *unrecognized* gate command under the `Bash` matcher) — preserves the Operator's value exactly as written and reports a structured conflict instead of silently overwriting it or silently claiming a production-ready install. Every other key in the Operator's `settings.json`, including an Operator's own `"agent"` choice (except the one exact legacy value described below) and unrelated hooks on other matchers/events, is passed through untouched. Re-running composition is a no-op: it never appends a duplicate gate hook entry.

   One exception to "preserve on conflict": a `Bash`-matcher hook entry whose command exactly matches a command Workflow Kit has itself previously shipped and since superseded (currently: the pre-relocation root-level gate path from before the script moved under `.claude/hooks/`) is recognized as our own stale artifact, not an Operator customization, and is safely removed as part of composition — never left coexisting alongside the current gate entry where it could independently deny every Bash call in a Director-rooted session as a dangling reference to a script that no longer exists at that path. Only an exact match against a known-shipped command is ever auto-removed this way; anything else that merely mentions the gate script is treated as a conflict, per the paragraph above. The removal is reported back in a separate `migrations` list, not folded into `conflicts`.

   A parallel exception applies to the top-level `"agent"` key itself: `LEGACY_DEFAULT_AGENT` (`"agency-director"`) is the exact value Workflow Kit itself used to compose into `settings.json` before ADR 0004 superseded that default. Only this exact string is auto-migrated (removed) by composition; an Operator's own `"agent"` value of any other kind (a different string, `null`, or a non-string value) is left alone, untouched and unreported, exactly as the paragraph above states. The honest consequence: an Operator who had deliberately hand-set `"agent": "agency-director"` — not as a stale Workflow Kit artifact but as their own deliberate choice — will have it removed on the next composition, with no on-disk backup. Composition cannot distinguish "Workflow Kit's own stale default" from "an Operator's deliberate choice that happens to share the same value"; it is reported in the returned `migrations` list (`{"field": "agent", "removed": "agency-director"}`), not silently preserved, so a caller can surface it, but the value itself is gone from `settings.json` once composition runs.

This is a guardrail, not a security boundary. `worktree.bgIsolation: "none"` disables Claude Code's own native isolation latch for Edit/Write, and composition detects and preserves that explicit choice as a reported conflict rather than silently overriding it back to `"worktree"` — but it does **not** disable the Workflow Kit gate hook itself: `studio_worktree_gate.py`'s decision logic never reads `bgIsolation` at all, so the hook keeps enforcing its own Bash-blocking rule regardless of that setting. Removing the gate hook entry entirely is also not a durable way to disable it: composition treats an absent gate entry the same as a fresh install and reinstalls it on the next composition run, by design, since composition's job is "ensure this required value is present," not "respect a one-time deletion." There is currently no supported way to permanently disable the gate hook through settings composition; an Operator who wants no gate at all must remove the hook entry and avoid re-running install/composition afterward. The gate itself constrains where a gated shell runs, not what it writes, so it does not stop a deliberate absolute-path or `git -C <shared-checkout>` write from an already-isolated worktree. Within that scope it removes reliance on Operator or model memory for the concurrency-isolation guarantee, and it works from the approved `claude agents` Agent View task-composer UX as well as from the shell, because `EnterWorktree` is a tool the Director carries with it rather than a CLI flag the caller must remember.

## Skills

Generated skills contain procedure, trigger, acceptance/output semantics and domain methodology only. They must not encode model selection, provider routing, tool/permission escalation, human approval authority, credentials or source precedence. Broad skill preloading is prohibited by default; domain/stack skills are on-demand unless an eval-backed change approves preload.

## Project scope

Project-specific `.claude/agents/` is reserved for intentional audited overrides because project scope can shadow user-scope agents. Installer must detect collisions/shadowing and fail or require explicit reviewed resolution. Project-only skills live under `.claude/skills/` when truly project-specific.

Claude Code scans project `.claude/agents/` while walking from the working directory toward the repository root and also supports user-scope agents under `~/.claude/agents/`. Runtime/preflight logic must account for shadowing and nested definitions rather than treating a single directory listing as authoritative.

External-project activation (`studio_activation.py`) installs only `.claude/hooks/studio_worktree_gate.py`, the plain-session entry rule `.claude/rules/agency-workflow-kit.md` (ADR 0008; source `rules/agency-workflow-kit.md` in this adapter; never installed at user scope), and the full Front Door runtime closure to `.claude/runtime/` (the modules in `studio_renderer.FRONT_DOOR_RUNTIME_MODULES` plus `_studio/kit/manifest.json`) at project scope, plus the project's renderer-profile skills, never `agents/` or core `skills/`. That `.claude/runtime/` subtree is what makes `studio_front_door.py` resolvable and runnable as an installed CLI from inside the activated project; `check_activation_state()`/`preflight_project_readiness()` require every file in that closure to be present before reporting `ACTIVATED`/`KEEP`.

## Determinism and golden fixtures

Same canonical inputs + same renderer version must produce byte-identical outputs. Golden fixtures must cover:

- core: 5 agents + 12 skills;
- wordpress-etch: 5 agents + 14 skills;
- no `model: inherit` or missing model;
- Director-only bounded delegation and specialists without Agent capability;
- Director grants `EnterWorktree` and its role body requires calling it before Bash; specialists carry no `EnterWorktree`;
- denylisted legacy agent names;
- hostile legacy Section 15 input ignored/quarantined;
- stale `_studio/skills`, `_studio/traces` and legacy runtime targets rejected;
- project/user agent and skill collision detection;
- forced model override detection;
- second Etch authoring authority rejected;
- catalog count/name drift rejected;
- repeated render byte-identical.

## Runtime acceptance evidence

Before release cutover, runtime evidence must include:

- pinned Claude Code version;
- exact canonical agent discovery through explicit agent invocation;
- exact Studio skill inventory from owned installation state;
- MCP configuration inspection plus provider-specific connection verification where required;
- structural permissions verification against the effective five agent definitions;
- observed Director Agent tool-use delegation;
- specialists verified without Agent capability;
- complete effective-model observations: four specialists from Claude Code `/tasks`, plus parent `agency-director` from session JSONL `message.model` (`claude-session-jsonl-message-model`);
- installer transaction, rollback and exact-revision verification.

Static configuration, fixture output, agent prose, or successful `claude mcp list` alone must never be promoted to full runtime certification. `claude doctor` is not a Studio certification requirement unless a future reviewed contract adds a specific documented health signal that Studio consumes.

Current Anthropic model lifecycle and Claude Code runtime semantics must be checked as release inputs rather than assumed from this file. Model or precedence changes are reviewed configuration releases.
