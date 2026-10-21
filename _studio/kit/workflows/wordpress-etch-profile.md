# WordPress + Etch Profile Contracts

This profile is internal and Etch-only. It adds exactly two Studio-owned generated wrappers. External upstream capabilities are dependencies, not generated Studio skills.

## studio-etch-author

**Trigger:** approved WordPress/Etch authoring work.

**Primary executor:** Coder. Researcher may gather references; Critic independently verifies.

**Canonical authoring authority:** official Etch Connector + Etch Public API. No second equal Etch mutation plane is permitted.

**Procedure:**
1. Confirm the correct site/project and active builder session.
2. Discover runtime Etch capabilities and relevant `etch.skills` on demand; do not hard-code stale Etch/ACSS conventions.
3. Prefer approved/reusable site components and patterns before creating new structures.
4. Perform bounded authoring through the official Connector/Public API.
5. Respect current API persistence semantics and feature-detect capabilities/methods rather than assuming API version behavior.
6. Produce deterministic evidence and hand the exact work/revision/runtime context to Critic.

**Blocked condition:** if official Connector/Public API authoring is required but unavailable, live Etch mutation is BLOCKED. No other MCP may silently become a substitute authoring authority.

**External references:** optional third-party validators/references remain disabled by default and require provenance/license review. Credentials and license keys are never stored in the kit.

## studio-wp-audit

**Trigger:** WordPress/Etch-specific audit or baseline assessment.

**Executors:** Researcher + Critic. Coder/Resolver fixes require separate work orders.

**Procedure:**
1. Identify project profile, environment and relevant WordPress/Etch runtime state.
2. Inspect configuration, performance, update/security posture, content/SEO integration and Etch-specific implementation evidence as applicable.
3. Use selective upstream WordPress infrastructure skills only when relevant; do not load a generic WordPress catalog by default.
4. Separate observations from mutations. Do not install/update/deploy/fix as part of the audit unless a separate authorized work order exists.
5. Return prioritized findings with evidence and recommended executor.

**Output:** audit evidence under the work-id evidence/artifact contract; no legacy `_studio/traces` output path.

## Release/content composition

- `studio-wp-deploy` is not generated. WordPress/Etch release extends the canonical Coder → Critic → Operator release procedure.
- `studio-wp-content` is not generated. Content work composes approved content/brand constraints with `studio-etch-author` when implementation in Etch is required.
