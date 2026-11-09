# Release notes

## 1.3.0-rc.2: public beta

This public beta follows `1.3.0-rc.1`. It adds evidence checks around delegated work and keeps agents away from `.env` files. `1.2.0` stays the stable release.

### New

- **Evidence for every acceptance criterion.** When a work order's acceptance criteria carry IDs, or the Critic reports per-criterion results, the work order is not DONE until the Critic records one evidenced pass for each criterion. A missing, blank or mismatched result fails the check. Older work orders with plain-text criteria and no per-criterion results keep the previous single verdict.
- **Open work orders stop the Director from finishing.** A Director turn cannot end while a work order it delegated is still open. After three blocked attempts the turn ends anyway, so a confused session is never stuck.
- **Finish check for Coder and Resolver handbacks (shadow).** A Coder or Resolver handback is checked for a claim per criterion and a clean working tree. In this release the check only records; it does not block. Set `STUDIO_FINISH_GATE=enforce` to have it block.
- **No reading of `.env` files.** Activation adds `Read(./.env)` and `Read(./.env.*)` to the project's `permissions.deny`. Your own permission entries are kept. This blocks Claude Code's Read tool; it is not a sandbox, and a shell command can still read the file.
- **Clearer errors.** Activation and Front Door errors say what went wrong and what to do, instead of a traceback.
- **Tool state stays out of git.** Activation adds a `.abide/` line to `.git/info/exclude`, unless git already ignores it.

### Updating from 1.3.0-rc.1

Plugin route: remove the marketplace, install the `v1.3.0-rc.2` tag, then run `/agency-workflow-kit:activate` in each project. Clone route: follow [Quick Start step 8](QUICKSTART.md#8-update) from a linked worktree of the new tag, user-global scope first, then each project. Activated projects report as not `ACTIVATED` until you activate them again, because their settings do not yet carry the new `.env` rules.

### Known limitations

The `1.3.0-rc.1` limitations below still apply. The finish check does not block unless you turn it on.

## 1.3.0-rc.1: public beta

This public beta follows the `1.2.0` stable release. It adds a Claude Code plugin install, and it changes the runtime, so it ships as a prerelease first. `1.2.0` stays the stable release.

### New

- **Install as a Claude Code plugin.** `claude plugin install agency-workflow-kit --marketplace agency-workflow-kit/agency-workflow-kit#v1.3.0-rc.1` installs the Kit once per machine, pinned to the tag. Then `/agency-workflow-kit:activate` inside a project runs the preflight, activation and check for you, and stops at the first blocker. The [Quick Start](QUICKSTART.md#2-install-the-kit-source) covers update and removal for this route.
- **Risk-tiered review.** A work order can carry `risk` (`low`, `medium` or `high`). The Director sets it by what a shipped defect would do to a user. For `high`, the Critic requires runtime or rendered proof and probes misuse, boundary and failure paths. Blocking Critic findings open with a kind label, such as `missing-validation:`, so the same class of defect can be fixed everywhere it occurs.
- **Durable run evidence.** A governed `--execute-json` run records its terminal outcome and per-work-order release evidence in a durable run event log. The evidence survives removal of the linked worktree it ran in. Critic-authored evidence is kept apart from metadata the runtime observed.
- **Clearer first run.** Activation prints `ACTIVATED` and the next command on success, or the state that stopped it. It refuses a Python older than 3.12 with a one-line message instead of a traceback. It finds its Kit relative to its own location, so `--kit` is optional.

### Updating from 1.2.0

Plugin route: install the plugin as above, then run `/agency-workflow-kit:activate` in each project. Clone route: follow [Quick Start step 8](QUICKSTART.md#8-update) from a linked worktree of the new tag, user-global scope first, then each project. Files you own are not changed. `--check-only` should report `ACTIVATED`; if it does not, run activation again (Quick Start step 4).

### Supported runtime

- Claude Code CLI `2.1.301`: formally qualified.
- Claude Code CLI `2.1.302`: real-project dogfood evidence.
- Newer patch versions: not formally qualified yet.
- Python 3.12 or newer, with git, on macOS or Linux.

### Known limitations

- **Prerelease.** Dogfood on the `1.3.0` baseline has not started. Interfaces, files and commands may change before `1.3.0`.
- **Plugin route.** It does not use the preview, authorize and update records of Quick Start step 8, which need a clone. A pinned plugin cannot be re-pinned in place: remove the marketplace and install the new tag. Uninstalling the plugin does not remove the files activation wrote. A folder Claude Code has not seen before asks you to trust it first.
- **Claude Code only.** Cursor and Codex are not supported.
- **Session location.** You must start Claude Code inside the activated project or worktree. A session started in another checkout does not load the Kit rule or hooks, and its native worktree isolation can root in the wrong clone.
- **Verification coverage.** Real-project use was on macOS. Linux is covered only by the unit suite. Windows is not supported. The Claude Code Desktop app is not verified.
- **No public test suite.** The test suite and certification evidence stay with the upstream development source. This repository contains the installable Kit only.
- **No global uninstall command.** Per-project uninstall exists. Remove the user-global files by hand (see the Quick Start).
- **Model access.** Pinned specialist models must be available to your Claude Code account.

### Feedback

Open an issue for bugs. Report security problems as described in [SECURITY.md](SECURITY.md).

## 1.2.0: stable release

This is the first stable release of Agency Workflow Kit. It follows the `1.2.0-rc.2` public beta and carries two runtime fixes.

### Fixed

- **No bytecode written into your project.** The Front Door router command in the entry rule and in the `studio-run-engine` skill now runs `python3 -B`. Routing no longer writes `__pycache__` files under `<project>/.claude/runtime/`. The Kit does not remove bytecode that an earlier release already wrote there.
- **Updates keep the Kit agents global.** An update now recognizes a project that an older Kit release activated, even when that release activated a different set of files. It no longer copies the five Kit agents and the `studio-run-engine` skill into that project's `.claude`, where they would shadow the user-global catalog.

### Updating from the public beta

Follow [Quick Start step 8](QUICKSTART.md#8-update) from a linked worktree of the new tag, user-global scope first, then each project. The update replaces the managed Front Door rule in each project and the managed `studio-run-engine` skill in the user-global catalog. Files you own are not changed. No re-activation is needed; `--check-only` should report `ACTIVATED`.

### Included

- Five Kit agents: `agency-director`, `agency-researcher`, `agency-coder`, `agency-resolver`, `agency-critic`. Each pins its model and effort in its frontmatter.
- Core skills, the Front Door entry rule, and the Front Door runtime that selects `DIRECT`, `DELEGATE` or `DIRECTOR`.
- A worktree gate hook, plus worktree-isolation settings composed at project scope.
- Commands to activate, inspect, preview, authorize, update, recover and uninstall, all journaled and fail-closed.

### Supported runtime

- Claude Code CLI `2.1.301`: formally qualified.
- Claude Code CLI `2.1.302`: real-project dogfood evidence.
- Claude Code CLI `2.1.308`, `2.1.309`, `2.1.310`: observed in release certification and real-project dogfood, not formally qualified.
- Other newer patch versions: not formally qualified yet.
- Python 3.12 or newer, with git, on macOS or Linux.

### Known limitations

- **Session location.** You must start Claude Code inside the activated project or worktree. A session started in another checkout does not load the Kit rule or hooks, and its native worktree isolation can root in the wrong clone.
- **Verification coverage.** Real-project use was on macOS. Linux is covered only by the unit suite. Windows is not supported. The Claude Code Desktop app is not verified.
- **No public test suite.** The test suite and certification evidence stay with the upstream development source. This repository contains the installable Kit only.
- **Updates need git.** `authorize`/`update` run only from a clean linked git worktree of the Kit.
- **No global uninstall command.** Per-project uninstall exists. Remove the user-global files by hand (see the Quick Start).
- **Legacy identifiers.** Some preflight blocker identifiers and messages refer to a legacy internal agent contract by its internal name. They are kept unchanged in this release.
- **Update ergonomics.** The user-global update needs an empty directory as its `--project` argument (see the Quick Start).
- **Open follow-ups:** provider-neutral runtime and model routing; a revisit of the Critic model pin after future model releases; a kit-repo-only read-only command allowlist; capture of Critic startup failures in real use.
- **Model access.** Pinned specialist models must be available to your Claude Code account.

### Feedback

Open an issue for bugs. Report security problems as described in [SECURITY.md](SECURITY.md).

## 1.2.0-rc.2: public beta

Superseded by `1.2.0`. As published:

**Pre-release. Not stable.** This is the second public beta of Agency Workflow Kit. It replaces the previous public beta prerelease and carries one runtime fix.

### Fixed

- **Front Door router lookup.** Before delegating, the session runs the installed Front Door router. The entry rule and the `studio-run-engine` skill located it through `$CLAUDE_PROJECT_DIR`, which Claude Code exports to hooks but not reliably to Bash tool calls. When the variable was absent, the command resolved to `/.claude/runtime/studio_front_door.py` and failed. The command now starts at `$CLAUDE_PROJECT_DIR` when it is set, otherwise at the current directory, and walks up to the nearest activated project. It runs that project's installed router, so it works from the project root, a nested directory or a linked worktree. With no activated project it prints `studio_front_door.py not found` and exits non-zero without routing anything.

### Updating from the previous public beta

Follow [Quick Start step 8](QUICKSTART.md#8-update) from a linked worktree of the new tag, user-global scope first, then each project. The update replaces the managed Front Door rule in each project and the managed `studio-run-engine` skill in the user-global catalog. Files you own are not changed. No re-activation is needed; `--check-only` should report `ACTIVATED`.

### Included

- Five Kit agents: `agency-director`, `agency-researcher`, `agency-coder`, `agency-resolver`, `agency-critic`. Each pins its model and effort in its frontmatter.
- Core skills, the Front Door entry rule, and the Front Door runtime that selects `DIRECT`, `DELEGATE` or `DIRECTOR`.
- A worktree gate hook, plus worktree-isolation settings composed at project scope.
- Commands to activate, inspect, preview, authorize, update, recover and uninstall, all journaled and fail-closed.

### Supported runtime

- Claude Code CLI `2.1.301`: formally qualified.
- Claude Code CLI `2.1.302`: real-project dogfood evidence.
- Newer patch versions: not formally qualified yet.
- Python 3.12 or newer, with git, on macOS or Linux.

### Known limitations

- **Beta.** File layout, commands and output may change before `1.2.0`.
- **Session location.** You must start Claude Code inside the activated project or worktree. A session started in another checkout does not load the Kit rule or hooks, and its native worktree isolation can root in the wrong clone.
- **Verification coverage.** Real-project use was on macOS. Linux is covered only by the unit suite. Windows is not supported. The Claude Code Desktop app is not verified.
- **No public test suite.** The test suite and certification evidence stay with the upstream development source. This repository contains the installable Kit only.
- **Updates need git.** `authorize`/`update` run only from a clean linked git worktree of the Kit.
- **No global uninstall command.** Per-project uninstall exists. Remove the user-global files by hand (see the Quick Start).
- **Legacy identifiers.** Some preflight blocker identifiers and messages refer to a legacy internal agent contract by its internal name. They are kept unchanged in this beta.
- **Update ergonomics.** The user-global update needs an empty directory as its `--project` argument (see the Quick Start).
- **Open follow-ups:** provider-neutral runtime and model routing; a revisit of the Critic model pin after future model releases; a kit-repo-only read-only command allowlist; capture of Critic startup failures in real use.
- **Model access.** Pinned specialist models must be available to your Claude Code account.

### Feedback

Open an issue for bugs. Report security problems as described in [SECURITY.md](SECURITY.md).

## 1.2.0-rc.1: public beta

Superseded by `1.2.0-rc.2`. As published:

**Pre-release. Not stable.** This is the first public release of Agency Workflow Kit. It is the same runtime that was used in controlled internal releases and real-project dogfood, packaged for public use. It has no runtime behavior changes from the last internal release.

Its Included, Supported runtime, Known limitations and Feedback sections were the same as those of `1.2.0-rc.2`.
