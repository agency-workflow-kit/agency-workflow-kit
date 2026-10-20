# Studio OS Authority Policy

Status: Phase 2 candidate source. Claude Code is the current implemented
adapter/runtime target; live-turn execution is not fully runtime-certified.

## Runtime scope

This policy's executable identities and model pins describe the current
Claude Code adapter only. Provider-neutral runtime/model routing across
Cursor, Claude Code, and Codex/ChatGPT is ACTIVE ARCHITECTURE WORK in GitHub
issue #75. It does not make Cursor or Codex/ChatGPT certified runtimes and it
does not alter the gates below.

## Authority model

The system has five executable Claude identities and one human authority:

- Director — orchestrator spawned by the Front Door on the DIRECTOR path (see ADR 0004), not the session's default identity.
- Researcher — evidence and discovery specialist.
- Coder — bounded implementation specialist.
- Resolver — bounded diagnose/repair/verify specialist.
- Critic — fresh independent verification specialist.
- Operator — human authority; never generated as a Claude agent.

Only Director may delegate to Studio specialists. Specialists must not receive recursive Studio delegation authority.

## Human gates

Operator authorization is mandatory for actions outside pre-authorized bounded execution, including production deploys, DNS changes, production-data mutation, destructive or credential-sensitive operations, protected-branch merge, spend, pricing/contract/deadline commitments, external sends/publication, and governed model/skill/kit promotion where standing authorization does not cover the exact action and scope.

## Model policy

Every generated executable agent must contain an explicit reviewed model. Missing model, `inherit`, silent fallback, or unexplained effective-model override blocks release.

Model routing candidate:

- Director: `claude-opus-5`
- Researcher: `claude-sonnet-5`
- Coder: `claude-sonnet-5`
- Resolver: `claude-sonnet-5`
- Critic: `claude-opus-5`

Model changes are reviewed configuration releases. They are not self-optimization targets.

## Skills and workflows

Agent definitions own identity, model, tool/permission boundaries, delegation, runtime semantics, and isolation.

Skills/workflows own reusable procedures, methods, checklists, acceptance criteria, and output schemas. A skill must not grant itself permissions, select a model, override governance, or bypass human approval.

Domain knowledge is normally expressed as on-demand capabilities rather than additional agents.

## Verification

Critic must review the exact implementation revision/context being accepted. A revision change invalidates prior acceptance evidence. Valid verdicts are PASS, FAIL, and BLOCKED/INCONCLUSIVE. BLOCKED/INCONCLUSIVE never satisfies Definition of Done.

Critic must not fix the implementation it is reviewing.

## Learning and promotion

SkillOpt is candidate-generation infrastructure only. It is disabled until a target skill has objective scoring, representative trajectories/fixtures, a held-out evaluation, independent Critic review, and human promotion approval.

Routing, permissions, security, approval semantics, model selection, source precedence, and commercial policy are governed and are not automatic optimization targets. SkillOpt-Sleep is disabled.

## Legacy quarantine

Legacy agent identities such as Growth, Design, Client Success, Engineering Delivery, Workflow Monitor, Finance, Linear, or Operator are non-executable migration evidence. A renderer must fail rather than generate these identities.
