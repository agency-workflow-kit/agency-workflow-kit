#!/usr/bin/env python3
"""Narrow Capability Router: for one work-order node, deterministically
select and scope its allowed specialist from the existing canonical
agent manifest.

M3 of the Director-orchestration milestone: routing is entirely
data-driven from `_studio/kit/manifest.json` (the director's own
`delegates` allowlist -- never a hardcoded specialist-name set) and
from `studio_renderer.ROLE_TOOLS` (the same per-role tool grants the
real rendered agent definitions already carry). No new capability
taxonomy is introduced; a routing decision returns exactly that role's
existing minimum tool scope plus the node's own `owned_paths`/
`permitted_side_effects` from its work-order record -- never a union
of every specialist's tools, never a globally injected skill/tool/MCP
set.

Composes with WorkOrderPlan.ready_set() (M2) by work_id only; neither
WorkOrderPlan nor WorkOrderRun (M1) is imported or modified here. No
parallel execution, SCM/Git action, join gate, retry/recovery, or
dynamic/pluggable agent registry is implemented by this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from studio_renderer import ROLE_TOOLS

DEFAULT_MANIFEST_PATH = Path(__file__).parent / "_studio" / "kit" / "manifest.json"


class CapabilityRouterError(RuntimeError):
    pass


class CapabilityRouter:
    def __init__(self, *, manifest_path: Path | str = DEFAULT_MANIFEST_PATH):
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        agents = manifest.get("agents")
        if not isinstance(agents, list) or not agents:
            raise CapabilityRouterError("manifest has no agent roster")

        roles_by_name = {}
        director_delegates = None
        for entry in agents:
            name, role = entry.get("name"), entry.get("role")
            if not name or not role:
                raise CapabilityRouterError("manifest agent entry missing name/role")
            roles_by_name[name] = role
            if role == "director":
                director_delegates = entry.get("delegates")

        if not director_delegates:
            raise CapabilityRouterError("manifest has no director delegation allowlist")
        unknown = [name for name in director_delegates if name not in roles_by_name]
        if unknown:
            raise CapabilityRouterError(f"delegate allowlist references unknown agent(s): {unknown}")

        self._roles_by_name = roles_by_name
        self._allowed_executors = frozenset(director_delegates)

    def route(self, work_order: dict) -> dict:
        work_id = work_order.get("work_id")
        if not work_id:
            raise CapabilityRouterError("work order requires a work_id to route")

        executor = work_order.get("executor")
        if executor not in self._allowed_executors:
            raise CapabilityRouterError(f"disallowed or unknown executor: {executor}")

        role = self._roles_by_name[executor]
        tools = ROLE_TOOLS.get(role)
        if tools is None:
            raise CapabilityRouterError(f"no capability scope defined for role: {role}")

        return {
            "work_id": work_id,
            "executor": executor,
            "tools": tools,
            "owned_paths": tuple(work_order.get("owned_paths") or ()),
            "permitted_side_effects": tuple(work_order.get("permitted_side_effects") or ()),
        }

    def route_ready(self, work_orders_by_id: dict, ready_work_ids) -> dict:
        return {work_id: self.route(work_orders_by_id[work_id]) for work_id in ready_work_ids}
