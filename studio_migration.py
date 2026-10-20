import hashlib
import json
import re
from pathlib import Path

from studio_installer import StudioInstaller


CANONICAL_AGENTS = (
    "agency-director", "agency-researcher", "agency-coder", "agency-resolver", "agency-critic",
)
CORE_SKILLS = (
    "studio-intake", "studio-lead", "studio-discovery-call", "studio-proposal", "studio-brand-voice",
    "studio-design-critique", "studio-email", "studio-linear-sync", "studio-retainer-report",
    "studio-maintenance-audit", "studio-gate", "studio-verify", "studio-grill",
)
# The historically-known Studio skill names above are used only to classify
# an already-installed skill directory (canonical-name-shadow, retired,
# etc.) regardless of which catalog version produced it -- unaffected by
# what the renderer generates by default going forward. What a migration
# should actually PROPOSE INSTALLING is the current frozen default catalog
# (Notion §31.8, 2026-09-18) instead.
DEFAULT_GENERATED_SKILLS = ("studio-gate", "studio-verify", "studio-run-engine", "studio-grill")
RETIRED_SKILLS = {
    "studio-launch", "studio-promote", "studio-resolve", "studio-weekly-retainer", "studio-weekly-audit",
    "studio-discovery", "studio-workflow-monitor", "studio-plan", "studio-wp-deploy", "studio-wp-content",
}
LEGACY_PROJECT_SKILLS = {"studio-design-decisions"}
PROJECT_PROFILE_COLLISIONS = {"studio-linear-sync": "project-profile", "studio-wp-audit": "project-profile-extension"}
GOVERNED_SKILLS = set(CORE_SKILLS) | set(PROJECT_PROFILE_COLLISIONS) | RETIRED_SKILLS | LEGACY_PROJECT_SKILLS
# Explicit allowlist, not a dotfile rule: only macOS Finder's `.DS_Store`.
HARMLESS_SKILL_METADATA = {".DS_Store"}
SECRET_KEY = re.compile(r"(?i)(?:^|[_-])(token|password|secret|credentials?|api[_-]?key|apikey|authorization)(?:$|[_-])")
PROJECT_REF = re.compile(r"\b[A-Z][A-Z0-9]+-[0-9]+\b")
LINEAR_SCOPE = re.compile(r"scoped to the `([^`]+)` project \(team `([^`]+)`\)", re.I)
WP_CAPABILITY_PATTERNS = (
    (re.compile(r"\betch(?:-based)?\b", re.I), "etch"),
    (re.compile(r"\brank\s+math\b", re.I), "rank-math"),
    (re.compile(r"\bpagespeed\b", re.I), "pagespeed"),
    (re.compile(r"\bpaper(?:\s+design)?\s+source\b", re.I), "paper-source"),
)
WP_DOCUMENT_MARKERS = ("# studio-wp-audit", "## Checks", "## Output", "## Rules")


class MigrationError(RuntimeError):
    pass


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _reject_secret_lines(content: str, source: str) -> None:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = stripped.split(":", 1)[0].split("=", 1)[0].strip()
        if SECRET_KEY.search(key):
            raise MigrationError(f"secret-bearing field is not allowed in migration profile: {source}:{key}")


def _extract_legacy_linear_scope(content: str) -> dict:
    match = LINEAR_SCOPE.search(content)
    if not match:
        return {}
    project, team = (value.strip() for value in match.groups())
    if not project or not team or len(project) > 128 or len(team) > 128:
        return {}
    return {"project": project, "team": team}


def _extract_wp_document(content: str) -> dict:
    if not all(marker in content for marker in WP_DOCUMENT_MARKERS):
        raise MigrationError("unrecognized project profile value in studio-wp-audit; inspect source locally")
    capabilities = []
    for pattern, capability in WP_CAPABILITY_PATTERNS:
        if pattern.search(content) and capability not in capabilities:
            capabilities.append(capability)
    refs = []
    for match in PROJECT_REF.finditer(content):
        value = match.group(0)
        if value not in refs:
            refs.append(value)
    return {"capabilities": capabilities, "project_refs": refs}


def _extract_profile(name: str, content: str) -> dict:
    _reject_secret_lines(content, name)
    if name == "studio-linear-sync":
        data = {}
        for line in content.splitlines():
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            if key in {"project", "team"} and value:
                data[key] = value
        return data
    if name == "studio-wp-audit":
        if any(marker in content for marker in WP_DOCUMENT_MARKERS):
            return _extract_wp_document(content)
        capabilities, project_refs = [], []
        simple = {"etch": "etch", "rank math": "rank-math", "pagespeed": "pagespeed", "paper source": "paper-source"}
        for line in content.splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            normalized = value.casefold()
            if normalized in simple:
                capability = simple[normalized]
                if capability not in capabilities:
                    capabilities.append(capability)
            elif PROJECT_REF.fullmatch(value):
                if value not in project_refs:
                    project_refs.append(value)
            else:
                raise MigrationError(f"unrecognized project profile value in {name}; inspect source locally")
        return {"capabilities": capabilities, "project_refs": project_refs}
    return {}


class MigrationPlanner:
    """Read-only deterministic migration planner for legacy Studio projects."""

    def __init__(self, project: Path):
        self.project = Path(project)

    def _skill_dirs(self):
        root = self.project / ".claude" / "skills"
        if not root.exists():
            return {}
        project_root = self.project.resolve()
        found = {}
        for path in sorted(root.iterdir(), key=lambda p: p.name):
            if path.is_symlink() and path.name in GOVERNED_SKILLS:
                try:
                    resolved = path.resolve(strict=True)
                except (FileNotFoundError, RuntimeError) as exc:
                    raise MigrationError(f"broken governed skill symlink: {path.name}") from exc
                if not resolved.is_relative_to(project_root):
                    raise MigrationError(f"governed skill symlink escapes project root: {path.name}")
            if path.is_dir():
                found[path.name] = path
        return found

    def _kit_managed(self, name: str, directory: Path) -> bool:
        """A Kit-installed renderer-profile skill, not legacy input: the dir
        holds only SKILL.md, recorded unmodified in the project `.claude`
        install journal. A regular-file Finder `.DS_Store` beside it is
        harmless metadata and does not change that identity (#99); any other
        entry still means not Kit-managed."""
        if directory.is_symlink():
            return False
        entries = [
            p.name for p in directory.iterdir()
            if not (p.name in HARMLESS_SKILL_METADATA and p.is_file() and not p.is_symlink())
        ]
        if entries != ["SKILL.md"]:
            return False
        try:
            journal = json.loads((self.project / ".claude" / StudioInstaller.JOURNAL).read_text(encoding="utf-8"))
            expected = journal["managed"][f"skills/{name}/SKILL.md"]["installed_sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return hashlib.sha256((directory / "SKILL.md").read_bytes()).hexdigest() == expected

    def _content(self, directory: Path) -> str:
        skill = directory / "SKILL.md"
        return skill.read_text(encoding="utf-8") if skill.exists() else ""

    def plan(self):
        skill_dirs = self._skill_dirs()
        preserve, quarantine, replace, install = [], [], [], []
        agents = self.project / "AGENTS.md"
        agents_content = agents.read_text(encoding="utf-8") if agents.exists() else ""
        if agents.exists():
            quarantine.append({"path": "AGENTS.md", "reason": "legacy-governance"})
        studio_agents = self.project / "_studio" / "AGENTS.md"
        if studio_agents.exists():
            quarantine.append({"path": "_studio/AGENTS.md", "reason": "legacy-governance"})

        claude = self.project / "CLAUDE.md"
        if claude.exists():
            content = claude.read_text(encoding="utf-8")
            if any(line.strip() == "@AGENTS.md" for line in content.splitlines()) and agents.exists():
                replace.append({
                    "path": "CLAUDE.md",
                    "reason": "legacy-governance-import",
                    "preserve_as": "project-instructions",
                    "source_sha256": _sha256_text(content),
                    "transformation": {"remove_exact_import": "@AGENTS.md"},
                })

        for name, directory in skill_dirs.items():
            rel = f".claude/skills/{name}"
            if name in RETIRED_SKILLS or name in LEGACY_PROJECT_SKILLS:
                quarantine.append({"path": rel, "reason": "retired-or-legacy-studio-capability"})
            elif name in PROJECT_PROFILE_COLLISIONS and self._kit_managed(name, directory):
                preserve.append({"path": rel, "reason": "unrelated-project-capability"})
            elif name in PROJECT_PROFILE_COLLISIONS:
                profile_data = _extract_profile(name, self._content(directory))
                if name == "studio-linear-sync" and not profile_data:
                    profile_data = _extract_legacy_linear_scope(agents_content)
                replace.append({"path": rel, "reason": "canonical-name-shadow", "preserve_as": PROJECT_PROFILE_COLLISIONS[name], "profile_data": profile_data})
            elif name in CORE_SKILLS:
                replace.append({"path": rel, "reason": "canonical-name-shadow", "preserve_as": None, "profile_data": {}})
            elif name not in GOVERNED_SKILLS:
                preserve.append({"path": rel, "reason": "unrelated-project-capability"})

        for agent in CANONICAL_AGENTS:
            install.append({"kind": "user-agent", "name": agent})
        for skill in DEFAULT_GENERATED_SKILLS:
            if skill not in skill_dirs:
                install.append({"kind": "user-skill", "name": skill})
        return {
            "version": "1.0-candidate", "project": self.project.name,
            "actions": {
                "preserve": sorted(preserve, key=lambda x: x["path"]),
                "quarantine": sorted(quarantine, key=lambda x: x["path"]),
                "replace": sorted(replace, key=lambda x: x["path"]),
                "install": sorted(install, key=lambda x: (x["kind"], x["name"])),
            },
        }
