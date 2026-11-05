---
name: activate
description: Activate Agency Workflow Kit in the current project, check whether it is activated, or remove it from the project. Use only when the user asks to install, activate, set up, check or remove Agency Workflow Kit in this project.
---

# Activate Agency Workflow Kit

The Kit source ships inside this plugin at `${CLAUDE_SKILL_DIR}/../..`. Activation writes the Kit's agents, skills and runtime to `~/.claude` (shared by every project on this machine), and its Front Door rule, worktree gate hook, runtime, `settings.json` keys and `_studio/project.json` to this project, journaling every file. It adds no tools, MCP servers or permissions.

If the user asked to remove the Kit, skip to the last paragraph. If they only asked to check, run step 5 alone. Running `/agency-workflow-kit:activate` with no other words counts as an explicit ask to activate.

Run each step from inside the project and stop at the first failure. Show the user the command's output when it fails. Every command passes `--project "$(git rev-parse --show-toplevel)"`, so the repository root is activated even when the shell is in a subfolder.

1. Find the project root: `git rev-parse --show-toplevel`. If this is not a git repository, or the root is not the project the user means, stop and ask. A linked git worktree is activated on its own; activating it does not activate its primary checkout.
2. Check, read-only: `python3 "${CLAUDE_SKILL_DIR}/../../studio_activation.py" --project "$(git rev-parse --show-toplevel)" --preflight`. If it says Python 3.12 or newer is needed, tell the user to install it (macOS: `brew install python@3.12`; Debian/Ubuntu: `sudo apt install python3.12`) and stop. Any other non-zero exit is a blocker: report its reason and stop.
3. If the user did not explicitly ask to activate this project, tell them what step 4 writes (see above) and wait for a yes.
4. Activate: `python3 "${CLAUDE_SKILL_DIR}/../../studio_activation.py" --project "$(git rev-parse --show-toplevel)"`. It prints `ACTIVATED` on success.
5. Verify: `python3 "${CLAUDE_SKILL_DIR}/../../studio_activation.py" --project "$(git rev-parse --show-toplevel)" --check-only`. Exit 0 means the state is `ACTIVATED`. Otherwise report the state and the missing items from its JSON output.
6. On `ACTIVATED`, tell the user to start a new Claude Code session inside this project (exit, then run `claude` here). The Front Door rule and hooks load at session start, so this session does not have them yet.

To remove the Kit from this project, confirm with the user, then run `python3 "${CLAUDE_SKILL_DIR}/../../studio_activation.py" --project "$(git rev-parse --show-toplevel)" --uninstall`. It removes only the Kit files that still match the journal and reports any file the user modified. It never touches `~/.claude`, which other projects share.
