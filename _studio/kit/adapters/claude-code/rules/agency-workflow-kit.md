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

    python3 "$CLAUDE_PROJECT_DIR/.claude/runtime/studio_front_door.py" --decomposition <file>

The decomposition is a JSON array of work orders. Each work order has `work_id`, `objective`, `scope`, `non_goals`, `acceptance_criteria`, `executor`, `owned_paths`, `permitted_side_effects` and `evidence_required`, plus optional `dependencies` and `operator_gate_required`. The result's `path` and `delegations` are the routing decision; the `studio-run-engine` skill describes how each route is carried out.

When planning signals are materially ambiguous, `--signals-json '<signals>'` returns a clarification/planning on-ramp. That output is a planning step and not a delegation-routing decision. Routing comes from `--decomposition`; the two flags can be combined.

## Host-native agents

Built-in `Explore`, `Plan` and `general-purpose` agents are outside Kit routing. When the Front Door result names a Kit specialist or `agency-director`, the named Kit agent is the one the result refers to, and a host-native agent is not an equivalent substitute for it.

## Independent verification

A request for independent verification, independent review or an independent double-check is satisfied by a fresh `agency-critic` run against the exact target revision and context. A re-read of the evidence by the same session is self-verification. Without Critic evidence, the result is reported as not independently verified. The authoritative statement of this obligation is the `agency-director` role contract.

## Scope of this file

This file grants no tools, MCP servers, approvals or model choices, and it does not override the Operator. It applies only in a checkout that contains it: a linked worktree without its own activation does not carry it.
