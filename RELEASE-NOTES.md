# Release notes

## 1.2.0-rc.2: public beta

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
