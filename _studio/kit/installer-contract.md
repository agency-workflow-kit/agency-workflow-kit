# Transactional Installer Contract

Status: transaction core implemented in `studio_installer.py`, including byte-exact backups, crash/interruption rollback, and hash-bound install verification. #114 wired `ClaudePreflight` into activation (before any write) and `preflight_project_readiness()`, records `claude --version`, added the supported project uninstall (`studio_activation.py --uninstall`) and the `_studio/project.json` contract. Full settings/MCP inspection and three-way merge remain open.

## Lifecycle

Installer implementation must separate:

1. inspect
2. plan
3. apply
4. verify
5. upgrade
6. uninstall

Inspect and plan are read-only.

Supported uninstall is `studio_activation.py --project <project> --uninstall` (`uninstall_project()`), a thin project-scope wrapper over `StudioInstaller.uninstall()`; see `docs/release/install-update-recovery.md` for the retained/removed contract. The user-global catalog is never uninstalled automatically.

Current implementation covers deterministic rendered-asset planning, collision-safe apply, atomic file replacement, SHA-256 ownership journaling, idempotent reruns, stale-plan rejection, byte-exact pre-write backups, crash/interruption rollback, hash-bound verification, and conservative uninstall. It does not yet claim the complete lifecycle above.

## Preflight

Before any write, the release-complete installer must inspect relevant project/user Claude configuration, including:

- `CLAUDE.md`
- `CLAUDE.local.md`
- `AGENTS.md`
- `.claude/rules/`
- `.claude/agents/`
- `.claude/skills/`
- `.claude/settings.json`
- `.claude/settings.local.json`
- hooks
- MCP configuration
- user-scope agents and Studio skills
- legacy commands/templates that may shadow generated assets
- dirty Git state
- Claude Code version

## Mutation safety

The installer must:

- detect collisions before writes;
- never overwrite unresolved user content;
- preserve unrelated settings, permissions, hooks, dependencies, and deployment behavior;
- keep exact pre-write hashes;
- create byte-exact backups for modified files before replacing or removing them;
- use atomic replacement where available;
- keep an install journal;
- make an identical rerun a no-op;
- use three-way comparison for upgrades;
- retain user-modified assets on uninstall unless the Operator explicitly chooses otherwise.

The current transaction core intentionally refuses to overwrite a user-modified managed asset, creates byte-exact backups under `.studio-backups/<timestamp>/` before any overwrite or removal, and rolls back a failed or interrupted apply from its own `.studio-transaction.json` log. Full historical three-way merge across arbitrary upstream/user edits remains unimplemented.

## Claude Code targets

Reusable generated agents target `~/.claude/agents/`.
Reusable Studio skills target `~/.claude/skills/studio-*/SKILL.md`.
Project-specific overrides/additions may target `.claude/agents/` and `.claude/skills/` only when explicitly planned.

The neutral kit source under `_studio/kit/` is not itself a Claude discovery directory.

Worktree-isolation settings composition is project scope: its gate hook command is `$CLAUDE_PROJECT_DIR`-relative. User/global/config-root installation installs the reusable global catalog and does not compose that recipe. An alternate or disposable `--global-root` gets the same global-scope behavior as `~/.claude` (activation's global step calls `StudioInstaller.apply(compose_settings=False)`). Activation composes the recipe only into the target project's own `.claude/settings.json`. The direct `studio_installer.py` CLI is project-local only: `--target` must be exactly `<project>/.claude` (`--project` defaults to the current directory, the same binding `studio_update.py` uses). Any other target, including a global or config root, is refused before anything is written; use `studio_activation.py --global-root` for a global root. `studio_activation.py --check-only` exits 0 only for `ACTIVATED`. Any other reported activation state exits 1 after printing the JSON. That exit means the activation condition is not satisfied.

## Failure conditions

Installation or release verification must fail on unresolved collisions, stale mutation plans, accidental model inheritance, unauthorized legacy agent generation, recursive specialist delegation, Director delegation allowlist drift, weakened deny/ask controls, unsupported source shadowing, incomplete provenance, or missing required evidence.

## Workspace trust

The installer must never simulate or auto-accept workspace trust, hooks, MCP servers, or permission prompts that require Operator review.

## Project-local install/version/update/migration contract

`studio_update.py` composes `render_kit`, `StudioInstaller`, and `MigrationPlanner`/`MigrationApplier` (all above) into the reproducible lifecycle: install/activate -> verify health -> identify installed/candidate version -> detect update availability -> non-mutating preview -> authorize -> execute -> verify -> recover. It is not a second installer/updater framework -- it is the CLI/API surface a Founder or an authorized execution capability calls (`python3 studio_update.py {inspect|preview|authorize|update|recover}`), run from a Workflow Kit checkout against a target project's `--target`/`--project` directories.

Five operations, five functions:

- `inspect(kit_dir, target_dir, project_dir=None)` -- read-only installed/candidate version + health. The CLI passes `--project`. When the target is that project's `.claude` and the install journal is exactly the activation closure, health scores the closure and the activation settings contract, not a second project-local agent/skill catalog. Otherwise, when the target is that project's `.claude`, health uses the update-managed set (CLI-default overlap omits `studio-gate`, `studio-grill`, and `studio-verify`). Without a project, health is a strict full-render verify. Operator record: `docs/release/install-update-recovery.md`.
- `preview(kit_dir, target_dir, project_dir)` -- non-mutating delta (asset plan, migration plan, compatibility), fingerprinted over its exact inputs including target/project identity. Fails closed rather than silently self-healing an interrupted transaction (`StudioInstaller.plan()`'s own recovery side effect) -- run `recover()` first if it does.
- `authorize(kit_dir, target_dir, project_dir, requested_by=...)` -- the Founder-facing operation: fails closed unless the kit source checkout is a clean, unlocked linked worktree containing `kit_dir` (reports repo root, worktree, branch, HEAD, dirty/index/stash, protected status; never stashes/commits/resets/switches to clear it); then writes a durable, atomic record under `<target>/.studio-update-authorization/`, binding a fresh preview's fingerprint to the exact target/project and requester, and attaching the checkout preflight report (not part of the fingerprint).
- `update(kit_dir, target_dir, project_dir, authorization_path=..., executed_by=...)` -- the execution-capability operation. Re-runs the same kit-checkout preflight as `authorize` before any mutation. Requires a path to an authorization record, re-validates its content against a freshly recomputed preview (rejects a stale fingerprint, a record minted for a different target/project, a malformed/unapproved/secret-bearing record), then applies the asset update and any required migration, writing durable evidence under `<target>/.studio-update-evidence/`. It does not compose `settings.json`. Settings composition remains activation's project-local `StudioInstaller.apply()`. When a migration is required, the installer transaction stays open until that migration commits. The only post-install plan change that is accepted is an authorized user-skill install that this asset phase satisfied; quarantine and replace stay as authorized. If the required migration fails, the open transaction restores the pre-update tree. `.studio-update-finalization.json` is written before migration commits. After a committed migration, `recover` finishes that record and does not roll the migration back.
- `recover(kit_dir, target_dir, project_dir)` -- bounded rollback: resolves an interrupted install transaction and/or migration transaction. Never advances an update.

Authorization is a genuinely separate, durable, attributable artifact from execution -- per the Founder-as-authorization-authority requirement, `authorize` and `update` are two distinct calls, so a human-run `authorize` can genuinely be separated in time and process from `update`, rather than the Founder needing to run the underlying git/install/migration commands by hand. This module does not itself enforce WHO is permitted to call `authorize()` -- that a human Founder, and not arbitrary code, actually approved a given update is a permission/consent-layer concern outside this module, exactly as `PlanGovernanceGate.evaluate()`'s caller-supplied `operator_authorized` evidence relies on the same external boundary rather than an in-process unforgeability primitive.

Scope boundary: this contract closes the asset-install/migration half of that requirement (StudioInstaller/MigrationApplier were already bounded, transactional, and idempotent; only a non-Bash, Operator-consented entry point was missing). It does not cover an activated project's own shared-checkout git operations (`git fetch`/`merge`) -- no bounded, Operator-gated git-execution capability exists in this codebase yet; that remains open.
