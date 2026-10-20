#!/usr/bin/env python3
"""Generic, integration-agnostic local capability detection primitives.

Detection here is deliberately mechanism-based, not integration-specific:
each function answers one generic question that any local capability -- an
MCP server entry, an installed Claude Code plugin, a future non-MCP
integration -- can be checked against by supplying its own path/key, never by
teaching this module about any specific integration's identity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path


class LifecycleState(Enum):
    """One capability lifecycle stage's evaluated state.

    PRESENT / ABSENT require the stage to have been actively evaluated with
    confident evidence either way. NOT_APPLICABLE means the stage has no
    meaning for this capability (e.g. "authenticated" for a local, no-auth
    capability) -- never fabricated as True or False. UNKNOWN is the default:
    not yet evaluated, or evaluation was genuinely inconclusive.
    """

    PRESENT = auto()
    ABSENT = auto()
    NOT_APPLICABLE = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class CapabilityObservation:
    """Independently observed lifecycle state for one capability.

    Generic across capability kinds -- a remote, authenticated integration
    and a local, no-auth capability are both represented by the same six
    fields; only their LifecycleState values differ.
    """

    integration_id: str
    available: LifecycleState = LifecycleState.UNKNOWN
    installed: LifecycleState = LifecycleState.UNKNOWN
    enabled: LifecycleState = LifecycleState.UNKNOWN
    configured: LifecycleState = LifecycleState.UNKNOWN
    authenticated: LifecycleState = LifecycleState.UNKNOWN
    verified: LifecycleState = LifecycleState.UNKNOWN


def detect_json_key_presence(manifest_path: Path, key: str) -> bool:
    """True only if manifest_path exists, contains a JSON object, and that
    object has `key` as a top-level key. Absence, unreadability, malformed
    JSON, or a non-object top-level value are evidence of no detection, not
    an error -- all resolve to False, never a fabricated positive result.
    """
    try:
        raw = Path(manifest_path).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and key in data


def detect_json_key_state(manifest_path: Path, key: str) -> LifecycleState:
    """LifecycleState for whether `key` is a top-level key in the JSON object
    at manifest_path, preserving the distinction between confident absence
    and inconclusive evidence:

    - manifest missing entirely, or a successfully-read object without the
      key -> ABSENT (confident negative evidence).
    - manifest present but unreadable, malformed, or not a JSON object ->
      UNKNOWN (the evaluation could not actually complete; never fabricated
      as ABSENT).
    - key present in a successfully-read object -> PRESENT.

    Never returns NOT_APPLICABLE -- that is an assembly/domain decision,
    not something this low-level detector can infer.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return LifecycleState.ABSENT
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return LifecycleState.UNKNOWN
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LifecycleState.UNKNOWN
    if not isinstance(data, dict):
        return LifecycleState.UNKNOWN
    return LifecycleState.PRESENT if key in data else LifecycleState.ABSENT


def detect_nested_json_key_prefix_state(manifest_path: Path, outer_key: str, prefix: str) -> LifecycleState:
    """LifecycleState for whether any key of the nested object at
    `data[outer_key]` starts with `prefix`, mirroring detect_json_key_state's
    exact semantics one level deeper:

    - manifest missing entirely, or a successfully-read top-level object
      without `outer_key` -> ABSENT (confident negative evidence).
    - manifest present but unreadable, malformed, not a JSON object at the
      top level, or `data[outer_key]` is not itself a dict -> UNKNOWN (the
      evaluation could not actually complete; never fabricated as ABSENT).
    - `data[outer_key]` is a dict and at least one of its keys starts with
      `prefix` -> PRESENT.
    - `data[outer_key]` is a dict but no key starts with `prefix` -> ABSENT.

    Never returns NOT_APPLICABLE -- that is an assembly/domain decision,
    not something this low-level detector can infer. Deliberately generic:
    parameterized entirely by outer_key/prefix, no hardcoded knowledge of
    "plugins" or any specific capability identity.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return LifecycleState.ABSENT
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return LifecycleState.UNKNOWN
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LifecycleState.UNKNOWN
    if not isinstance(data, dict):
        return LifecycleState.UNKNOWN
    if outer_key not in data:
        return LifecycleState.ABSENT
    inner = data[outer_key]
    if not isinstance(inner, dict):
        return LifecycleState.UNKNOWN
    return LifecycleState.PRESENT if any(key.startswith(prefix) for key in inner) else LifecycleState.ABSENT


def assemble_installed_observation(
    integration_id: str,
    manifest_path: Path,
    key: str,
) -> CapabilityObservation:
    return CapabilityObservation(
        integration_id=integration_id,
        installed=detect_json_key_state(manifest_path, key),
    )


def verify_manifest_path_reference(
    manifest_path: Path,
    key: str,
    path_field: str,
    expected_relative_path: str,
) -> LifecycleState:
    """LifecycleState for whether a manifest-declared install path actually
    contains expected_relative_path -- independent evidence beyond mere
    manifest presence, never equating "declared" with "verified".

    - manifest missing, unreadable, malformed, or not a JSON object ->
      UNKNOWN (the evaluation could not actually complete).
    - key absent from a well-formed object, or its value isn't a usable
      path-bearing shape (not a dict, or a list with no dict entries, or
      missing/blank path_field), or the declared path doesn't exist as a
      directory, or doesn't contain expected_relative_path -> ABSENT
      (confidently evaluated: something was claimed, reality doesn't back
      it up).
    - the declared path exists and genuinely contains expected_relative_path,
      with the resolved target verified to stay under the declared root
      (never following a traversal or symlink escape to a fabricated
      PRESENT) -> PRESENT.

    Never returns NOT_APPLICABLE -- an assembly/domain decision, not
    something this low-level detector can infer.
    """
    manifest_path = Path(manifest_path)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return LifecycleState.UNKNOWN
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LifecycleState.UNKNOWN
    if not isinstance(data, dict):
        return LifecycleState.UNKNOWN
    if key not in data:
        return LifecycleState.ABSENT

    value = data[key]
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return LifecycleState.ABSENT

    declared = value.get(path_field)
    if not isinstance(declared, str) or not declared:
        return LifecycleState.ABSENT

    base_path = Path(declared)
    if not base_path.is_dir():
        return LifecycleState.ABSENT
    base_resolved = base_path.resolve()

    candidate = base_path / expected_relative_path
    try:
        candidate_resolved = candidate.resolve(strict=True)
    except OSError:
        return LifecycleState.ABSENT

    if not candidate_resolved.is_relative_to(base_resolved):
        return LifecycleState.ABSENT

    return LifecycleState.PRESENT


def declare_stage_not_applicable(observation: CapabilityObservation, stage: str) -> CapabilityObservation:
    """Domain/assembly decision that one lifecycle stage has no meaning for this
    capability (e.g. "authenticated" for a local, no-auth capability). Only ever
    overrides UNKNOWN; refuses to overwrite a stage already carrying real
    PRESENT/ABSENT evidence, since that would discard genuine detection results.
    Performs no detection itself -- not a new detector, purely a data transform.
    """
    current = getattr(observation, stage)
    if current is not LifecycleState.UNKNOWN:
        raise ValueError(f"cannot declare already-evaluated stage not applicable: {stage}")
    return replace(observation, **{stage: LifecycleState.NOT_APPLICABLE})


def assemble_verified_observation(
    integration_id: str,
    manifest_path: Path,
    key: str,
    path_field: str,
    expected_relative_path: str,
) -> CapabilityObservation:
    return CapabilityObservation(
        integration_id=integration_id,
        verified=verify_manifest_path_reference(manifest_path, key, path_field, expected_relative_path),
    )
