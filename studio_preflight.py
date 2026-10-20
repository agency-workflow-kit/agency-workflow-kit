#!/usr/bin/env python3
"""Read-only Claude Code environment preflight (SPEC §7 "Preflight", §23, §30.11).

Canonical lifecycle disposition (#114): activation runs this before any write
and preflight_project_readiness() folds it into its KEEP/ADD/FIX/BLOCK
outcome (studio_activation.run_claude_preflight). The core catalog is passed
in from the rendered kit, never hardcoded here, so it cannot drift from the
manifest.

Two blocker classes, per the spec's "block only the affected operation":

- ``blockers`` block install/activation: unresolved project agent/skill
  shadowing of the rendered core catalog (§30.11 "install plan BLOCKED"),
  retired Studio skills and the legacy Panda agent contract (both need the
  update/migration path first).
- ``certification_blockers`` are recorded, never block install: an
  unresolved ``claude --version`` (§23 "Preflight records claude --version")
  and a CLAUDE_CODE_SUBAGENT_MODEL override (§30.11 "certification BLOCKED").
  studio_release_validator consumes the latter.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


RETIRED_SKILLS = {
    "studio-launch",
    "studio-promote",
    "studio-resolve",
    "studio-weekly-retainer",
    "studio-weekly-audit",
    "studio-discovery",
    "studio-workflow-monitor",
    "studio-plan",
    "studio-wp-deploy",
    "studio-wp-content",
}

LEGACY_ROLE_MARKERS = {
    "Growth & Sales",
    "Client Success",
    "Engineering Delivery",
    "Design Agent",
    "Workflow Monitor",
}

# Canonical names may contain project-specific data that must be extracted or
# deliberately migrated rather than overwritten by a global Studio skill.
PRESERVE_CANDIDATE_SKILLS = {"studio-linear-sync"}


MODEL_OVERRIDE_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"


def model_override_blockers(env) -> list[str]:
    """Certification blockers for an effective-model override (§29.3)."""
    return [f"effective-model-override:{MODEL_OVERRIDE_ENV}"] if env.get(MODEL_OVERRIDE_ENV) else []


def probe_claude_version(runner=subprocess.run) -> str | None:
    """Record ``claude --version``; None when unavailable. Never installs Claude Code."""
    try:
        result = runner(["claude", "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "").strip()
    return output.splitlines()[0] if result.returncode == 0 and output else None


class ClaudePreflight:
    """Inspect relevant Claude Code inputs without mutating them.

    ``user_root`` is the user-global Claude root (``~/.claude`` or an
    explicit ``--global-root``). ``core_agents``/``core_skills`` are the
    rendered catalog names a project-scope definition would shadow.
    ``kit_owned_rels`` are project ``.claude`` paths the Kit itself
    journaled and that still match their journaled bytes (e.g. the supported
    update path's project-scope catalog): those are owned, not shadows. A
    user-owned or user-modified copy is still a shadow.
    """

    def __init__(self, user_root: Path, project: Path, *, core_agents, core_skills, env=None, claude_version=None,
                 kit_owned_rels=()):
        self.user_root = Path(user_root)
        self.project = Path(project)
        self.core_agents = frozenset(core_agents)
        self.core_skills = frozenset(core_skills)
        self.env = dict(os.environ if env is None else env)
        self.claude_version = claude_version
        self.kit_owned_rels = frozenset(kit_owned_rels)

    def _exists(self, path: Path) -> bool:
        return path.exists()

    def _project_agent_names(self) -> set[str]:
        base = self.project / ".claude" / "agents"
        if not base.is_dir():
            return set()
        return {p.stem for p in base.glob("*.md") if p.is_file()}

    def _project_skill_names(self) -> set[str]:
        base = self.project / ".claude" / "skills"
        if not base.is_dir():
            return set()
        names = set()
        for child in base.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                names.add(child.name)
        return names

    def _has_legacy_panda_agent_contract(self) -> bool:
        path = self.project / "AGENTS.md"
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return any(marker in text for marker in LEGACY_ROLE_MARKERS)

    def inspect(self) -> dict:
        inputs = {
            "project_claude_md": self._exists(self.project / "CLAUDE.md"),
            "project_claude_local_md": self._exists(self.project / "CLAUDE.local.md"),
            "project_agents_md": self._exists(self.project / "AGENTS.md"),
            "project_rules_dir": self._exists(self.project / ".claude" / "rules"),
            "project_agents_dir": self._exists(self.project / ".claude" / "agents"),
            "project_skills_dir": self._exists(self.project / ".claude" / "skills"),
            "project_settings_json": self._exists(self.project / ".claude" / "settings.json"),
            "project_settings_local_json": self._exists(self.project / ".claude" / "settings.local.json"),
            "project_mcp_json": self._exists(self.project / ".mcp.json"),
            "user_agents_dir": self._exists(self.user_root / "agents"),
            "user_skills_dir": self._exists(self.user_root / "skills"),
        }

        blockers: list[str] = []
        certification_blockers: list[str] = []
        warnings: list[str] = []
        preserve_candidates: list[str] = []

        if not self.claude_version:
            certification_blockers.append("claude-code-version-unresolved")

        override = self.env.get(MODEL_OVERRIDE_ENV)
        certification_blockers.extend(model_override_blockers(self.env))

        project_skills = self._project_skill_names()
        for name in sorted(self._project_agent_names() & self.core_agents):
            if f"agents/{name}.md" not in self.kit_owned_rels:
                blockers.append(f"agent-shadow:{name}")
        for name in sorted(project_skills & self.core_skills):
            if f"skills/{name}/SKILL.md" not in self.kit_owned_rels:
                blockers.append(f"skill-shadow:{name}")
        for name in sorted(project_skills & RETIRED_SKILLS):
            blockers.append(f"retired-skill:{name}")
        for name in sorted(project_skills & PRESERVE_CANDIDATE_SKILLS):
            preserve_candidates.append(name)

        if self._has_legacy_panda_agent_contract():
            blockers.append("legacy-panda-agent-contract")

        if inputs["project_agents_md"] and not inputs["project_claude_md"]:
            warnings.append("AGENTS.md-present-without-CLAUDE.md-import-contract")

        return {
            "status": "blocked" if blockers else "clear",
            "claude_code_version": self.claude_version,
            "inputs": inputs,
            "blockers": blockers,
            "certification_blockers": certification_blockers,
            "warnings": warnings,
            "preserve_candidates": preserve_candidates,
            "effective_model_override": override,
        }
