# Agency Workflow Kit: Front Door entry

This project is activated with the Agency Workflow Kit. This file is a Kit-managed asset, installed and journaled by Kit activation. The project's root `CLAUDE.md` stays Operator-owned.

## The Front Door

The plain Claude Code session in this project is the Kit Front Door. Work takes one of three routes:

- `DIRECT`: the current session does the work itself.
- `DELEGATE`: Kit specialists do the work (`agency-researcher`, `agency-coder`, `agency-resolver`, `agency-critic`).
- `DIRECTOR`: `agency-director` runs governed execution.

The installed Front Door CLI selects the route. This file carries no routing logic.

## When the CLI is used

A clear task that the current session completes itself, with no delegation, is `DIRECT` and needs no CLI call.

Before any part of a task is delegated to another agent, the session writes a bounded decomposition to a file and runs:

    d="${CLAUDE_PROJECT_DIR:-$PWD}"; until [ -f "$d/.claude/rules/agency-workflow-kit.md" ] || [ "$d" = "$(dirname "$d")" ]; do d="$(dirname "$d")"; done; [ -f "$d/.claude/rules/agency-workflow-kit.md" ] && [ -f "$d/.claude/runtime/studio_front_door.py" ] || { echo "studio_front_door.py not found: no activated project at or above ${CLAUDE_PROJECT_DIR:-$PWD}" >&2; false; } && python3 -B "$d/.claude/runtime/studio_front_door.py" --decomposition <file>

The command starts at `$CLAUDE_PROJECT_DIR` when it is set, otherwise at the current directory, finds the nearest activated project at or above it (the directory holding this file as `.claude/rules/agency-workflow-kit.md`) and runs that project's installed Front Door CLI, so it resolves from the project root, a nested directory or a linked worktree under the project. With no activated project, or one without its installed CLI, it prints `studio_front_door.py not found` and exits non-zero without routing anything.

The decomposition is a JSON array of work orders. Each work order has `work_id`, `objective`, `scope`, `non_goals`, `acceptance_criteria`, `executor`, `owned_paths`, `permitted_side_effects` and `evidence_required`, plus optional `dependencies`, `operator_gate_required` and `risk` (`low`, `medium` or `high`). The result's `path` and `delegations` are the routing decision; the `studio-run-engine` skill describes how each route is carried out.

When planning signals are materially ambiguous, `--signals-json '<signals>'` returns a clarification/planning on-ramp. That output is a planning step and not a delegation-routing decision. Routing comes from `--decomposition`; the two flags can be combined.

## Pushing and pull requests (CI budget)

CI minutes are spent only after local evidence is green. Every push and pull request in this project goes through the installed helper `python3 -B "$d/.claude/runtime/studio_ci_budget.py" --project <checkout>`, where `$d` is the activated project the lookup above finds:

1. Finish the work and commit it. `--local-checks` runs the project's lint, typecheck, test and build (`_studio/project.json` `commands`, else package.json scripts); fix and rerun until all are green. In `strict` mode, the default, a missing check command blocks.
2. `--ship --base <branch> --title <title> --body-file <file>` refuses uncommitted changes, empty commits and a HEAD that is already pushed, reruns the local checks, then pushes once and opens the pull request as a draft. Batch commits so a task needs one push.
3. Mark it ready with `--ready --evidence <ship output>` only when the local evidence for the pushed HEAD is complete, including independent review where the work requires it.
4. An empty or "retrigger" commit is not pushed, and a push is not used to restart CI.
5. Read the remote checks with `--classify-checks`; it waits up to `--wait-seconds` (default 300) for the required checks to appear. `infrastructure_block` (a required check that did not start, or that failed with no run behind it: billing, quota, runner unavailable) means an infrastructure block: do not push again; report it at the gate with the local evidence. `failed` means reproduce and fix locally, then make one new push.

`--risks` reports CI-minute risks in the project's workflows without editing them. The CI templates (`--render-templates <dir>`) are applied to a project only by a normal task run, as a pull request.

## Host-native agents

Built-in `Explore`, `Plan` and `general-purpose` agents are outside Kit routing. When the Front Door result names a Kit specialist or `agency-director`, the named Kit agent is the one the result refers to, and a host-native agent is not an equivalent substitute for it.

## Independent verification

A request for independent verification, independent review or an independent double-check is satisfied by a fresh `agency-critic` run against the exact target revision and context. A re-read of the evidence by the same session is self-verification. Without Critic evidence, the result is reported as not independently verified. The authoritative statement of this obligation is the `agency-director` role contract.

## Scope of this file

This file grants no tools, MCP servers, approvals or model choices, and it does not override the Operator. It applies only in a checkout that contains it: a linked worktree without its own activation does not carry it.
