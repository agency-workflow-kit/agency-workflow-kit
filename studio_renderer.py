#!/usr/bin/env python3
"""Deterministic Phase 1 renderer for the Claude Code Studio OS adapter."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from studio_director_mcp_grants import DirectorMcpGrantError, resolve_director_mcp_grants


class RenderError(RuntimeError):
    pass


ROLE_DESCRIPTIONS = {
    "director": "Route and coordinate Studio OS work; delegate only to approved specialists and enforce gates.",
    "researcher": "Gather and synthesize evidence without implementation mutation.",
    "coder": "Implement bounded project changes and produce deterministic verification evidence.",
    "resolver": "Diagnose and repair bounded failures, escalating after two failed correction cycles.",
    "critic": "Independently verify exact-revision acceptance evidence without implementation mutation.",
}

ROLE_TOOLS = {
    # Grep/Glob are real, working tools, but are subsumed/dropped from
    # the effective runtime set whenever Bash is co-granted in the same
    # allowlist (empirically established fixing agency-director's own
    # contract) -- every role below carries Bash, so listing Grep/Glob
    # for them would be a dead grant. agency-researcher has no Bash, so
    # Grep/Glob remain its only real content-search capability.
    "director": "Read, Bash, EnterWorktree, ToolSearch, Agent(agency-researcher, agency-coder, agency-resolver, agency-critic)",
    "researcher": "Read, Grep, Glob, WebSearch, WebFetch",
    "coder": "Read, Edit, Write, Bash",
    "resolver": "Read, Edit, Write, Bash",
    "critic": "Read, Bash",
}

APPROVED_DELEGATES = {
    "agency-researcher", "agency-coder", "agency-resolver", "agency-critic"
}

RETIRED_SKILLS = {
    "studio-plan", "studio-resolve", "studio-launch", "studio-promote",
    "studio-wp-deploy", "studio-wp-content", "studio-weekly-retainer",
    "studio-weekly-audit", "studio-discovery",
}

WORKTREE_GATE_SCRIPT = "studio_worktree_gate.py"

# ADR 0008: the one Kit-owned project-scope instruction asset. Installed only
# into a project's own .claude/ (activation closure), never the user-global
# root; the project's root CLAUDE.md stays Operator-owned.
ENTRY_RULE_RELATIVE = Path("rules") / "agency-workflow-kit.md"

# Transitive import closure of the Front Door CLI (studio_front_door.py),
# computed by AST analysis over local studio_* modules -- this is the
# complete, minimal runtime an activated external project needs in order
# to execute `python3 .../studio_front_door.py --decomposition <file>`
# outside this repo. Order is source -> its own local imports, ending
# with the two modules that carry no further local imports. Keep this
# minimal: every one of the other 29+ studio_* modules in this repo is
# deliberately excluded, and a new module must only be added here if the
# transitive-import closure of studio_front_door.py actually grows.
FRONT_DOOR_RUNTIME_MODULES = (
    "studio_front_door",
    "studio_planning_onramp",
    "studio_autonomous_execution",
    "studio_factory_run",
    "studio_factory_provider_binding",
    "studio_verification_adapters",
    "studio_live_provider_runtime",
    "studio_parallel_coordinator",
    "studio_execution_routing",
    "studio_decision_provider",
    "studio_authorization_gate",
    "studio_jev_decision_provider",
    "studio_runtime_observability",
    "studio_workflow_profiles",
    "studio_director_bridge",
    "studio_capability_router",
    "studio_renderer",
    "studio_work_order_plan",
    "studio_director_mcp_grants",
)

# studio_capability_router.DEFAULT_MANIFEST_PATH resolves to
# "<module dir>/_studio/kit/manifest.json" -- the runtime subtree must
# therefore carry the manifest at that exact relative location for the
# packaged CapabilityRouter to find it without any code change.
RUNTIME_MANIFEST_RELATIVE = Path("_studio") / "kit" / "manifest.json"

# The renderer's own sibling directory -- studio_renderer.py already lives
# beside every other module in FRONT_DOOR_RUNTIME_MODULES, so this is the
# single source of truth for the runtime subtree's module sources; no
# duplicated copy of any of these files is kept anywhere else in the repo.
_RUNTIME_MODULE_SOURCE_DIR = Path(__file__).resolve().parent

LEGACY_AGENTS = {
    "agency-growth", "agency-design", "agency-client-success",
    "agency-engineering", "agency-workflow-monitor", "agency-finance",
    "agency-linear", "agency-operator",
}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"invalid manifest: {path}: {exc}") from exc


def _extract_sections(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## studio-"):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _validate_manifest(manifest: dict) -> None:
    agents = manifest.get("agents", [])
    invariants = manifest.get("invariants", {})
    catalog = manifest.get("catalog", {})
    expected_agents = invariants.get("agent_count")
    if len(agents) != expected_agents or expected_agents != 5:
        raise RenderError("agent count drift")

    names = [a.get("name") for a in agents]
    if len(names) != len(set(names)) or any(name in LEGACY_AGENTS for name in names):
        raise RenderError("invalid or legacy agent identity")
    if "agency-operator" in names:
        raise RenderError("Operator is human authority, not a generated agent")

    directors = [a for a in agents if a.get("role") == "director"]
    if len(directors) != 1 or directors[0].get("name") != "agency-director":
        raise RenderError("exactly one canonical Director is required")

    for agent in agents:
        model = agent.get("model")
        if not model or model == "inherit":
            raise RenderError(f"explicit pinned model required for {agent.get('name')}")
        delegates = agent.get("delegates", [])
        if agent.get("role") != "director" and delegates:
            raise RenderError(f"recursive Studio delegation forbidden for {agent.get('name')}")

    if set(directors[0].get("delegates", [])) != APPROVED_DELEGATES:
        raise RenderError("Director delegation allowlist drift")

    core = catalog.get("core_generated_skills", [])
    expected_core = catalog.get("core_generated_skill_count")
    if len(core) != expected_core or expected_core != 4 or len(core) != len(set(core)):
        raise RenderError("core catalog count/name drift")
    if RETIRED_SKILLS.intersection(core):
        raise RenderError("retired skill present in generated core catalog")

    wp = catalog.get("wordpress_etch_profile_generated_skills", [])
    if len(wp) != catalog.get("wordpress_etch_profile_generated_skill_count") or len(wp) != 2:
        raise RenderError("WordPress/Etch profile catalog drift")
    if set(wp) != {"studio-etch-author", "studio-wp-audit"}:
        raise RenderError("WordPress/Etch profile must contain only approved wrappers")


def _render_agent(agent: dict, role_body: str, extra_tools: list[str] | None = None) -> str:
    role = agent["role"]
    tools = ROLE_TOOLS[role]
    if extra_tools:
        tools = ", ".join([tools, *extra_tools])
    frontmatter = [
        "---",
        f"name: {agent['name']}",
        f"description: {ROLE_DESCRIPTIONS[role]}",
        f"model: {agent['model']}",
        f"effort: {agent['effort']}",
        f"tools: {tools}",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + role_body.strip() + "\n"


def _render_skill(name: str, body: str) -> str:
    if not body:
        raise RenderError(f"missing canonical procedure for {name}")
    description = f"Studio OS procedure for {name.removeprefix('studio-').replace('-', ' ')}. Use when its documented trigger applies."
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body.strip()}\n"


def render_kit(
    kit_dir: Path,
    output_dir: Path,
    *,
    profile: str = "core",
    manifest_path: Path | None = None,
    director_mcp_grants: list[str] | None = None,
) -> dict:
    kit_dir = Path(kit_dir)
    output_dir = Path(output_dir)
    manifest = _load_json(Path(manifest_path) if manifest_path else kit_dir / "manifest.json")
    _validate_manifest(manifest)
    if profile not in {"core", "wordpress-etch"}:
        raise RenderError(f"unsupported profile: {profile}")

    resolved_director_mcp_grants: list[str] = []
    if director_mcp_grants:
        try:
            resolved_director_mcp_grants = resolve_director_mcp_grants(director_mcp_grants)
        except DirectorMcpGrantError as exc:
            raise RenderError(str(exc)) from exc

    core_sections = _extract_sections(kit_dir / "workflows" / "core-contracts.md")
    profile_sections = _extract_sections(kit_dir / "workflows" / "wordpress-etch-profile.md")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "agents").mkdir(parents=True)
    (output_dir / "skills").mkdir(parents=True)
    (output_dir / "hooks").mkdir(parents=True)
    (output_dir / ENTRY_RULE_RELATIVE.parent).mkdir(parents=True)
    (output_dir / "runtime" / RUNTIME_MANIFEST_RELATIVE.parent).mkdir(parents=True)

    gate_source = kit_dir / "adapters" / "claude-code" / WORKTREE_GATE_SCRIPT
    if not gate_source.is_file():
        raise RenderError(f"missing worktree gate script: {gate_source}")
    (output_dir / "hooks" / WORKTREE_GATE_SCRIPT).write_text(
        gate_source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )

    rule_source = kit_dir / "adapters" / "claude-code" / ENTRY_RULE_RELATIVE
    if not rule_source.is_file():
        raise RenderError(f"missing project entry rule: {rule_source}")
    (output_dir / ENTRY_RULE_RELATIVE).write_text(
        rule_source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )

    for module_name in FRONT_DOOR_RUNTIME_MODULES:
        module_source = _RUNTIME_MODULE_SOURCE_DIR / f"{module_name}.py"
        if not module_source.is_file():
            raise RenderError(f"missing front door runtime module: {module_source}")
        (output_dir / "runtime" / f"{module_name}.py").write_text(
            module_source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )

    runtime_manifest_source = kit_dir / "manifest.json"
    if not runtime_manifest_source.is_file():
        raise RenderError(f"missing manifest for runtime: {runtime_manifest_source}")
    (output_dir / "runtime" / RUNTIME_MANIFEST_RELATIVE).write_text(
        runtime_manifest_source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )

    for agent in manifest["agents"]:
        role_path = kit_dir / "roles" / f"{agent['role']}.md"
        if not role_path.is_file():
            raise RenderError(f"missing role contract: {role_path}")
        extra_tools = resolved_director_mcp_grants if agent["role"] == "director" else None
        content = _render_agent(agent, role_path.read_text(encoding="utf-8"), extra_tools=extra_tools)
        (output_dir / "agents" / f"{agent['name']}.md").write_text(content, encoding="utf-8", newline="\n")

    skill_names = list(manifest["catalog"]["core_generated_skills"])
    if profile == "wordpress-etch":
        skill_names += manifest["catalog"]["wordpress_etch_profile_generated_skills"]

    for name in skill_names:
        body = core_sections.get(name) or profile_sections.get(name)
        skill_dir = output_dir / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_render_skill(name, body), encoding="utf-8", newline="\n")

    return {"profile": profile, "agent_count": len(manifest["agents"]), "skill_count": len(skill_names)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, default=Path("_studio/kit"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["core", "wordpress-etch"], default="core")
    args = parser.parse_args()
    result = render_kit(args.kit, args.output, profile=args.profile)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
