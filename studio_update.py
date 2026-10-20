#!/usr/bin/env python3
"""Project-local install/version/update/migration contract.

Not a second installer/updater framework: this module only composes four
already-proven pieces --

  * `render_kit`              (studio_renderer.py)  -- deterministic rendering
  * `StudioInstaller`         (studio_installer.py)  -- transactional asset
    plan/apply/verify. update() calls apply(compose_settings=False).
    Settings composition stays on activation's apply(); it is not an
    update mutation (ADR 0004, CONTRIBUTING activation contract)
  * `MigrationPlanner`/`MigrationApplier` (studio_migration.py /
    studio_migration_apply.py) -- the existing project-local migration
    dry-run/execute/rollback contract, including its
    `global_prerequisite_evidence` authorization gate
  * `WorkOrderRun`'s secret-rejection/state-validation discipline
    (studio_work_order.py) -- reused for the durable authorization record
    below, not reimplemented

The kit's own `manifest.json` (`kit.version`, `schema_version`) is the single
source of truth for "installed" vs. "candidate" version: the renderer already
copies it, unmodified, into every install's `runtime/_studio/kit/manifest.json`
(studio_renderer.FRONT_DOOR_RUNTIME_MODULES/RUNTIME_MANIFEST_RELATIVE), so
"installed version" is just reading that file back, not a new field to track.

Authorization model (Critic-revised): `preview()` is a pure read (no
target/project mutation -- including never triggering StudioInstaller's own
interrupted-transaction self-heal, which `plan()` performs unconditionally;
see the fail-closed check in `preview()` below) that returns a `fingerprint`
binding the exact current asset plan + migration plan + versions +
target/project IDENTITY. Binding identity closes a real replay path: without
it, two different targets needing an identical plan (e.g. two fresh installs
of the same kit version) would fingerprint identically, and an authorization
minted for one would silently validate against the other.

`authorize()` and `update()` are two separate calls, so a human-run
`authorize` (the Founder-facing operation) can genuinely be separated in
time and process from `update` (the execution-capability operation) -- this
is not a security boundary this module enforces in-process. `update()`
requires a path to a durable, atomic, attributable record -- not a bare
fingerprint value passed by reference -- and validates its CONTENT the same
way `WorkOrderRun` validates its own persisted `operator_gate` step
(schema-checked, secret-rejecting, strict `approved is True` identity check;
`WorkOrderRun._reject_secrets` is reused directly, not duplicated), and
re-checks the record's fingerprint AND target/project identity against a
freshly recomputed live preview() before any mutation, so a record can never
be replayed against a materially different state, a different target, or a
different project. What it does NOT do is prevent a caller who already has
`update()` in-process from also calling `authorize()` itself, or from
constructing an equivalent record by hand -- enforcing WHO is permitted to
invoke `authorize()` (i.e. that a human Founder, not arbitrary code, actually
approved it) is a permission/consent-layer concern outside this module,
exactly as `PlanGovernanceGate.evaluate()`'s `evidence["operator_authorized"]
is True` check (studio_plan_governance.py) is equally caller-supplied and
relies on the same external boundary, not on this module reimplementing an
unforgeability primitive Python cannot give it.

SCOPE BOUNDARY (produced as the classification artifact the Operator's
authorization-boundary requirement asked for): this module closes the
asset-install/migration half of "the Founder should not need to run routine
commands manually" -- that half was EXISTING-CAPABILITY-NOT-ROUTED
(StudioInstaller/MigrationApplier were already bounded, transactional,
idempotent, and not Bash/worktree-gated; only a non-Bash, Operator-consented
entry point into them was missing, and this module is that entry point). It
does NOT close the git-fetch/merge half of the original incident (an
activated project's own shared-checkout git operations): studio_worktree_gate.py
has no Operator-authorization bypass clause for Bash-based git mutation, and
no bounded git-execution capability exists anywhere in this codebase. That is
a GENUINE KIT CAPABILITY GAP, not a routing defect and not an intentional
bootstrap-only limitation (nothing in the ADRs documents "no Operator-
authorized git execution path" as permanent architecture) -- it is simply
unbuilt, and building it is out of this bounded slice's scope.

DISTRIBUTION SURFACE (Critic finding D4): this module is intentionally NOT
added to `studio_renderer.FRONT_DOOR_RUNTIME_MODULES`. That closure governs
what an ACTIVATED PROJECT's own Front Door session needs installed under its
`.claude/runtime/` to run its own Director/routing logic locally -- neither
`studio_installer` nor `studio_activation` (the two modules this one composes
and is architecturally identical in kind to) are in it either, for the same
reason: install/update/activation is a canonical-kit-checkout-side operation
performed AGAINST a project's `--target`/`--project` directories, not an
operation the activated project ever runs on itself. `studio_update.py` is
invoked the same way `studio_activation.py` already is (a human or an
authorized caller, running it from a Workflow Kit checkout) -- it is not
undistributed, it is correctly kit-side-only by the codebase's own existing
convention, and adding it to FRONT_DOOR_RUNTIME_MODULES would be a scope
inconsistency, not a fix.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from studio_activation import (
    _default_global_root,
    activation_closure_rels,
    check_activation_state,
    is_activation_closure_journal,
    project_render_view,
    project_scope_only_rels,
    project_settings_match_activation_contract,
    render_project_kit,
)
from studio_installer import InstallError, StudioInstaller
from studio_migration import (
    CORE_SKILLS,
    LEGACY_PROJECT_SKILLS,
    RETIRED_SKILLS,
    MigrationError,
    MigrationPlanner,
)
from studio_migration_apply import MigrationApplyError, MigrationApplier
from studio_renderer import RenderError, render_kit
from studio_work_order import WorkOrderError, WorkOrderRun

# Same GIT_*-stripped probe discipline as studio_worktree_gate /
# studio_run_events: an inherited GIT_DIR silently retargets answers.
_CHECKOUT_GIT_TIMEOUT_SECONDS = 5

RUNTIME_MANIFEST_RELATIVE = Path("runtime") / "_studio" / "kit" / "manifest.json"
EVIDENCE_DIR = ".studio-update-evidence"
AUTH_DIR = ".studio-update-authorization"
FINALIZATION_NAME = ".studio-update-finalization.json"

# Complete pre-runtime core generation (1.2.0-draft @ 4c838b4). That
# renderer did not emit runtime/. A schema-1 journal whose managed set and
# installed_sha256 values equal this map is a proven legacy-complete
# install. Unproven journal-without-manifest states stay fail-closed.
LEGACY_PRERUNTIME_GENERATION_COMMIT = "4c838b4056a305ebfb89550c03e84b1ec558b1e6"
LEGACY_PRERUNTIME_VERSION = "1.2.0-draft"
LEGACY_PRERUNTIME_SCHEMA_VERSION = "1.0"
LEGACY_PRERUNTIME_MANAGED_SHA256 = {
    "agents/agency-coder.md": "59e46134173ed1ff7f661890d4ffdce87b7528dc772e94b6a12fac0e9b11b8cb",
    "agents/agency-critic.md": "f8fb05d56c0f80e380b015c780451ef7c7878b2f44495847d9d32583a7b5a1c9",
    "agents/agency-director.md": "1d7c9820cbd6be84d88a4a5ce7fff7ec5d0aa496047b4efcbc5f4b4d7f5f5525",
    "agents/agency-researcher.md": "80df9114f1c79c14d3089041321252bafe4838ced6e36689ecbac3c50125ba3e",
    "agents/agency-resolver.md": "eccb5c11db7b8be72ac5b9a027b7919a5d7ba76aaab0a6ddef1d2a9600f19796",
    "hooks/studio_worktree_gate.py": "e44428025c0a5fd0e5f91c5a91d59f7f420c57e1dd929a2feb0e71fbd095365a",
    "skills/studio-gate/SKILL.md": "59b9a8a78c24ad9ff0205c95738f8aeb4b9ce270dfac8bcc13d3f181385c91f5",
    "skills/studio-grill/SKILL.md": "53f3052fc835861edea498a9abc812f7a2801104cda3fce767b01ef0753c12de",
    "skills/studio-run-engine/SKILL.md": "c5ce7f233e63a574538198a11a9dffff8e1ae6610a2ffc075ee4785a35b4632b",
    "skills/studio-verify/SKILL.md": "cdf9fc1b6ce5b1d02a6208845026955ac3e29433b31adf3f627e498ce70ced74",
}

# Unjournaled paths that remain project-owned across a legacy update preview.
_LEGACY_PROJECT_OWNED_RELS = (
    "settings.json",
    "settings.local.json",
    "launch.json",
)

# Reused only for its `_reject_secrets` guard (a plain, stateless recursive
# scan) -- this module's authorization/evidence records are not a
# WorkOrderRun's four-step lifecycle (there is no Director delegation or
# Critic review of an individual `update()` call), so WorkOrderRun.new_state/
# complete_step do not fit; the secret-rejection discipline is what's reused,
# not the step machinery.
_SECRET_GUARD = WorkOrderRun(work_id="studio-update")


def _reject_secrets(record: dict, path: str) -> None:
    """Reuses WorkOrderRun._reject_secrets, converted to this module's own
    UpdateError so a secret-bearing authorization record fails through the
    same error contract as every other validation failure in this module,
    rather than leaking WorkOrderError to callers who only catch UpdateError
    (main()'s own CLI error handling among them)."""
    try:
        _SECRET_GUARD._reject_secrets(record, path)
    except WorkOrderError as exc:
        raise UpdateError(str(exc)) from exc


class UpdateError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git_readonly(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Read-only git probe. Never used for stash/commit/reset/checkout/clean."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_CHECKOUT_GIT_TIMEOUT_SECONDS,
        env=_git_env(),
    )


def _path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _worktree_locked(worktree_path: Path) -> bool:
    """True when `git worktree list --porcelain` marks this worktree locked."""
    listing = _git_readonly(worktree_path, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return False
    current: str | None = None
    locked = False
    target = str(worktree_path.resolve())
    for raw in listing.stdout.splitlines():
        line = raw.strip()
        if line.startswith("worktree "):
            current = os.path.realpath(line[len("worktree ") :])
            locked = False
        elif line.startswith("locked") and current == target:
            locked = True
        elif line == "" and current == target:
            return locked
    return bool(current == target and locked)


def checkout_preflight(kit_dir: Path) -> dict:
    """Read-only kit-source checkout report for authorize/update.

    Records repo root, worktree path, branch, HEAD, dirty working tree,
    dirty index, stash presence, and protected-worktree status. Never
    mutates git state (no stash, commit, reset, checkout, clean, or
    branch switch). Does not participate in the preview fingerprint.
    """
    kit_dir = Path(kit_dir).resolve()
    report: dict = {
        "kit_dir": str(kit_dir),
        "repo_root": None,
        "worktree_path": None,
        "branch": None,
        "head": None,
        "dirty": None,
        "index_dirty": None,
        "stash_present": None,
        "protected_worktree": None,
        "is_linked_worktree": None,
        "intended_root": None,
        "ok": False,
        "refuse_reason": None,
    }

    toplevel = _git_readonly(kit_dir, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        report["refuse_reason"] = (
            "kit source is not inside a git worktree; "
            "authorize/update require a clean linked kit checkout"
        )
        return report

    worktree_path = Path(os.path.realpath(toplevel.stdout.strip()))
    report["worktree_path"] = str(worktree_path)

    binding = _git_readonly(worktree_path, "rev-parse", "--git-dir", "--git-common-dir")
    if binding.returncode != 0:
        report["refuse_reason"] = (
            "git could not resolve --git-dir/--git-common-dir for the kit checkout"
        )
        return report
    lines = [line.strip() for line in binding.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        report["refuse_reason"] = (
            "git rev-parse --git-dir/--git-common-dir returned an unexpected shape"
        )
        return report
    git_dir = os.path.realpath(os.path.join(str(worktree_path), lines[0]))
    git_common_dir = os.path.realpath(os.path.join(str(worktree_path), lines[1]))
    is_linked = git_dir != git_common_dir
    report["is_linked_worktree"] = is_linked
    if Path(git_common_dir).name == ".git":
        report["repo_root"] = str(Path(git_common_dir).parent)
    else:
        report["repo_root"] = str(worktree_path)

    head = _git_readonly(worktree_path, "rev-parse", "HEAD")
    if head.returncode != 0:
        report["refuse_reason"] = "git could not resolve HEAD for the kit checkout"
        return report
    report["head"] = head.stdout.strip()

    branch = _git_readonly(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    report["branch"] = branch.stdout.strip() if branch.returncode == 0 else None

    porcelain = _git_readonly(worktree_path, "status", "--porcelain")
    if porcelain.returncode != 0:
        report["refuse_reason"] = "git status --porcelain failed for the kit checkout"
        return report
    dirty = bool(porcelain.stdout.strip())
    report["dirty"] = dirty

    cached = _git_readonly(worktree_path, "diff", "--cached", "--quiet")
    # exit 1 => differences in index; 0 => clean; other => probe failure
    if cached.returncode not in (0, 1):
        report["refuse_reason"] = "git diff --cached failed for the kit checkout"
        return report
    index_dirty = cached.returncode == 1
    report["index_dirty"] = index_dirty

    stash = _git_readonly(worktree_path, "stash", "list")
    report["stash_present"] = bool(stash.stdout.strip()) if stash.returncode == 0 else None

    locked = _worktree_locked(worktree_path)
    protected = (not is_linked) or locked
    report["protected_worktree"] = protected

    intended = _path_is_under(kit_dir, worktree_path)
    report["intended_root"] = intended

    if not intended:
        report["refuse_reason"] = (
            f"kit source {kit_dir} is not under worktree {worktree_path}; "
            "authorize/update refuse a kit path outside the checkout root"
        )
        return report
    if dirty or index_dirty:
        report["refuse_reason"] = (
            "kit checkout is dirty (working tree and/or index); "
            "authorize/update refuse to write authorization from an unsettled tree "
            "and never stash, commit, reset, or clean to clear it"
        )
        return report
    if protected:
        why = "shared/main checkout" if not is_linked else "git-locked worktree"
        report["refuse_reason"] = (
            f"kit checkout is a protected worktree ({why}); "
            "authorize/update require a clean, unlocked linked worktree"
        )
        return report

    report["ok"] = True
    return report


def require_checkout_preflight(kit_dir: Path) -> dict:
    """Fail closed before authorize/update write or mutate.

    Returns the preflight report when the kit checkout is a clean,
    unlocked linked worktree that contains kit_dir. Raises UpdateError
    with the precise refuse_reason otherwise. Never mutates the tree.
    """
    report = checkout_preflight(kit_dir)
    if not report.get("ok"):
        reason = report.get("refuse_reason") or "kit checkout preflight failed"
        raise UpdateError(reason)
    return report


def _load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"invalid kit manifest: {path}: {exc}") from exc


def _fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def candidate_version(kit_dir: Path) -> dict:
    """Candidate (source-of-truth) kit identity: never touches target/project."""
    manifest = _load_manifest(Path(kit_dir) / "manifest.json")
    version = manifest.get("kit", {}).get("version")
    schema_version = manifest.get("schema_version")
    if not version or not schema_version:
        raise UpdateError(f"kit manifest missing kit.version/schema_version: {Path(kit_dir) / 'manifest.json'}")
    return {"version": version, "schema_version": schema_version}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_open_hold_error(target_dir: Path) -> str | None:
    """Return a fail-closed message when an open lifecycle hold is present."""
    if (target_dir / StudioInstaller.TRANSACTION).is_file():
        return (
            "interrupted install transaction exists; legacy recognition cannot "
            "proceed -- run recover() first"
        )
    if (target_dir / FINALIZATION_NAME).is_file():
        return (
            "update finalization is incomplete; legacy recognition cannot "
            "proceed -- run recover() first"
        )
    return None


def recognize_legacy_preruntime_install(target_dir: Path) -> dict | None:
    """Classify a target that may be a schema-1 pre-runtime install.

    Returns None for a genuine fresh/unmanaged target (no install journal)
    or for a normal modern install (runtime manifest present -- caller uses
    the manifest path). Raises UpdateError for every unproven
    journal-without-manifest state.

    States distinguished on failure message / return:
      * legacy_complete -- proven match to the known 1.2.0-draft@4c838b4 set
      * missing managed file / wrong type / hash mismatch (user-modified)
      * partial or corrupt / unknown generation (unproven)
      * open transaction or finalization
    Does not write a runtime manifest, delete the journal, or mutate files.
    """
    target_dir = Path(target_dir)
    journal_path = target_dir / StudioInstaller.JOURNAL
    if not journal_path.is_file():
        return None
    manifest_path = target_dir / RUNTIME_MANIFEST_RELATIVE
    if manifest_path.is_file():
        return None
    hold = _legacy_open_hold_error(target_dir)
    if hold:
        raise UpdateError(hold)
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"invalid install journal: {journal_path}: {exc}") from exc
    if not isinstance(journal, dict) or journal.get("schema_version") != 1:
        raise UpdateError(f"unsupported install journal: {journal_path}")
    managed = journal.get("managed")
    if not isinstance(managed, dict) or not managed:
        raise UpdateError(
            "installed journal exists but runtime manifest is missing and "
            "managed set is empty or invalid; unproven pre-runtime state"
        )
    observed: dict[str, str] = {}
    for rel, record in sorted(managed.items()):
        if not isinstance(rel, str) or not rel or not isinstance(record, dict):
            raise UpdateError(
                f"installed journal exists but runtime manifest is missing; "
                f"corrupt managed entry: {rel!r}"
            )
        expected = record.get("installed_sha256")
        if not isinstance(expected, str) or not expected:
            raise UpdateError(
                f"installed journal exists but runtime manifest is missing; "
                f"managed entry lacks installed_sha256: {rel}"
            )
        target = target_dir / rel
        if not target.exists():
            raise UpdateError(
                f"legacy pre-runtime install is incomplete; missing managed file: {rel}"
            )
        if not target.is_file():
            raise UpdateError(
                f"legacy pre-runtime install is corrupt; managed path is not a file: {rel}"
            )
        actual = _sha256_file(target)
        if actual != expected:
            raise UpdateError(
                f"legacy pre-runtime managed file does not match journal "
                f"installed_sha256 (user-modified or corrupt): {rel}"
            )
        observed[rel] = actual
    if observed != LEGACY_PRERUNTIME_MANAGED_SHA256:
        raise UpdateError(
            "installed journal exists but runtime manifest is missing; "
            "managed set is not the known complete pre-runtime generation "
            f"({LEGACY_PRERUNTIME_VERSION}@{LEGACY_PRERUNTIME_GENERATION_COMMIT[:7]}); "
            "unproven state stays fail-closed"
        )
    return {
        "state": "legacy_complete",
        "version": LEGACY_PRERUNTIME_VERSION,
        "schema_version": LEGACY_PRERUNTIME_SCHEMA_VERSION,
        "generation_commit": LEGACY_PRERUNTIME_GENERATION_COMMIT,
        "managed": dict(observed),
        "journal_path": str(journal_path),
        # Recognition never synthesizes on-disk runtime identity.
        "runtime_manifest_present": False,
    }


def installed_version(target_dir: Path) -> dict | None:
    """Installed kit identity read from the already-installed runtime
    manifest copy. Returns None only for a genuinely fresh (never installed)
    target -- no install journal at all.

    A proven legacy-complete schema-1 pre-runtime install (no runtime
    manifest, journal matches the known 1.2.0-draft@4c838b4 core set) returns
    that generation's version identity with ``legacy_preruntime=True``. Other
    journal-without-manifest states remain fail-closed.
    """
    target_dir = Path(target_dir)
    journal_path = target_dir / StudioInstaller.JOURNAL
    if not journal_path.is_file():
        return None
    manifest_path = target_dir / RUNTIME_MANIFEST_RELATIVE
    if not manifest_path.is_file():
        legacy = recognize_legacy_preruntime_install(target_dir)
        if legacy is None or legacy.get("state") != "legacy_complete":
            raise UpdateError(f"installed journal exists but runtime manifest is missing: {manifest_path}")
        return {
            "version": legacy["version"],
            "schema_version": legacy["schema_version"],
            "legacy_preruntime": True,
            "generation_commit": legacy["generation_commit"],
        }
    manifest = _load_manifest(manifest_path)
    version = manifest.get("kit", {}).get("version")
    schema_version = manifest.get("schema_version")
    if not version or not schema_version:
        raise UpdateError(f"installed kit manifest missing kit.version/schema_version: {manifest_path}")
    return {"version": version, "schema_version": schema_version}


def _lifecycle_preview_actions(
    asset_plan: dict,
    migration_plan: dict,
    *,
    target_dir: Path,
    installed: dict | None,
) -> dict:
    """Explicit add/update/preserve/migrate/refuse buckets for preview evidence.

    Derived from the existing installer asset plan and MigrationPlanner plan;
    does not bypass studio_migration ownership.
    """
    managed = _journal_managed_rels(target_dir) or set()
    add: list[str] = []
    update: list[str] = []
    for rel in asset_plan.get("writes") or []:
        if rel in managed:
            update.append(rel)
        else:
            add.append(rel)
    preserve: list[dict] = [
        {"path": rel, "reason": "content-matches-candidate"}
        for rel in asset_plan.get("satisfied") or []
    ]
    for rel in _LEGACY_PROJECT_OWNED_RELS:
        path = target_dir / rel
        if path.is_file() and rel not in managed:
            preserve.append({"path": rel, "reason": "unjournaled-project-owned"})
    for item in (migration_plan.get("actions") or {}).get("preserve") or []:
        if isinstance(item, dict):
            preserve.append(dict(item))
    migrate: list[dict] = []
    for key in ("quarantine", "replace"):
        for item in (migration_plan.get("actions") or {}).get(key) or []:
            if isinstance(item, dict):
                migrate.append({**item, "action": key})
    refuse = [
        {"path": rel, "reason": "collision"}
        for rel in asset_plan.get("collisions") or []
    ]
    for rel in asset_plan.get("removals") or []:
        update.append(rel)
    return {
        "add": sorted(add),
        "update": sorted(set(update)),
        "preserve": sorted(preserve, key=lambda item: item.get("path", "")),
        "migrate": sorted(migrate, key=lambda item: item.get("path", "")),
        "refuse": sorted(refuse, key=lambda item: item.get("path", "")),
        "legacy_preruntime": bool(installed and installed.get("legacy_preruntime")),
    }




def _journal_managed_rels(target_dir: Path) -> set[str] | None:
    journal_path = target_dir / StudioInstaller.JOURNAL
    if not journal_path.is_file():
        return None
    try:
        data = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    managed = data.get("managed") if isinstance(data, dict) else None
    if not isinstance(managed, dict):
        return None
    return set(managed)


def _update_available(installed: dict | None, candidate: dict, asset_plan: dict | None = None) -> bool:
    """Whether an update should be offered.

    Opaque version-string inequality remains the primary signal (no SemVer
    parser). When the version strings match, a non-empty asset plan write or
    removal set means install/runtime identity drifted (different closure or
    write-set under the same kit.version) and must not report "up to date".
    """
    if installed is None:
        return True
    if installed["version"] != candidate["version"]:
        return True
    if asset_plan is not None and (asset_plan.get("writes") or asset_plan.get("removals")):
        return True
    return False


def _is_activated_project_closure(target_dir: Path, project_dir: Path, rendered: Path) -> bool:
    """True when this target is the project's .claude and its journal is
    the activation closure.

    activate_external_project() rewrites that journal to the hooks/runtime
    render plus renderer-profile skills. An update-only install journals a
    wider set, including the five agents and studio-run-engine. No second
    journal is required.
    """
    try:
        overlapping = target_dir.resolve() == (project_dir.resolve() / ".claude")
    except OSError:
        return False
    if not overlapping:
        return False
    managed = _journal_managed_rels(target_dir)
    return managed is not None and is_activation_closure_journal(managed, rendered)


def _activation_closure_health(rendered: Path, target_dir: Path, project_dir: Path) -> dict:
    """Score the activation project-local contract against target_dir.

    The desired set is the hooks/runtime closure plus renderer-profile
    skills. Settings readiness uses the activation contract. Agents and
    studio-run-engine are not required. A hash mismatch or a settings
    contract miss forces verified false.
    """
    closure_rendered = project_render_view(rendered, rendered.parent / "rendered-closure")
    report = StudioInstaller(closure_rendered, target_dir).verify()
    if not project_settings_match_activation_contract(project_dir):
        report = {**report, "verified": False}
    return report


def inspect(
    kit_dir: Path,
    target_dir: Path,
    project_dir: Path | None = None,
    *,
    global_root: Path | None = None,
) -> dict:
    """Read-only health/version inspection. Renders the candidate kit into a
    private temporary directory (never the target) purely to compare hashes;
    performs no privileged mutation. When the target is an activated
    project's ``.claude`` (journal equals the activation closure), health
    scores that closure plus the activation settings contract. When
    ``project_dir`` makes the target that project's ``.claude`` and it has
    not been activated, health uses the same overlap omissions as
    ``update()``. Otherwise health is ``StudioInstaller.verify()``.
    StudioInstaller.verify() already fails closed on an interrupted
    transaction (raises InstallError rather than recovering it) -- that is
    correct fail-closed *behavior*, but InstallError is not UpdateError, so
    it must be translated here or it escapes this module's own error
    contract (Critic finding D6: main()'s `except UpdateError` did not catch
    it, producing a raw traceback on stderr instead of a clean message).

    ``health`` is project integrity only. Readiness (#83) is not inferred
    from it: with ``project_dir`` and ``global_root`` given, ``activation``
    is the exact ``check_activation_state()`` result for the same render
    (global catalog closure, renderer-profile closure, project closure,
    settings contract), and ``ready`` is true only when health is verified
    AND that state is ACTIVATED. Without a global root, readiness is not
    assessed (``activation`` and ``ready`` are None) rather than guessed.

    ``update_blocker`` (#111) is the refusal preview()/update() would hit for
    the same target and candidate -- the MigrationPlanner error or unresolved
    asset collisions -- or None. ``ready`` is also false while it is set."""
    kit_dir, target_dir = Path(kit_dir), Path(target_dir)
    candidate = candidate_version(kit_dir)
    installed = installed_version(target_dir)
    health = None
    asset_plan = None
    activation = None
    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered"
        try:
            render_profile = render_project_kit(kit_dir, rendered, project_dir)
        except RenderError as exc:
            raise UpdateError(str(exc)) from exc
        try:
            if installed is not None:
                installer = StudioInstaller(rendered, target_dir)
                if project_dir is not None:
                    _configure_overlap_omissions(installer, Path(project_dir))
                else:
                    # No project: nothing marks this target as a project's own
                    # .claude, so project-scope-only assets are not required.
                    installer.omitted_rels = set(project_scope_only_rels(rendered))
                if not installer.transaction_path.is_file():
                    asset_plan = installer.plan(recover_interrupted=False)
                if project_dir is not None and _is_activated_project_closure(target_dir, Path(project_dir), rendered):
                    health = _activation_closure_health(rendered, target_dir, Path(project_dir))
                else:
                    health = _final_managed_verification(installer, set())
            if project_dir is not None and global_root is not None:
                activation = check_activation_state(rendered, Path(project_dir), Path(global_root))
        except InstallError as exc:
            raise UpdateError(str(exc)) from exc
    # #111: the same refusals preview()/update() enforce, reported instead of
    # raised. MigrationPlanner.plan() is read-only; its error is preview's
    # (OSError too: an unreadable managed SKILL.md is reported, not raised).
    update_blocker = None
    if asset_plan is not None and asset_plan.get("collisions"):
        update_blocker = "unresolved collisions: " + ", ".join(asset_plan["collisions"])
    if project_dir is not None:
        try:
            MigrationPlanner(Path(project_dir)).plan()
        except (MigrationError, OSError) as exc:
            update_blocker = str(exc)
    ready = None
    if activation is not None:
        ready = bool(
            health and health.get("verified") is True and activation["state"] == "ACTIVATED"
            and update_blocker is None
        )
    return {
        "installed": installed,
        "candidate": candidate,
        "update_available": _update_available(installed, candidate, asset_plan),
        "health": health,
        "render_profile": render_profile,
        "activation": activation,
        "ready": ready,
        "update_blocker": update_blocker,
    }


def preview(kit_dir: Path, target_dir: Path, project_dir: Path) -> dict:
    """Non-mutating update preview: candidate/installed versions, the exact
    asset delta (StudioInstaller.plan(), read-only), and any required
    project-local migration (MigrationPlanner.plan(), read-only), bound
    together by a fingerprint that changes with any of those inputs -- and
    with the exact target/project this preview describes (Critic finding D2:
    without that, two different targets needing an identical plan would
    fingerprint identically)."""
    kit_dir, target_dir, project_dir = Path(kit_dir), Path(target_dir), Path(project_dir)
    if (target_dir / FINALIZATION_NAME).is_file():
        raise UpdateError(
            "update finalization is incomplete; preview cannot finish it -- run recover() first"
        )
    candidate = candidate_version(kit_dir)
    installed = installed_version(target_dir)
    compatible = installed is None or installed["schema_version"] == candidate["schema_version"]

    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered"
        try:
            render_profile = render_project_kit(kit_dir, rendered, project_dir)
        except RenderError as exc:
            raise UpdateError(str(exc)) from exc
        installer = StudioInstaller(rendered, target_dir)
        _configure_overlap_omissions(installer, project_dir)
        # Critic finding D1: StudioInstaller.plan() unconditionally calls
        # _recover_interrupted_transaction() as its first action -- a real
        # target mutation. verify() already refuses to proceed with an
        # existing transaction file rather than silently recovering it
        # (studio_installer.py); plan() has no equivalent guard, so preview()
        # must add the identical fail-closed check itself before calling it.
        if installer.transaction_path.is_file():
            raise UpdateError(
                "interrupted install transaction exists; preview cannot recover or "
                "mutate it -- run recover() first"
            )
        try:
            asset_plan = installer.plan(recover_interrupted=False)
        except InstallError as exc:
            raise UpdateError(str(exc)) from exc

    # Critic finding D12: MigrationPlanner.plan() raises MigrationError on a
    # dangling governed-skill symlink (and related fail-closed cases) --
    # preview() must translate it to UpdateError (with MigrationError as
    # __cause__) so main()'s `except UpdateError` catches it instead of
    # letting a raw MigrationError traceback escape. authorize()/update()
    # inherit this contract via preview().
    try:
        migration_plan = MigrationPlanner(project_dir).plan()
    except MigrationError as exc:
        raise UpdateError(str(exc)) from exc
    migration_required = bool(migration_plan["actions"]["replace"] or migration_plan["actions"]["quarantine"])

    lifecycle_actions = _lifecycle_preview_actions(
        asset_plan,
        migration_plan,
        target_dir=target_dir,
        installed=installed,
    )
    delta = {
        "installed": installed,
        "candidate": candidate,
        "update_available": _update_available(installed, candidate, asset_plan),
        "compatible": compatible,
        # Renderer profile resolved from the project's declaration; update()
        # renders exactly this profile, so a profile change re-authorizes.
        "render_profile": render_profile,
        "asset_plan": asset_plan,
        "migration_required": migration_required,
        "migration_plan": migration_plan,
        # Explicit add/update/preserve/migrate/refuse evidence (WK-LEGACY-02).
        "lifecycle_actions": lifecycle_actions,
        "target_identity": str(target_dir.resolve()),
        "project_identity": str(project_dir.resolve()),
        # Bound into the fingerprint. Update never mutates settings.json.
        # Activation owns that composition. Flipping mutates_settings_json
        # changes the fingerprint and invalidates an existing authorization.
        "settings_composition": {
            "mutates_settings_json": False,
            "owner": "activation",
        },
    }
    return {**delta, "fingerprint": _fingerprint(delta)}


def authorize(kit_dir: Path, target_dir: Path, project_dir: Path, *, requested_by: str) -> dict:
    """The Founder-facing operation that produces an update authorization
    record -- the only *function* in this module that writes one, kept
    separate from `update()` (the execution-capability operation) so a
    human-run `authorize` can genuinely be a distinct step in time and
    process from `update`. Recomputes a fresh preview() and writes it as a
    durable, atomic record binding: the fingerprint, the exact target/project
    identity it was computed against, who requested it, and an explicit
    `approved: True`. `update()` requires a PATH to one of these records
    rather than a bare fingerprint value, and re-validates its content
    (schema, secrets, `approved is True`, fingerprint, target/project
    identity) against a freshly recomputed live preview(). This module does
    NOT enforce who is permitted to call `authorize()` -- that a human
    Founder, not arbitrary code, actually approved a given update -- any
    more than `PlanGovernanceGate.evaluate()`'s `operator_authorized`
    evidence enforces who set it to `True`; both rely on an external
    permission/consent boundary, not on an in-process unforgeability
    primitive.

    Before writing the record, require_checkout_preflight(kit_dir) fails
    closed when the kit source checkout is dirty, protected (shared/main
    or git-locked), or not the intended worktree root. The preflight
    report is attached to the durable record for Operator approval-card
    attachment; it is not part of the preview fingerprint.
    """
    kit_dir, target_dir, project_dir = Path(kit_dir), Path(target_dir), Path(project_dir)
    preflight = require_checkout_preflight(kit_dir)
    current = preview(kit_dir, target_dir, project_dir)
    record = {
        "schema_version": 1,
        "fingerprint": current["fingerprint"],
        "target_identity": current["target_identity"],
        "project_identity": current["project_identity"],
        "requested_by": requested_by,
        "approved": True,
        "timestamp_ns": time.time_ns(),
        "checkout_preflight": preflight,
    }
    _reject_secrets(record, "authorization")
    path = target_dir / AUTH_DIR / f"{record['timestamp_ns']}.json"
    _atomic_json(path, record)
    return {**record, "path": str(path)}


def _finalization_path(target_dir: Path) -> Path:
    return target_dir / FINALIZATION_NAME


def _load_finalization(path: Path) -> dict:
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"unreadable update finalization record: {path}: {exc}") from exc
    if (
        not isinstance(pending, dict)
        or pending.get("schema_version") != 1
        or pending.get("state") not in {"INTENT", "PENDING"}
    ):
        raise UpdateError(f"unsupported update finalization record: {path}")
    return pending


def _finish_finalization(
    installer: StudioInstaller,
    project_dir: Path,
    target_dir: Path,
    *,
    finalized_by: str,
) -> dict:
    """Reconcile the journal and write update evidence from a pending record.

    The asset install and the migration have already committed. This does not
    roll either of them back. A second call writes the evidence file only if
    it is missing, then removes the pending record.
    """
    path = _finalization_path(target_dir)
    pending = _load_finalization(path)
    evidence_path = target_dir / pending["evidence_rel"]
    if not evidence_path.is_file():
        reconciled = _reconcile_overlapping_journal(
            installer, project_dir, pending["migration_plan"],
        )
        verification = _final_managed_verification(installer, set(reconciled))
        evidence = {
            "schema_version": 1,
            "operation": "update",
            "authorized_fingerprint": pending["authorized_fingerprint"],
            "authorized_by": pending["authorized_by"],
            "authorization_record": pending["authorization_record"],
            "executed_by": pending["executed_by"],
            "timestamp_ns": pending["timestamp_ns"],
            "render_profile": pending.get("render_profile"),
            "pre_state": pending["pre_state"],
            "post_state": {"installed": installed_version(target_dir)},
            "install_result": pending["install_result"],
            "migration_result": pending["migration_result"],
            "post_update_verification": verification,
            "finalized_by": finalized_by,
        }
        _atomic_json(evidence_path, evidence)
    path.unlink()
    return json.loads(evidence_path.read_text(encoding="utf-8"))


def _migration_journal_state(project_dir: Path, state_base: Path | None, fingerprint: str) -> str:
    """State of the migration journal bound to this finalization fingerprint."""
    applier = MigrationApplier(project_dir, state_base=state_base)
    if not applier.state_root.is_dir():
        return "ABSENT"
    states = []
    for journal_path in applier.state_root.glob("*/journal.json"):
        try:
            data = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("source_fingerprint") != fingerprint:
            continue
        state = data.get("state")
        if isinstance(state, str):
            states.append(state)
    if "APPLYING" in states:
        return "APPLYING"
    if "COMMITTED" in states:
        return "COMMITTED"
    if "ROLLED_BACK" in states:
        return "ROLLED_BACK"
    return "ABSENT"


def _committed_migration_result(project_dir: Path, state_base: Path | None, fingerprint: str) -> dict:
    """Rebuild the update evidence migration result from the committed journal."""
    applier = MigrationApplier(project_dir, state_base=state_base)
    for journal_path in sorted(applier.state_root.glob("*/journal.json")):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if journal.get("source_fingerprint") != fingerprint or journal.get("state") != "COMMITTED":
            continue
        manifest_path = journal_path.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {
            "status": "APPLIED",
            "source_fingerprint": fingerprint,
            "quarantine_dir": str(journal_path.parent / "quarantine"),
            "transaction": str(manifest_path),
            "verification": manifest.get("verification"),
            "global_actions_deferred": manifest.get("global_actions_deferred"),
            "global_prerequisite_evidence": manifest.get("global_prerequisite_evidence"),
        }
    raise UpdateError("committed migration artifact missing for update finalization")


def recover(kit_dir: Path, target_dir: Path, project_dir: Path, *, state_base: Path | None = None) -> dict:
    """Heal an interrupted install or migration transaction, or finish a
    persisted update finalization.

    An open installer transaction and an APPLYING migration journal are
    restored to their pre-transaction state. An INTENT finalization record is
    written before migration commits. If the matching migration journal is
    COMMITTED, recover keeps that migration, commits a still-open installer
    transaction without rolling it back, and finishes reconciliation and
    evidence. Otherwise the INTENT record is discarded and the pre-update
    tree is restored.
    """
    kit_dir, target_dir, project_dir = Path(kit_dir), Path(target_dir), Path(project_dir)
    installer_recovered = False
    migration_recovered = []
    finalization_completed = False
    finalization = _finalization_path(target_dir)
    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered"
        try:
            # A pending finalization finishes against the profile update()
            # rendered, not one a committed migration may have declared since.
            # A record written before #83 has no key; that update rendered core.
            if finalization.is_file():
                profile = _load_finalization(finalization).get("render_profile") or "core"
                render_kit(kit_dir, rendered, profile=profile)
            else:
                render_project_kit(kit_dir, rendered, project_dir)
        except RenderError as exc:
            raise UpdateError(str(exc)) from exc
        installer = StudioInstaller(rendered, target_dir)
        _configure_overlap_omissions(installer, project_dir)
        phase = "ABSENT"
        if finalization.is_file():
            pending = _load_finalization(finalization)
            phase = _migration_journal_state(
                project_dir, state_base, pending["migration_fingerprint"],
            )
        if finalization.is_file() and phase == "COMMITTED":
            if installer.transaction_path.is_file():
                installer.transaction_path.unlink()
            pending = _load_finalization(finalization)
            if pending.get("state") != "PENDING" or pending.get("migration_result") is None:
                pending["state"] = "PENDING"
                pending["migration_result"] = _committed_migration_result(
                    project_dir, state_base, pending["migration_fingerprint"],
                )
                _atomic_json(finalization, pending)
            _finish_finalization(
                installer, project_dir, target_dir, finalized_by="recover",
            )
            finalization_completed = True
        else:
            if finalization.is_file():
                finalization.unlink()
            had_transaction = installer.transaction_path.is_file()
            try:
                installer.plan()  # triggers _recover_interrupted_transaction() as a side effect
            except InstallError as exc:
                raise UpdateError(str(exc)) from exc
            installer_recovered = had_transaction
    if not finalization_completed:
        migration_recovered = MigrationApplier(project_dir, state_base=state_base).recover_incomplete()
    return {
        "installer_recovered": installer_recovered,
        "migration_recovered": migration_recovered,
        "finalization_completed": finalization_completed,
    }




def _configure_overlap_omissions(installer: StudioInstaller, project_dir: Path) -> set[str]:
    """On the CLI-default layout, do not plan project copies of CORE_SKILLS.

    Those names are canonical-name shadows when they appear under the
    project's `.claude`. The installer must not write them back on a later
    same-version update. A separate installer target omits only the
    project-scope-only assets (renderer-profile skills and the ADR 0008
    entry rule): no target other than the project's own `.claude` (the
    user-global catalog included) may receive them. ``StudioInstaller.verify()`` is not affected.

    When the target journal is already the activation closure, also omit
    every rendered source outside that closure (the five agency agents and
    ``studio-run-engine``; renderer-profile skills are inside it). Activation keeps those global-only; a
    same-version update must not reintroduce project-scope copies.
    """
    try:
        overlapping = installer.target_dir.resolve() == (project_dir.resolve() / ".claude")
    except OSError:
        overlapping = False
    if not overlapping:
        installer.omitted_rels = set(project_scope_only_rels(installer.rendered_dir))
        return set(installer.omitted_rels)
    sources = installer._sources()
    omitted = {
        rel
        for rel in sources
        if rel.split("/")[:1] == ["skills"] and len(rel.split("/")) >= 2 and rel.split("/")[1] in CORE_SKILLS
    }
    if _is_activated_project_closure(installer.target_dir, project_dir, installer.rendered_dir):
        closure = activation_closure_rels(installer.rendered_dir)
        omitted |= {rel for rel in sources if rel not in closure}
    installer.omitted_rels = omitted
    return omitted


def _migration_moved_project_rels(plan: dict) -> list[str]:
    """Project-relative paths a migration removes from the project tree.

    In-place transformations stay on disk. Quarantine and non-transforming
    replace actions are the moves that can overlap an installer journal.
    """
    actions = plan.get("actions") if isinstance(plan, dict) else None
    if not isinstance(actions, dict):
        return []
    moved: list[str] = []
    for action in actions.get("quarantine") or []:
        path = action.get("path") if isinstance(action, dict) else None
        if isinstance(path, str) and path:
            moved.append(path)
    for action in actions.get("replace") or []:
        if not isinstance(action, dict) or "transformation" in action:
            continue
        path = action.get("path")
        if isinstance(path, str) and path:
            moved.append(path)
    return moved


def _reconcile_overlapping_journal(installer: StudioInstaller, project_dir: Path, plan: dict) -> list[str]:
    """Remove installer-managed paths that this migration moved off the final tree.

    Journal paths are target-relative. Migration paths are project-relative.
    A managed file overlaps only when its target path is the moved path or a
    file inside it. A separate installer target does not overlap the project's
    `.claude` tree, so that journal is left unchanged.
    """
    moved_rels = _migration_moved_project_rels(plan)
    if not moved_rels or not installer.journal_path.is_file():
        return []
    journal = json.loads(installer.journal_path.read_text(encoding="utf-8"))
    managed = journal.get("managed")
    if not isinstance(managed, dict) or not managed:
        return []
    project_root = project_dir.resolve()
    moved_paths = []
    for rel in moved_rels:
        moved = (project_dir / rel).resolve()
        if moved == project_root or moved.is_relative_to(project_root):
            moved_paths.append(moved)
    if not moved_paths:
        return []
    dropped = []
    for rel in list(managed):
        target_file = (installer.target_dir / rel).resolve()
        if any(target_file == moved or target_file.is_relative_to(moved) for moved in moved_paths):
            dropped.append(rel)
    if not dropped:
        return []
    for rel in dropped:
        managed.pop(rel, None)
    journal["managed"] = managed
    installer._write_journal(journal)
    return dropped


def _skills_satisfied_by_asset_phase(
    authorized_plan: dict,
    installer: StudioInstaller,
    project_dir: Path,
    asset_writes: list[str],
) -> set[str]:
    """User-skill installs this asset phase wrote into the project `.claude`.

    An authorized user-skill install action counts, and so does a
    non-governed skill (e.g. a renderer-profile skill) in the authorized
    asset writes, which the planner now reports as preserved. The file must
    match the journal hash the installer just recorded. A separate
    installer target does not satisfy a project migration action.
    """
    try:
        overlapping = installer.target_dir.resolve() == (project_dir.resolve() / ".claude")
    except OSError:
        overlapping = False
    if not overlapping or not installer.journal_path.is_file():
        return set()
    journal = json.loads(installer.journal_path.read_text(encoding="utf-8"))
    managed = journal.get("managed")
    if not isinstance(managed, dict):
        return set()
    names = set()
    for action in authorized_plan.get("actions", {}).get("install", []):
        if not isinstance(action, dict) or action.get("kind") != "user-skill":
            continue
        name = action.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    migration_governed = set(CORE_SKILLS) | RETIRED_SKILLS | LEGACY_PROJECT_SKILLS
    for rel in asset_writes:
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" and parts[1] not in migration_governed:
            names.add(parts[1])
    satisfied = set()
    for name in sorted(names):
        rel = f"skills/{name}/SKILL.md"
        record = managed.get(rel)
        target = installer.target_dir / rel
        if not isinstance(record, dict) or not target.is_file():
            continue
        expected = record.get("installed_sha256")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if isinstance(expected, str) and actual == expected:
            satisfied.add(name)
    return satisfied


def _rebase_authorized_migration(authorized_plan: dict, satisfied_skills: set[str]) -> dict:
    """Copy the authorized plan, dropping only asset-satisfied skill installs.

    A satisfied skill moves from ``install`` to ``preserve``. Quarantine,
    replace, and every install the asset phase did not satisfy stay identical.
    """
    actions = authorized_plan["actions"]
    install = [
        action for action in actions["install"]
        if not (
            isinstance(action, dict)
            and action.get("kind") == "user-skill"
            and action.get("name") in satisfied_skills
        )
    ]
    preserve = list(actions["preserve"])
    existing = {item.get("path") for item in preserve if isinstance(item, dict)}
    for name in sorted(satisfied_skills):
        path = f".claude/skills/{name}"
        if path not in existing:
            preserve.append({"path": path, "reason": "unrelated-project-capability"})
    return {
        "version": authorized_plan["version"],
        "project": authorized_plan["project"],
        "actions": {
            "preserve": sorted(preserve, key=lambda item: item["path"]),
            "quarantine": list(actions["quarantine"]),
            "replace": list(actions["replace"]),
            "install": sorted(install, key=lambda item: (item["kind"], item["name"])),
        },
    }


def _final_managed_verification(
    installer: StudioInstaller,
    reconciled_out: set[str],
    *,
    allow_open_transaction: bool = False,
) -> dict:
    """Score the reconciled journal against the tree update() returns.

    ``StudioInstaller.verify()`` still requires every rendered source to be
    present and managed. This function does not change that. When migration
    has intentionally moved overlapping managed paths out of the tree, those
    paths are no longer journal entries, so the returned object scores only
    the remaining managed set. A rendered source that is still managed and
    missing still fails.
    """
    report = installer.verify(allow_open_transaction=allow_open_transaction)
    omitted = set(getattr(installer, "omitted_rels", set()))
    drop = set(reconciled_out) | omitted
    if not drop:
        return report
    journal = json.loads(installer.journal_path.read_text(encoding="utf-8")).get("managed") or {}
    if not isinstance(journal, dict):
        journal = {}
    assets = {
        rel: item
        for rel, item in report["assets"].items()
        if rel not in drop
    }
    rendered_ok = bool(assets) and all(
        item["source_sha256"] == item["target_sha256"] and item["managed"] is True
        for item in assets.values()
    )
    journal_ok = bool(journal) and not (set(journal) & set(reconciled_out))
    if journal_ok:
        for rel, record in journal.items():
            target = installer.target_dir / rel
            expected = record.get("installed_sha256") if isinstance(record, dict) else None
            if not target.is_file() or not isinstance(expected, str):
                journal_ok = False
                break
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                journal_ok = False
                break
    canonical = json.dumps(
        {rel: item["source_sha256"] for rel, item in sorted(assets.items())},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "schema_version": 1,
        "verified": rendered_ok and journal_ok,
        "asset_count": len(assets),
        "source_fingerprint": hashlib.sha256(canonical).hexdigest(),
        "assets": assets,
    }


def update(
    kit_dir: Path,
    target_dir: Path,
    project_dir: Path,
    *,
    authorization_path: Path,
    executed_by: str,
    state_base: Path | None = None,
    migration_fail_after: int | None = None,
    migration_crash_after: int | None = None,
    crash_after_migration_commit: bool = False,
) -> dict:
    """Safe update execution. Requires a durable authorization record
    (normally written by `authorize()`; `update()` never mints one itself --
    see the module docstring on who may call `authorize()`) whose fingerprint AND
    target identity both match a freshly recomputed preview() of the current
    state exactly -- an authorization can never be replayed against a
    materially different state (fingerprint changes with it) or against a
    different target (target_identity binds it; Critic finding D2). Reads
    and validates the record the same way `WorkOrderRun` validates its own
    persisted state (schema-checked, secret-rejecting, strict identity
    check on `approved`; Critic finding D3). Performs the asset update
    (StudioInstaller.apply(compose_settings=False); the asset transaction
    rolls back, and this call does not compose settings.json) and, only if a migration is actually required, the
    project-local migration (MigrationApplier.apply(execute=True, ...))
    gated on this same update's own fresh installer.verify() as
    `global_prerequisite_evidence` -- no shared-checkout mutation happens
    before this point. Writes durable evidence of what was authorized, who
    authorized it, who/what executed it, the exact operation, pre/post
    state, and verification result."""
    kit_dir, target_dir, project_dir = Path(kit_dir), Path(target_dir), Path(project_dir)
    authorization_path = Path(authorization_path)
    require_checkout_preflight(kit_dir)

    try:
        record = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"unreadable or malformed authorization record: {authorization_path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise UpdateError(f"unsupported authorization record schema: {authorization_path}")
    if record.get("approved") is not True:
        raise UpdateError(f"authorization record is not approved: {authorization_path}")
    _reject_secrets(record, "authorization")

    current = preview(kit_dir, target_dir, project_dir)
    if not current["compatible"]:
        raise UpdateError(
            "incompatible update: installed schema_version "
            f"{current['installed']['schema_version']!r} differs from candidate "
            f"{current['candidate']['schema_version']!r}; manual migration required"
        )
    if record.get("target_identity") != current["target_identity"]:
        raise UpdateError(
            "authorization record was minted for a different target "
            f"({record.get('target_identity')!r} != {current['target_identity']!r}); "
            "an authorization can never be used against a different target"
        )
    if record.get("project_identity") != current["project_identity"]:
        # Critic finding B2a-project: project_identity's own contribution
        # to the fingerprint was mutation-survivable (a cross-project
        # replay could still coincidentally collide, e.g. two projects
        # sharing a basename under different parents). An explicit check,
        # symmetric with target_identity above, closes this at the source
        # rather than relying solely on the fingerprint to carry it.
        raise UpdateError(
            "authorization record was minted for a different project "
            f"({record.get('project_identity')!r} != {current['project_identity']!r}); "
            "an authorization can never be used against a different project"
        )
    if record.get("fingerprint") != current["fingerprint"]:
        raise UpdateError(
            "authorization fingerprint does not match the current update preview; "
            "re-run preview()/authorize() and re-authorize against the fresh fingerprint"
        )

    pre_state = {"installed": current["installed"]}

    render_profile = current["render_profile"]
    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered"
        try:
            render_kit(kit_dir, rendered, profile=render_profile)
        except RenderError as exc:
            raise UpdateError(str(exc)) from exc
        installer = StudioInstaller(rendered, target_dir)
        _configure_overlap_omissions(installer, project_dir)
        migration_required = current["migration_required"]
        try:
            asset_plan = installer.plan()
            install_result = installer.apply(
                asset_plan,
                compose_settings=False,
                hold_transaction=migration_required,
            )
        except InstallError as exc:
            raise UpdateError(f"update failed and was rolled back: {exc}") from exc
        # The asset transaction stays open while a required migration runs.
        # Committing it first lets the install itself change MigrationPlanner
        # output and then fail freshness, leaving the assets behind.
        migration_result = None
        migration_committed = False
        try:
            # Migration's global-prerequisite gate still requires this pre-move
            # snapshot: every rendered source matches the journal before any
            # intentional quarantine. It is not the verification update() returns.
            install_verification = _final_managed_verification(
                installer, set(), allow_open_transaction=migration_required,
            )
            if migration_required:
                authorized_plan = current["migration_plan"]
                satisfied = _skills_satisfied_by_asset_phase(
                    authorized_plan, installer, project_dir, asset_plan["writes"],
                )
                expected_plan = _rebase_authorized_migration(authorized_plan, satisfied)
                if MigrationPlanner(project_dir).plan() != expected_plan:
                    raise MigrationApplyError(
                        "post-install migration plan is not a satisfaction of the authorized plan"
                    )
                applier = MigrationApplier(project_dir, state_base=state_base)
                dry = applier.apply(expected_plan)
                timestamp_ns = time.time_ns()
                # Durable before migration commits, so a crash after APPLIED
                # still has a record recover can finish.
                _atomic_json(_finalization_path(target_dir), {
                    "schema_version": 1,
                    "state": "INTENT",
                    "authorized_fingerprint": record["fingerprint"],
                    "authorized_by": record["requested_by"],
                    "authorization_record": str(authorization_path),
                    "executed_by": executed_by,
                    "timestamp_ns": timestamp_ns,
                    "evidence_rel": f"{EVIDENCE_DIR}/{timestamp_ns}.json",
                    "render_profile": render_profile,
                    "pre_state": pre_state,
                    "install_result": install_result,
                    "migration_result": None,
                    "migration_plan": expected_plan,
                    "migration_fingerprint": dry["source_fingerprint"],
                })
                migration_result = applier.apply(
                    expected_plan,
                    execute=True,
                    expected_fingerprint=dry["source_fingerprint"],
                    require_global_prerequisites=True,
                    global_prerequisite_evidence=install_verification,
                    fail_after=migration_fail_after,
                    crash_after=migration_crash_after,
                )
                migration_committed = migration_result.get("status") == "APPLIED"
                if crash_after_migration_commit:
                    raise SystemExit("injected crash after migration commit")
                pending = _load_finalization(_finalization_path(target_dir))
                pending["state"] = "PENDING"
                pending["migration_result"] = migration_result
                _atomic_json(_finalization_path(target_dir), pending)
                # Commit the asset transaction before any later bookkeeping.
                # A failure after this point must not roll assets back while
                # leaving the committed migration in place.
                try:
                    installer.transaction_path.unlink()
                except OSError as exc:
                    applier.rollback(migration_result["transaction"])
                    if installer.transaction_path.is_file():
                        installer._recover_interrupted_transaction()
                    if _finalization_path(target_dir).is_file():
                        _finalization_path(target_dir).unlink()
                    raise MigrationApplyError(
                        "installer commit failed after migration; both phases were rolled back"
                    ) from exc
        except Exception as exc:
            if migration_required and not migration_committed:
                if _finalization_path(target_dir).is_file():
                    _finalization_path(target_dir).unlink()
                if installer.transaction_path.is_file():
                    installer._recover_interrupted_transaction()
            raise UpdateError(f"required migration failed: {exc}") from exc

        if migration_committed:
            try:
                return _finish_finalization(
                    installer, project_dir, target_dir, finalized_by="update",
                )
            except Exception as exc:
                raise UpdateError(
                    "update finalization is incomplete; run recover() to finish "
                    "reconciliation and evidence"
                ) from exc

        post_update_verification = _final_managed_verification(installer, set())

    post_state = {"installed": installed_version(target_dir)}
    evidence = {
        "schema_version": 1,
        "operation": "update",
        "authorized_fingerprint": record["fingerprint"],
        "authorized_by": record["requested_by"],
        "authorization_record": str(authorization_path),
        "executed_by": executed_by,
        "timestamp_ns": time.time_ns(),
        "render_profile": render_profile,
        "pre_state": pre_state,
        "post_state": post_state,
        "install_result": install_result,
        "migration_result": migration_result,
        "post_update_verification": post_update_verification,
    }
    _atomic_json(target_dir / EVIDENCE_DIR / f"{evidence['timestamp_ns']}.json", evidence)
    return evidence


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, default=Path("_studio/kit"))
    parser.add_argument("--target", type=Path, default=Path(".claude"))
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--global-root", type=Path, default=None,
                        help="user-global Claude root inspect scores readiness against (default ~/.claude)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    sub.add_parser("preview")
    sub.add_parser("recover")
    authorize_parser = sub.add_parser("authorize")
    authorize_parser.add_argument("--requested-by", required=True)
    update_parser = sub.add_parser("update")
    update_parser.add_argument("--authorization-file", required=True, type=Path,
                                help="path to a record written by `authorize`")
    update_parser.add_argument("--executed-by", required=True)
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            global_root = args.global_root if args.global_root is not None else _default_global_root()
            result = inspect(args.kit, args.target, args.project, global_root=global_root)
        elif args.command == "preview":
            result = preview(args.kit, args.target, args.project)
        elif args.command == "recover":
            result = recover(args.kit, args.target, args.project)
        elif args.command == "authorize":
            result = authorize(args.kit, args.target, args.project, requested_by=args.requested_by)
        else:
            result = update(
                args.kit, args.target, args.project,
                authorization_path=args.authorization_file, executed_by=args.executed_by,
            )
    except UpdateError as exc:
        print(f"update error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
