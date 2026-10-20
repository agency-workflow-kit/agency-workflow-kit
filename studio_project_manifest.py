#!/usr/bin/env python3
"""Canonical project manifest: ``<project>/_studio/project.json``.

Parent spec §6/§8/§17: one user-owned file holding project identity, profile
and external-resource references, with a schema and validator. Activation
generates it once (fresh identity, unknown fields null) and never overwrites
it; uninstall leaves it in place; release packaging never ships a live copy.

v1.2 field set (Operator decision on #114, reconciling the stale §8 example):
identity, mode, stack descriptors, runtime, structured commands,
environments and authority. Deliberately NOT here, because another surface
already owns it -- this is not a second settings system:

- renderer profile: ``.studio/project-profile-extension.json``
  (studio_activation.resolve_render_profile);
- execution limits: ``_studio/kit/policy.md`` and the kit manifest;
- readiness: studio_readiness / check_activation_state;
- integrations/features: studio_capability_providers;
- Linear bindings: deprecated, GitHub is the live work system.

Schema document: ``_studio/kit/schemas/project.schema.json``. This repository
has no runtime JSON Schema validator (ADR 0006), so validation is the
hand-written ``validate_project_manifest`` below; a test keeps the two in step.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from studio_installer import _atomic_write

SCHEMA_VERSION = 1
PROJECT_MANIFEST_RELATIVE = Path("_studio") / "project.json"
TOP_LEVEL_FIELDS = (
    "schemaVersion", "kitVersion", "projectId", "projectName", "mode",
    "profile", "runtime", "commands", "environments", "authority",
)
MODES = ("new", "adopt")
PROFILE_FIELDS = ("framework", "packageManager", "hosting", "database", "stage")
RUNTIME_FIELDS = ("primary", "testedVersion")
RUNTIME_PRIMARY = "claude-code"
COMMAND_NAMES = ("install", "lint", "typecheck", "test", "build", "e2e")
COMMAND_FIELDS = ("executable", "args")
ENVIRONMENT_FIELDS = ("localUrl", "previewUrl", "productionUrl")
AUTHORITY_FIELDS = ("production", "externalSend", "commercialCommitment", "destructiveAction")
AUTHORITY_HOLDER = "operator"

_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
# Executables are argv[0], never shell text: no whitespace or shell syntax.
_SHELL_SYNTAX = re.compile(r"[\s;&|$`<>()*?!~{}\[\]'\"\\#]")
# Top-level entries that do not make a directory an existing project.
_NON_PROJECT_ENTRIES = frozenset({".git", ".claude", "_studio", ".studio", ".DS_Store"})


class ProjectManifestError(RuntimeError):
    pass


def _exact_keys(value, fields: tuple[str, ...], where: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object")
        return False
    for name in fields:
        if name not in value:
            errors.append(f"{where}.{name} is required")
    for name in sorted(set(value) - set(fields)):
        errors.append(f"{where}.{name} is not a v1.2 project manifest field")
    return True


def _nullable_text(value, where: str, errors: list[str]) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        errors.append(f"{where} must be null or a non-empty string")


def _check_command(value, where: str, project_root: Path | None, errors: list[str]) -> None:
    if value is None:
        return
    if not _exact_keys(value, COMMAND_FIELDS, where, errors):
        return
    executable = value.get("executable")
    args = value.get("args")
    if not isinstance(executable, str) or not executable:
        errors.append(f"{where}.executable must be a non-empty string")
    elif _SHELL_SYNTAX.search(executable) or "\0" in executable:
        errors.append(f"{where}.executable must be a bare executable, not shell text")
    elif executable.startswith("/") or ".." in Path(executable).parts:
        errors.append(f"{where}.executable must not be absolute or escape the repository")
    elif "/" in executable and project_root is not None:
        candidate = Path(project_root) / executable
        if not candidate.is_file():
            errors.append(f"{where}.executable {executable!r} does not exist in the repository")
        else:
            try:
                candidate.resolve().relative_to(Path(project_root).resolve())
            except ValueError:
                errors.append(f"{where}.executable {executable!r} resolves outside the repository")
    if not isinstance(args, list) or not all(isinstance(a, str) and "\0" not in a for a in args):
        errors.append(f"{where}.args must be a list of strings")


def _check_url(value, where: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        errors.append(f"{where} must be null or an http(s) URL")
        return
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        errors.append(f"{where} must be null or an http(s) URL")
    elif parts.username is not None or parts.password is not None:
        errors.append(f"{where} must not embed credentials (store references, not secrets)")


def validate_project_manifest(data, *, project_root: Path | None = None, template: bool = False) -> list[str]:
    """Return every contract violation; an empty list means valid.

    ``template=True`` validates the distributable ``project.example.json``:
    identity must be unresolved (null) because a template never carries a
    live project's identity.
    """
    errors: list[str] = []
    if not _exact_keys(data, TOP_LEVEL_FIELDS, "project", errors):
        return errors

    version = data.get("schemaVersion")
    if type(version) is not int or version != SCHEMA_VERSION:
        errors.append(f"project.schemaVersion must be {SCHEMA_VERSION}")
    kit_version = data.get("kitVersion")
    if not isinstance(kit_version, str) or not kit_version.strip():
        errors.append("project.kitVersion must be a non-empty string")

    project_id = data.get("projectId")
    if template:
        if project_id is not None or data.get("projectName") is not None:
            errors.append("template projectId and projectName must be null")
    elif not isinstance(project_id, str) or not _UUID4.match(project_id):
        errors.append("project.projectId must be a generated UUID4 (identity is never unresolved after setup)")
    _nullable_text(data.get("projectName"), "project.projectName", errors)

    if data.get("mode") not in MODES:
        errors.append(f"project.mode must be one of {list(MODES)}")

    profile = data.get("profile")
    if _exact_keys(profile, PROFILE_FIELDS, "project.profile", errors):
        for name in PROFILE_FIELDS:
            _nullable_text(profile.get(name), f"project.profile.{name}", errors)

    runtime = data.get("runtime")
    if _exact_keys(runtime, RUNTIME_FIELDS, "project.runtime", errors):
        if runtime.get("primary") != RUNTIME_PRIMARY:
            errors.append(f"project.runtime.primary must be {RUNTIME_PRIMARY!r}")
        _nullable_text(runtime.get("testedVersion"), "project.runtime.testedVersion", errors)

    commands = data.get("commands")
    if _exact_keys(commands, COMMAND_NAMES, "project.commands", errors):
        for name in COMMAND_NAMES:
            _check_command(commands.get(name), f"project.commands.{name}", project_root, errors)

    environments = data.get("environments")
    if _exact_keys(environments, ENVIRONMENT_FIELDS, "project.environments", errors):
        for name in ENVIRONMENT_FIELDS:
            _check_url(environments.get(name), f"project.environments.{name}", errors)

    authority = data.get("authority")
    if _exact_keys(authority, AUTHORITY_FIELDS, "project.authority", errors):
        for name in AUTHORITY_FIELDS:
            if authority.get(name) != AUTHORITY_HOLDER:
                errors.append(f"project.authority.{name} must be {AUTHORITY_HOLDER!r} (profiles cannot weaken central authority)")
    return errors


def new_project_manifest(*, kit_version: str, mode: str, project_id: str | None = None) -> dict:
    """Fresh identity; every unknown field null (SPEC §7: leave unknown app fields unset)."""
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "kitVersion": kit_version,
        "projectId": project_id or str(uuid.uuid4()),
        "projectName": None,
        "mode": mode,
        "profile": {name: None for name in PROFILE_FIELDS},
        "runtime": {"primary": RUNTIME_PRIMARY, "testedVersion": None},
        "commands": {name: None for name in COMMAND_NAMES},
        "environments": {name: None for name in ENVIRONMENT_FIELDS},
        "authority": {name: AUTHORITY_HOLDER for name in AUTHORITY_FIELDS},
    }
    errors = validate_project_manifest(manifest)
    if errors:
        raise ProjectManifestError("; ".join(errors))
    return manifest


def serialize_project_manifest(data: dict, *, template: bool = False) -> bytes:
    """Deterministic bytes; refuses to serialize an invalid manifest."""
    errors = validate_project_manifest(data, template=template)
    if errors:
        raise ProjectManifestError("; ".join(errors))
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def detect_mode(project_root: Path) -> str:
    """``new`` for an empty directory, ``adopt`` once it holds project content."""
    root = Path(project_root)
    if not root.is_dir():
        return "new"
    return "adopt" if any(entry.name not in _NON_PROJECT_ENTRIES for entry in root.iterdir()) else "new"


def _manifest_path(project_root: Path) -> Path:
    root = Path(project_root)
    path = root / PROJECT_MANIFEST_RELATIVE
    studio_dir = path.parent
    if studio_dir.is_symlink() or path.is_symlink():
        raise ProjectManifestError(f"{PROJECT_MANIFEST_RELATIVE} must not be a symlink or under a symlinked _studio/")
    if studio_dir.exists() and not studio_dir.is_dir():
        raise ProjectManifestError("_studio exists but is not a directory")
    if path.exists() and not path.is_file():
        raise ProjectManifestError(f"{PROJECT_MANIFEST_RELATIVE} exists but is not a regular file")
    return path


def read_project_manifest(project_root: Path, *, kit_version: str | None = None) -> dict:
    """Read-only: MISSING, VALID or INVALID, never raises on bad content."""
    rel = PROJECT_MANIFEST_RELATIVE.as_posix()
    try:
        path = _manifest_path(project_root)
    except ProjectManifestError as exc:
        return {"state": "INVALID", "path": rel, "errors": [str(exc)], "manifest": None}
    if not path.exists():
        return {"state": "MISSING", "path": rel, "errors": [], "manifest": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"state": "INVALID", "path": rel, "errors": [f"malformed JSON: {exc}"], "manifest": None}
    errors = validate_project_manifest(data, project_root=Path(project_root))
    result = {
        "state": "INVALID" if errors else "VALID",
        "path": rel,
        "errors": errors,
        "manifest": None if errors else data,
    }
    if not errors and kit_version is not None:
        # Reported, never rewritten: the file is user-owned after setup.
        result["kit_version_current"] = data["kitVersion"] == kit_version
    return result


def ensure_project_manifest(project_root: Path, *, kit_version: str, mode: str) -> dict:
    """Create the manifest once; never overwrite. Invalid content raises."""
    current = read_project_manifest(project_root, kit_version=kit_version)
    if current["state"] == "INVALID":
        raise ProjectManifestError(
            f"{current['path']} is invalid: " + "; ".join(current["errors"])
        )
    if current["state"] == "VALID":
        return {**current, "created": False}
    manifest = new_project_manifest(kit_version=kit_version, mode=mode)
    _atomic_write(_manifest_path(project_root), serialize_project_manifest(manifest))
    return {
        "state": "VALID",
        "path": current["path"],
        "errors": [],
        "manifest": manifest,
        "kit_version_current": True,
        "created": True,
    }
