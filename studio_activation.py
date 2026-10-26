#!/usr/bin/env python3
"""Composition layer over render_kit()/StudioInstaller/settings-merge that
answers "is this external project actually activated right now?" and, when
it is not, runs the full two-step activation recipe in one call.

Context (see the CG-01 dogfood finding this module exists to close): the
mechanism to activate Workflow Kit in an external project already exists --
StudioInstaller installs rendered agents/skills/hooks and (at project
scope only) composes the worktree-isolation settings.json recipe;
render_kit() renders the kit. What was missing was a single call that
chains both scopes together, and a read-only way to tell an Operator which
of four states a project is actually in. This module adds exactly that --
no new hashing, journaling, transaction, or settings-merge logic;
everything below is a direct caller of the existing primitives.

Per ADR 0004 (Front Door entry routing): activation no longer composes an
`"agent": "agency-director"` default into a project's settings.json.
Canonical flow is Operator -> Front Door -> DIRECT | DELEGATE | DIRECTOR;
the Front Door is the plain, unaddressed native Claude Code session itself
(studio_front_door.py), not a settings.json default that pre-routes every
session to the Director. Restoring a Director-as-default entry point would
require a new superseding ADR. `check_activation_state()` still surfaces
`project_settings_agent` as evidence -- it now tells an Operator whether a
legacy forced-Director default is still present in that project, not
whether activation succeeded.

The intended external-project model (see
_studio/kit/adapters/claude-code/README.md, "Required user-scope outputs" +
"Project scope"): agents/skills install once, globally, to ~/.claude/
(shared across every project on the machine); each individual external
project only needs its own .claude/hooks/studio_worktree_gate.py, the full
Front Door runtime closure at .claude/runtime/ (see
studio_renderer.FRONT_DOOR_RUNTIME_MODULES / RUNTIME_MANIFEST_RELATIVE --
this is what makes studio_front_door.py resolvable as an installed CLI),
plus a .claude/settings.json with the worktree-isolation recipe composed
into it. Project-scope agents/skills are reserved for intentional, audited
Operator overrides -- this module's project-local step therefore installs
only the rendered hooks/ and runtime/ subtrees, never agents/ or core
skills/, so activation itself can never shadow the global catalog.

Renderer-profile assets (#83) are the one project-scope skill exception:
a project's reviewed declaration (resolve_render_profile) selects the
render, its profile skills (e.g. studio-etch-author, studio-wp-audit for
wordpress-etch) install into that project's .claude/skills/, and the
user-global catalog stays the profile-independent core render. No global
copy exists for them to shadow, and one project's profile can never add
or remove assets another project on the same host sees.

The ADR 0008 plain-session entry rule (studio_renderer.ENTRY_RULE_RELATIVE,
`.claude/rules/agency-workflow-kit.md`) is the other project-scope-only
asset: it is part of the activation closure, is journaled like every other
closure file, and is excluded from the user-global view. Readiness checks it
at the exact project root given -- a linked worktree without its own copy is
not ready, whatever the primary checkout holds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from studio_installer import InstallError, StudioInstaller, _is_rel_path_confined
from studio_migration import WP_CAPABILITY_PATTERNS
from studio_migration_apply import MigrationApplier
from studio_renderer import (
    ENTRY_RULE_RELATIVE,
    FRONT_DOOR_RUNTIME_MODULES,
    RUNTIME_MANIFEST_RELATIVE as RENDERER_RUNTIME_MANIFEST_RELATIVE,
    RenderError,
    render_kit,
)
from studio_preflight import ClaudePreflight, probe_claude_version
from studio_project_manifest import (
    ProjectManifestError,
    detect_mode,
    ensure_project_manifest,
    read_project_manifest,
)
from studio_settings_merge import compose_worktree_isolation_settings

DIRECTOR_ASSET_KEY = "agents/agency-director.md"
GATE_HOOK_RELATIVE = Path("hooks") / "studio_worktree_gate.py"
# The full, exhaustive on-disk closure the Front Door CLI needs in order to
# actually run from this project: every module in
# studio_renderer.FRONT_DOOR_RUNTIME_MODULES (the renderer's own single
# source of truth for what it writes under runtime/), plus the manifest at
# studio_renderer.RUNTIME_MANIFEST_RELATIVE. Reusing that constant here
# (rather than a second hardcoded list) means a future change to the
# closure automatically flows through to this check -- unlike
# GATE_HOOK_RELATIVE, where the rendered hooks/ root genuinely contains
# exactly one file, so a single-path check there is already exhaustive.
RUNTIME_MODULE_RELATIVES = tuple(
    Path("runtime") / f"{module_name}.py" for module_name in FRONT_DOOR_RUNTIME_MODULES
)
RUNTIME_MANIFEST_RELATIVE = Path("runtime") / RENDERER_RUNTIME_MANIFEST_RELATIVE

# Renderer profile (render_kit(profile=...); not studio_workflow_profiles).
# The one project-side source is the reviewed declaration an authorized
# migration already materializes: .studio/project-profile-extension.json.
# Its `capabilities` use the migration's own extraction vocabulary; only
# `etch` selects a non-core render. Absent declaration means "core".
PROFILE_DECLARATION_RELATIVE = (
    Path(MigrationApplier.PROFILE_ROOT) / MigrationApplier.PROFILE_FILES["project-profile-extension"]
)
KNOWN_PROFILE_CAPABILITIES = frozenset(capability for _, capability in WP_CAPABILITY_PATTERNS)
CAPABILITY_RENDER_PROFILES = {"etch": "wordpress-etch"}


def resolve_render_profile(project_root: Path | None) -> str:
    """The project's renderer profile. Raises RenderError on a malformed or
    unknown declaration instead of silently falling back to "core"."""
    if project_root is None:
        return "core"
    path = Path(project_root) / PROFILE_DECLARATION_RELATIVE
    if not path.exists() and not path.is_symlink():
        return "core"
    try:
        declaration = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"malformed project profile declaration: {path}: {exc}") from exc
    capabilities = declaration.get("capabilities") if isinstance(declaration, dict) else None
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise RenderError(
            f"malformed project profile declaration: {path}: capabilities must be a list of strings"
        )
    unknown = sorted(set(capabilities) - KNOWN_PROFILE_CAPABILITIES)
    if unknown:
        raise RenderError(f"unknown project profile capability in {path}: {', '.join(unknown)}")
    profiles = sorted({CAPABILITY_RENDER_PROFILES[item] for item in capabilities if item in CAPABILITY_RENDER_PROFILES})
    if len(profiles) > 1:
        raise RenderError(f"conflicting renderer profiles in {path}: {', '.join(profiles)}")
    return profiles[0] if profiles else "core"


def render_project_kit(kit_dir: Path, output_dir: Path, project_root: Path | None) -> str:
    """render_kit() with the project's resolved renderer profile. Every
    install/inspect/update/activation render goes through here."""
    profile = resolve_render_profile(project_root)
    render_kit(kit_dir, output_dir, profile=profile)
    return profile


def _core_skill_names(rendered_dir: Path) -> frozenset[str]:
    manifest = json.loads((Path(rendered_dir) / RUNTIME_MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    return frozenset(manifest["catalog"]["core_generated_skills"])


def _skill_name(rel: str) -> str | None:
    parts = rel.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" else None


def profile_skill_rels(rendered_dir: Path) -> frozenset[str]:
    """Renderer-profile assets in this render: rendered skills outside the
    manifest's core catalog. They are project-scope, never user-global --
    the global catalog is shared by every project on the host."""
    skills = Path(rendered_dir) / "skills"
    if not skills.is_dir():
        return frozenset()
    core = _core_skill_names(rendered_dir)
    return frozenset(
        f"skills/{path.parent.name}/SKILL.md"
        for path in skills.glob("*/SKILL.md")
        if path.parent.name not in core
    )


def is_activation_closure_journal(managed: set[str], rendered_dir: Path) -> bool:
    """True when a project journal is an activation closure: the
    hooks/runtime closure plus only non-core skills (this render's profile
    assets, or a previous profile's still pending removal)."""
    core = _core_skill_names(rendered_dir)
    profile_like = {rel for rel in managed if _skill_name(rel) not in (None, *core)}
    rest = set(managed) - profile_like
    # An older activation journals an older closure: before the entry rule
    # (#101), before runtime/studio_authorization_gate.py (13d8dfa). It is
    # still an activation closure, so an update adds the newer files instead
    # of reintroducing project-scope agents. What separates any activation
    # from an update-only install is the gate hook without the global
    # catalog: no agents and no core skills.
    return GATE_HOOK_RELATIVE.as_posix() in rest and not any(
        rel.startswith(("agents/", "skills/")) for rel in rest
    )


def project_scope_only_rels(rendered_dir: Path) -> frozenset[str]:
    """Rendered assets no target other than the project's own .claude may
    receive: renderer-profile skills and the ADR 0008 entry rule."""
    return profile_skill_rels(rendered_dir) | {ENTRY_RULE_RELATIVE.as_posix()}


def _global_render_view(rendered_dir: Path, dest: Path) -> Path:
    """The user-global catalog: the render without its project-scope assets."""
    shutil.copytree(rendered_dir, dest)
    for rel in profile_skill_rels(rendered_dir):
        shutil.rmtree((dest / rel).parent)
    shutil.rmtree(dest / ENTRY_RULE_RELATIVE.parent, ignore_errors=True)
    return dest


def project_render_view(rendered_dir: Path, dest: Path) -> Path:
    """The project-scope activation closure as its own rendered tree."""
    for rel in sorted(activation_closure_rels(rendered_dir)):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(rendered_dir) / rel, dest / rel)
    return dest


def _journal_managed(claude_dir: Path) -> set[str]:
    try:
        journal = json.loads((claude_dir / StudioInstaller.JOURNAL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    managed = journal.get("managed") if isinstance(journal, dict) else None
    return set(managed) if isinstance(managed, dict) else set()


def _default_global_root() -> Path:
    """The one place the user-global Claude Code scope is spelled out --
    every other caller in this module (activate_external_project's own
    default, the CLI's --global-root default) routes through here instead
    of repeating "Path.home() / '.claude'" a second time."""
    return Path.home() / ".claude"


def activation_closure_rels(rendered_dir: Path | None = None) -> frozenset[str]:
    """Project-local paths activate_external_project() installs and journals.

    This is the hooks/runtime render and the ADR 0008 entry rule, not the
    global agent/skill catalog, plus the renderer-profile assets of
    ``rendered_dir`` when given.
    """
    return frozenset(
        {
            GATE_HOOK_RELATIVE.as_posix(),
            ENTRY_RULE_RELATIVE.as_posix(),
            *(relative.as_posix() for relative in RUNTIME_MODULE_RELATIVES),
            RUNTIME_MANIFEST_RELATIVE.as_posix(),
            *(profile_skill_rels(rendered_dir) if rendered_dir is not None else ()),
        }
    )


def project_settings_match_activation_contract(project_root: Path) -> bool:
    """Settings half of check_activation_state()'s project-ready predicate.

    "agent" is no longer a settings.json default this module composes
    (ADR 0004), so it cannot stand in as a proxy for composition. Reusing
    compose_worktree_isolation_settings() checks the worktree/gate keys
    composition owns: readiness requires that composing the current settings
    changes nothing, leaves nothing unapplied, and removes nothing stale.
    A project still carrying the legacy "agent": "agency-director" key is
    therefore not ready until it is re-composed and that key is gone.
    A pending migration is a removal, so it already shows up as a settings
    difference; there is no separate migrations clause.
    """
    settings_path = Path(project_root) / ".claude" / "settings.json"
    if not settings_path.is_file():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(settings, dict):
        return False
    composed = compose_worktree_isolation_settings(settings)
    return composed["settings"] == settings and not composed["conflicts"]


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_kit_version(manifest_path: Path) -> str | None:
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = (payload.get("kit") or {}).get("version") if isinstance(payload.get("kit"), dict) else None
    return version if isinstance(version, str) and version else None


def _project_closure_matches(rendered_dir: Path, project_root: Path, rels: frozenset[str]) -> bool:
    """Project activation closure must match the rendered bytes, not merely exist."""
    for relative in sorted(rels):
        source = rendered_dir / relative
        target = project_root / ".claude" / relative
        source_hash = _sha256_file(source)
        if source_hash is None or source_hash != _sha256_file(target):
            return False
    return True


def run_claude_preflight(
    rendered_dir: Path,
    project_root: Path,
    global_root: Path,
    *,
    claude_version: str | None = None,
    env=None,
) -> dict:
    """ClaudePreflight against this render's core catalog: the agents and
    core skills the user-global step installs. Renderer-profile skills are
    project scope (no global copy), so they can never be shadows. Project
    files the Kit journaled and that still match the journal are Kit-owned
    (the supported update path installs them), not unresolved shadowing."""
    rendered_dir = Path(rendered_dir)
    core_agents = {path.stem for path in (rendered_dir / "agents").glob("*.md")}
    return ClaudePreflight(
        global_root,
        project_root,
        core_agents=core_agents,
        core_skills=_core_skill_names(rendered_dir),
        env=env,
        claude_version=claude_version,
        kit_owned_rels=_kit_owned_unchanged_rels(Path(project_root) / ".claude"),
    ).inspect()


def _kit_owned_unchanged_rels(claude_dir: Path) -> frozenset[str]:
    """Journaled project paths whose bytes still match the install journal."""
    try:
        journal = json.loads((claude_dir / StudioInstaller.JOURNAL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    managed = journal.get("managed") if isinstance(journal, dict) else None
    if not isinstance(managed, dict):
        return frozenset()
    return frozenset(
        rel for rel, record in managed.items()
        if isinstance(record, dict) and _is_rel_path_confined(rel)
        and _sha256_file(claude_dir / rel) == record.get("installed_sha256")
    )


def check_activation_state(rendered_dir: Path, project_root: Path, global_root: Path) -> dict:
    """Read-only. Never writes anything, never raises on a missing/absent
    global or project install -- absence is reported as data (NOT_ACTIVATED
    / PROJECT_CONFIGURATION_MISSING), not an error.

    ACTIVATED requires the shared catalog to be the rendered Kit cut: every
    managed global asset matches the render, and the installed manifest's
    kit.version equals that render. A matching director file alone is not
    convergence. The project closure must also match the rendered bytes.

    Renderer-profile assets are project scope: the global catalog is scored
    without them, and the project's managed non-core skills must be exactly
    this render's profile assets (``profile_closure_matches``).
    """
    rendered_dir = Path(rendered_dir)
    project_root = Path(project_root)
    global_root = Path(global_root)

    with tempfile.TemporaryDirectory() as tmp:
        global_view = _global_render_view(rendered_dir, Path(tmp) / "global")
        verify_result = StudioInstaller(global_view, global_root).verify()
    director_asset = verify_result["assets"].get(
        DIRECTOR_ASSET_KEY,
        {"source_sha256": None, "target_sha256": None, "managed": False},
    )
    required_catalog_version = _read_kit_version(rendered_dir / RUNTIME_MANIFEST_RELATIVE)
    catalog_version = _read_kit_version(global_root / RUNTIME_MANIFEST_RELATIVE)
    global_closure_verified = verify_result.get("verified") is True
    catalog_converged = (
        global_closure_verified
        and required_catalog_version is not None
        and catalog_version == required_catalog_version
    )
    project_closure_matches = _project_closure_matches(
        rendered_dir, project_root, activation_closure_rels(rendered_dir)
    )
    profile_assets = profile_skill_rels(rendered_dir)
    core_skills = _core_skill_names(rendered_dir)
    managed_profile_assets = {
        rel for rel in _journal_managed(project_root / ".claude")
        if _skill_name(rel) not in (None, *core_skills)
    }
    profile_closure_matches = (
        _project_closure_matches(rendered_dir, project_root, profile_assets)
        and managed_profile_assets == profile_assets
    )

    project_hook_present = (project_root / ".claude" / GATE_HOOK_RELATIVE).is_file()
    project_entry_rule_matches = _project_closure_matches(
        rendered_dir, project_root, frozenset({ENTRY_RULE_RELATIVE.as_posix()})
    )
    project_runtime_present = all(
        (project_root / ".claude" / relative).is_file()
        for relative in (*RUNTIME_MODULE_RELATIVES, RUNTIME_MANIFEST_RELATIVE)
    )
    # _studio/project.json (#114): MISSING stays ready (projects activated
    # before the manifest existed get it on their next activation); an
    # INVALID manifest is a configuration defect and is never ready.
    project_manifest_state = read_project_manifest(project_root)["state"]
    settings_path = project_root / ".claude" / "settings.json"
    settings: dict = {}
    project_settings_agent = None
    project_settings_error = None
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            project_settings_error = str(exc)
            settings = {}
        else:
            if isinstance(settings, dict):
                project_settings_agent = settings.get("agent")
            else:
                project_settings_error = "settings.json does not contain a JSON object"
                settings = {}

    evidence = {
        "global_asset": director_asset,
        "catalog_version": catalog_version,
        "required_catalog_version": required_catalog_version,
        "global_closure_verified": global_closure_verified,
        "global_closure_count": verify_result.get("asset_count"),
        "project_closure_matches": project_closure_matches,
        "profile_assets": sorted(profile_assets),
        "profile_closure_matches": profile_closure_matches,
        "project_hook_present": project_hook_present,
        "project_entry_rule_matches": project_entry_rule_matches,
        "project_runtime_present": project_runtime_present,
        "project_settings_agent": project_settings_agent,
        "project_settings_error": project_settings_error,
        "project_manifest_state": project_manifest_state,
    }

    if director_asset.get("target_sha256") is None:
        return {"state": "NOT_ACTIVATED", **evidence}

    if (
        director_asset.get("source_sha256") != director_asset.get("target_sha256")
        or not director_asset.get("managed")
        or not catalog_converged
    ):
        return {"state": "STALE_GLOBAL_INSTALL", **evidence}

    project_ready = (
        project_hook_present
        and project_entry_rule_matches
        and project_runtime_present
        and project_closure_matches
        and profile_closure_matches
        and project_settings_match_activation_contract(project_root)
        and project_manifest_state != "INVALID"
    )
    if not project_ready:
        return {"state": "PROJECT_CONFIGURATION_MISSING", **evidence}

    return {"state": "ACTIVATED", **evidence}


def activate_external_project(
    kit_dir: Path,
    project_root: Path,
    *,
    global_root: Path | None = None,
    claude_version: str | None = None,
    env=None,
) -> dict:
    """One-call activation: render the kit once, install the full
    agents/skills/hooks set at global_root (with compose_settings=False:
    settings composition is project scope for every global root, not only
    the real ~/.claude that StudioInstaller._is_user_scope_target guards),
    then install only the rendered hooks/ subtree at
    <project_root>/.claude (which does compose settings.json's
    worktree/hook keys, since that target is not the user-global scope;
    per ADR 0004, composition no longer writes an "agent" key).
    Re-derives the final state via check_activation_state() rather than
    trusting the two apply() results alone.

    No new abstraction: this only chains render_kit() and
    StudioInstaller.plan()/apply() exactly as studio_installer.py's own
    main() already does, once per scope.

    Idempotent: relies entirely on StudioInstaller's own proven
    idempotent-rerun guarantee (unchanged managed files -> zero writes).

    Before any write it runs ClaudePreflight (run_claude_preflight) and
    validates an existing _studio/project.json; a preflight blocker or an
    invalid manifest raises InstallError with nothing written. After the
    project step it creates _studio/project.json once if absent (fresh
    identity; never overwritten). ``claude_version`` is the recorded
    `claude --version` (the CLI probes it); absent, it is reported as a
    certification blocker, never an install blocker.

    Raises InstallError (never a new exception type) on unresolved
    collisions, identifying which scope collided. The global step always
    runs first; if the project-local step then fails, the raised
    InstallError carries the already-completed global apply() result both
    in its message and as a `.global_result` attribute, so a caller can
    never lose visibility into a global step that already succeeded.
    """
    kit_dir = Path(kit_dir)
    project_root = Path(project_root)
    resolved_global_root = _default_global_root() if global_root is None else Path(global_root)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        full_rendered_path = tmp_path / "rendered-full"
        render_project_kit(kit_dir, full_rendered_path, project_root)

        preflight = run_claude_preflight(
            full_rendered_path, project_root, resolved_global_root, claude_version=claude_version, env=env
        )
        if preflight["blockers"]:
            raise InstallError(
                "activation blocked by Claude preflight before any write: "
                + ", ".join(preflight["blockers"])
                + " (retired skills / legacy contract: use studio_update.py preview/authorize/update)"
            )
        manifest_before = read_project_manifest(project_root)
        if manifest_before["state"] == "INVALID":
            raise InstallError(
                f"activation blocked before any write: {manifest_before['path']} is invalid: "
                + "; ".join(manifest_before["errors"])
            )
        mode = detect_mode(project_root)
        kit_version = _read_kit_version(full_rendered_path / RUNTIME_MANIFEST_RELATIVE)

        global_installer = StudioInstaller(
            _global_render_view(full_rendered_path, tmp_path / "rendered-global"), resolved_global_root
        )
        global_plan = global_installer.plan()
        if global_plan["collisions"]:
            raise InstallError(
                "activation blocked: unresolved collisions at global scope "
                f"({resolved_global_root}): " + ", ".join(global_plan["collisions"])
            )
        # The gate recipe is $CLAUDE_PROJECT_DIR-relative (project scope), so
        # no global root -- real ~/.claude or an alternate --global-root --
        # gets it; only the project step below composes settings (#106).
        global_result = global_installer.apply(global_plan, compose_settings=False)

        project_rendered_path = project_render_view(full_rendered_path, tmp_path / "rendered-project")

        project_claude_dir = project_root / ".claude"
        project_installer = StudioInstaller(project_rendered_path, project_claude_dir)
        try:
            project_plan = project_installer.plan()
            if project_plan["collisions"]:
                raise InstallError(
                    "activation blocked: unresolved collisions at project scope "
                    f"({project_claude_dir}): " + ", ".join(project_plan["collisions"])
                )
            project_result = project_installer.apply(project_plan)
            try:
                manifest = ensure_project_manifest(project_root, kit_version=kit_version, mode=mode)
            except (ProjectManifestError, OSError) as manifest_exc:
                raise InstallError(f"project manifest creation failed: {manifest_exc}") from manifest_exc
        except InstallError as exc:
            wrapped = InstallError(
                f"{exc} -- global scope step at {resolved_global_root} already "
                f"completed successfully: {global_result}"
            )
            wrapped.global_result = global_result
            raise wrapped from exc

        activation = check_activation_state(full_rendered_path, project_root, resolved_global_root)

    return {
        "global": global_result,
        "project": project_result,
        "activation": activation,
        "claude_preflight": preflight,
        "project_manifest": {
            "path": manifest["path"],
            "created": manifest["created"],
            "project_id": manifest["manifest"]["projectId"],
            "kit_version_current": manifest.get("kit_version_current"),
        },
    }


def _git_dirty_paths(project_root: Path) -> list[str]:
    """Best-effort, purely informational `git status --porcelain` read at
    project_root. Never raises: a non-git directory, a missing `git`
    executable, a timeout, or any other failure is reported as "no dirty
    paths" rather than propagated -- this helper's output is evidence only,
    never a decision input (see preflight_project_readiness's docstring for
    why git-dirtiness must never by itself gate the outcome)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def preflight_project_readiness(
    kit_dir: Path,
    project_root: Path,
    *,
    global_root: Path | None = None,
    claude_version: str | None = None,
    env=None,
) -> dict:
    """Read-only preflight: composes the existing read-only primitives
    (render_kit, StudioInstaller.verify()/plan(),
    compose_worktree_isolation_settings(), check_activation_state(), plus a
    local git-status read) into a single KEEP/ADD/FIX/BLOCK outcome an
    Operator can consult *before* deciding whether to run
    activate_external_project() at all. Never calls .apply() on any
    StudioInstaller and never writes to project_root or global_root.

    BLOCK comes exclusively from real installer/settings-composition
    signals -- an interrupted transaction surfaced by verify()/plan(), a
    plan() collision at either scope, a settings.json composition conflict,
    or a malformed existing settings.json. Git-dirty state (see
    `dirty_paths`) is surfaced as pure evidence and never itself blocks:
    real precedent in this codebase is a project's own .claude/settings.json
    left legitimately git-dirty by a prior, successful Workflow Kit
    activation, with zero installer collisions -- git-dirtiness and
    installer-collision are orthogonal signals.

    #114: the same ClaudePreflight activation runs (run_claude_preflight) and
    the _studio/project.json read are folded in. A preflight install
    blocker or an invalid project manifest is a BLOCK, exactly as
    activate_external_project() would refuse it; certification blockers
    (`claude --version` unresolved, model override) are reported only.
    """
    kit_dir = Path(kit_dir)
    project_root = Path(project_root)
    resolved_global_root = _default_global_root() if global_root is None else Path(global_root)

    dirty_paths = _git_dirty_paths(project_root)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        full_rendered_path = tmp_path / "rendered-full"
        render_project_kit(kit_dir, full_rendered_path, project_root)
        claude_preflight = run_claude_preflight(
            full_rendered_path, project_root, resolved_global_root, claude_version=claude_version, env=env
        )
        project_manifest = read_project_manifest(project_root)

        global_installer = StudioInstaller(
            _global_render_view(full_rendered_path, tmp_path / "rendered-global"), resolved_global_root
        )
        try:
            global_installer.verify()
        except InstallError as exc:
            return {
                "outcome": "BLOCK",
                "activation_state": None,
                "global_plan": None,
                "project_plan": None,
                "settings_conflicts": [],
                "settings_migrations": [],
                "settings_parse_error": None,
                "block_reason": f"global scope verify() failed: {exc}",
                "dirty_paths": dirty_paths,
                "claude_preflight": claude_preflight,
                "project_manifest": project_manifest,
            }

        global_plan = None
        project_plan = None
        try:
            global_plan = global_installer.plan()

            project_rendered_path = project_render_view(full_rendered_path, tmp_path / "rendered-project")

            project_claude_dir = project_root / ".claude"
            project_installer = StudioInstaller(project_rendered_path, project_claude_dir)
            try:
                project_installer.verify()
            except InstallError as exc:
                return {
                    "outcome": "BLOCK",
                    "activation_state": None,
                    "global_plan": global_plan,
                    "project_plan": None,
                    "settings_conflicts": [],
                    "settings_migrations": [],
                    "settings_parse_error": None,
                    "block_reason": f"project scope verify() failed: {exc}",
                    "dirty_paths": dirty_paths,
                "claude_preflight": claude_preflight,
                "project_manifest": project_manifest,
                }
            project_plan = project_installer.plan()
        except InstallError as exc:
            return {
                "outcome": "BLOCK",
                "activation_state": None,
                "global_plan": global_plan,
                "project_plan": project_plan,
                "settings_conflicts": [],
                "settings_migrations": [],
                "settings_parse_error": None,
                "block_reason": f"plan() failed: {exc}",
                "dirty_paths": dirty_paths,
                "claude_preflight": claude_preflight,
                "project_manifest": project_manifest,
            }

        collisions = list(global_plan["collisions"]) + list(project_plan["collisions"])

        settings_path = project_root / ".claude" / "settings.json"
        settings_conflicts: list[dict] = []
        settings_migrations: list[dict] = []
        settings_parse_error = None
        if settings_path.is_file():
            try:
                existing = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                settings_parse_error = str(exc)
            else:
                if isinstance(existing, dict):
                    settings_compose_result = compose_worktree_isolation_settings(existing)
                    settings_conflicts = settings_compose_result["conflicts"]
                    settings_migrations = settings_compose_result["migrations"]
                else:
                    settings_parse_error = "settings.json does not contain a JSON object"
        else:
            settings_compose_result = compose_worktree_isolation_settings({})
            settings_conflicts = settings_compose_result["conflicts"]
            settings_migrations = settings_compose_result["migrations"]

        activation_state = check_activation_state(full_rendered_path, project_root, resolved_global_root)

        block_reason = None
        if claude_preflight["blockers"]:
            outcome = "BLOCK"
            block_reason = "Claude preflight blockers: " + ", ".join(claude_preflight["blockers"])
        elif project_manifest["state"] == "INVALID":
            outcome = "BLOCK"
            block_reason = f"{project_manifest['path']} is invalid: " + "; ".join(project_manifest["errors"])
        elif collisions:
            outcome = "BLOCK"
            block_reason = "unresolved installer collisions: " + ", ".join(sorted(set(collisions)))
        elif settings_parse_error is not None:
            outcome = "BLOCK"
            block_reason = f"existing settings.json is malformed: {settings_parse_error}"
        elif settings_conflicts:
            outcome = "BLOCK"
            block_reason = "settings.json composition conflicts: " + ", ".join(
                str(conflict.get("field")) for conflict in settings_conflicts
            )
        elif activation_state["state"] == "ACTIVATED":
            # check_activation_state() already reported ACTIVATED, but
            # project_plan (computed above from the same StudioInstaller
            # primitives) is the ground truth on whether re-running
            # activate_external_project() would actually write anything at
            # project scope. Consulting it here -- rather than trusting
            # activation_state alone -- means a state-machine check that is
            # itself incomplete can never make preflight discard evidence
            # it already has. No parallel filesystem scan: project_plan is
            # reused as-is.
            # A missing _studio/project.json is one more write activation
            # would make (#114), so it is ADD, not KEEP.
            outcome = "ADD" if project_plan["writes"] or project_manifest["state"] == "MISSING" else "KEEP"
        elif activation_state["state"] == "STALE_GLOBAL_INSTALL":
            outcome = "FIX"
        else:  # NOT_ACTIVATED or PROJECT_CONFIGURATION_MISSING
            outcome = "ADD"

        return {
            "outcome": outcome,
            "activation_state": activation_state,
            "global_plan": global_plan,
            "project_plan": project_plan,
            "settings_conflicts": settings_conflicts,
            "settings_migrations": settings_migrations,
            "settings_parse_error": settings_parse_error,
            "block_reason": block_reason,
            "dirty_paths": dirty_paths,
            "claude_preflight": claude_preflight,
            "project_manifest": project_manifest,
        }


def uninstall_project(project_root: Path) -> dict:
    """Supported uninstall of one project's activation (SPEC §7, #114).

    The one uninstall engine is StudioInstaller.uninstall(): it recovers an
    interrupted transaction first, removes only journaled Kit-owned files
    whose bytes still match the journal, retains and reports modified ones
    (trimming the journal to them), and strips only the gate hook entries
    the Kit itself composed. This wrapper adds only scope and reporting:

    - project scope only. The user-global catalog is shared by every project
      on the host and is never removed ("Never uninstall shared dependencies
      automatically"); a home directory is refused as a project root.
    - user-owned files stay: _studio/project.json, the rest of
      .claude/settings.json (including the worktree keys activation
      composed, which carry no recorded original), and .studio-backups/.
    - idempotent: a second run removes nothing and reports the same
      retained modifications.

    Uninstall needs no render: it works from the journal alone, so a broken
    kit checkout or profile declaration cannot strand an installed project.
    """
    project_root = Path(project_root)
    if project_root.resolve() == Path.home().resolve():
        raise InstallError("uninstall refused: the home directory is not a project root")
    claude_dir = project_root / ".claude"
    installed = (claude_dir / StudioInstaller.JOURNAL).is_file() or (claude_dir / StudioInstaller.TRANSACTION).is_file()
    if installed:
        with tempfile.TemporaryDirectory() as empty_render:
            # StudioInstaller requires a rendered dir; uninstall() never reads it.
            result = StudioInstaller(Path(empty_render), claude_dir).uninstall()
    else:
        result = {"removed": [], "preserved_modified": []}
    if not installed:
        state = "NOT_INSTALLED"
    elif result["preserved_modified"]:
        state = "UNINSTALLED_RETAINED_MODIFIED"
    else:
        state = "UNINSTALLED"
    return {
        "state": state,
        "removed": result["removed"],
        "preserved_modified": result["preserved_modified"],
        "retained": {
            "project_manifest": (project_root / "_studio" / "project.json").is_file(),
            "settings_json": (claude_dir / "settings.json").is_file(),
            "backups": (claude_dir / StudioInstaller.BACKUP_ROOT).is_dir(),
        },
        "global_catalog": "untouched",
    }


def main() -> int:
    """Thin Operator-facing CLI, mirroring studio_installer.py's and
    studio_provider_detection.py's existing shape: --check-only performs no
    writes at all (renders into a temp dir, calls only
    check_activation_state, prints the JSON state); the default path runs
    the full activate_external_project() composition. Exit code is always
    derived from the resulting activation state, so scripting "did this
    fully succeed" never requires parsing the JSON body."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, default=Path("_studio/kit"))
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--global-root", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true",
                      help="read-only: report activation state without installing anything")
    mode.add_argument("--preflight", action="store_true",
                      help="read-only: KEEP/ADD/FIX/BLOCK readiness including Claude preflight; exits 1 on BLOCK")
    mode.add_argument("--uninstall", action="store_true",
                      help="remove this project's unchanged Kit-owned files; keeps modified and user-owned files "
                           "and never touches the user-global catalog")
    args = parser.parse_args()

    if args.uninstall:
        try:
            result = uninstall_project(args.project)
        except InstallError as exc:
            print(f"uninstall error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0

    global_root = args.global_root if args.global_root is not None else _default_global_root()

    if args.preflight:
        try:
            result = preflight_project_readiness(
                args.kit, args.project, global_root=global_root, claude_version=probe_claude_version()
            )
        except RenderError as exc:
            print(f"preflight error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 1 if result["outcome"] == "BLOCK" else 0

    if args.check_only:
        with tempfile.TemporaryDirectory() as tmp:
            rendered_dir = Path(tmp) / "rendered"
            try:
                render_project_kit(args.kit, rendered_dir, args.project)
            except RenderError as exc:
                print(f"activation error: {exc}", file=sys.stderr)
                return 1
            result = check_activation_state(rendered_dir, args.project, global_root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["state"] == "ACTIVATED" else 1

    try:
        result = activate_external_project(
            args.kit, args.project, global_root=global_root, claude_version=probe_claude_version()
        )
    except (InstallError, RenderError) as exc:
        print(f"activation error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0 if result["activation"]["state"] == "ACTIVATED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
