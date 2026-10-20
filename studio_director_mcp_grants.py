#!/usr/bin/env python3
"""Declarative, hand-curated catalog of exact MCP tool-name grants Director
may be given at render time.

The authorization unit is the exact runtime MCP tool name (`tool_name`),
never an integration/provider ID. If authorization were recorded at the
integration level (e.g. "linear is authorized"), then later adding a new
curated tool for that same integration would silently expand every
already-authorized caller's grants with zero new decision -- a real
privilege-escalation risk. Recording authorization as an explicit list of
exact tool names avoids this structurally: a caller only ever gets exactly
what it explicitly named.

This is knowledge and a pure resolver only: no filesystem access, no
subprocess, no network, and no coupling to any other Workflow Kit module
(stdlib only). Mirrors the declarative pattern already used by
studio_integrations.py's IntegrationDefinition/DEFAULT_INTEGRATIONS and
studio_capability_providers.py's ProviderDefinition/DEFAULT_PROVIDERS.

Deliberately NOT wired into live activation/detection: `authorized_tool_names`
below is always the caller's explicit, already-decided, exact-tool-level
authorization list -- never an integration_id, never a wildcard, never
derived automatically from detection/connection state. That composition is
a later, separate slice's job.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DirectorMcpGrant:
    """Static identity for one curated, code-reviewed MCP tool grant.

    integration_id: descriptive/grouping metadata only (reuses the
    vocabulary already established by studio_integrations.py's
    integration_id values, e.g. "linear") -- NEVER itself the authorization
    unit, purely for future detection-intersection/curation use, out of
    scope here.

    tool_name: exact runtime MCP tool name, e.g.
    "mcp__claude_ai_Linear__get_issue" -- this IS the authorization unit.

    read_only: explicit, not assumed by omission -- this slice only ever
    curates read_only=True entries; the field exists so a future slice
    can't silently misclassify by leaving it implicit.
    """

    integration_id: str
    tool_name: str
    display_name: str
    read_only: bool = True


# Static, hand-curated, code-reviewed catalog. Seeded with exactly the one
# entry already proven in dogfood evidence (Probe B + a real managed-project
# experiment): a Director-shaped custom agent with a closed tools: allowlist
# plus this one exact, literal MCP tool name works end-to-end.
CURATED_DIRECTOR_MCP_GRANTS: tuple[DirectorMcpGrant, ...] = (
    DirectorMcpGrant(
        integration_id="linear",
        tool_name="mcp__claude_ai_Linear__get_issue",
        display_name="Linear: get issue (read-only)",
    ),
)


def _check_no_duplicate_tool_names(catalog: tuple[DirectorMcpGrant, ...]) -> None:
    tool_names = [item.tool_name for item in catalog]
    if len(tool_names) != len(set(tool_names)):
        raise ValueError("duplicate tool_name in CURATED_DIRECTOR_MCP_GRANTS")


_check_no_duplicate_tool_names(CURATED_DIRECTOR_MCP_GRANTS)


class DirectorMcpGrantError(RuntimeError):
    pass


def known_grant_tool_names(
    catalog: tuple[DirectorMcpGrant, ...] = CURATED_DIRECTOR_MCP_GRANTS,
) -> frozenset[str]:
    """Every curated, read-only tool_name in the catalog."""
    return frozenset(item.tool_name for item in catalog if item.read_only)


def resolve_director_mcp_grants(
    authorized_tool_names: list[str],
    *,
    catalog: tuple[DirectorMcpGrant, ...] = CURATED_DIRECTOR_MCP_GRANTS,
) -> list[str]:
    """Pure. `authorized_tool_names` is the caller's EXPLICIT, already-decided,
    exact-tool-level authorization list -- never an integration_id, never a
    wildcard, never derived automatically from detection/connection state
    (that composition is a later, separate slice's job).

    For each name: reject (raise DirectorMcpGrantError, naming the
    offending tool_name) if it is not present in the curated catalog at
    all, or if the matching catalog entry has read_only=False (defensive --
    the current catalog has no such entry, but this must not silently pass
    one through if it ever did).

    On success, return the validated names deduplicated and deterministically
    sorted (so downstream rendering is deterministic regardless of input
    order/duplicates). An empty input list returns an empty list, not an
    error.
    """
    by_tool_name = {item.tool_name: item for item in catalog}

    validated: set[str] = set()
    for tool_name in authorized_tool_names:
        entry = by_tool_name.get(tool_name)
        if entry is None:
            raise DirectorMcpGrantError(
                f"tool_name {tool_name!r} is not present in the curated Director MCP grant catalog"
            )
        if not entry.read_only:
            raise DirectorMcpGrantError(
                f"tool_name {tool_name!r} is not read-only and cannot be granted"
            )
        validated.add(tool_name)

    return sorted(validated)
