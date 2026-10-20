# Studio OS Contracts

Status: Phase 2 candidate source. Not runtime-certified.

## Work Order

Every delegated unit of work has one stable `work_id` and records:

- objective
- scope
- non-goals
- acceptance criteria
- owned paths/resources
- dependencies
- permitted side effects
- required evidence
- applicable human gates
- project/profile context
- exact implementation/revision context when available

A specialist must not silently expand scope or authority.

## Specialist Result

Every specialist result returns:

- `work_id`
- role
- status
- exact revision/context inspected or changed
- files/resources changed, if any
- checks executed
- evidence locations
- unresolved risks/blockers
- requested next action

## Role-specific evidence

### Researcher

Return sources/evidence, assumptions, confidence, conflicts, and decision-relevant findings. Researcher is non-mutating by default.

### Coder

Return exact changed paths, implementation summary, deterministic checks, known limitations, and revision/context for independent review.

### Resolver

Return observed failure, reproduction, hypotheses, actions attempted, verification result, and correction-cycle count. After two failed correction cycles, stop and return to Director.

### Critic

Return exact revision/context reviewed, acceptance criteria evaluated, evidence, defects, and one verdict: `PASS`, `FAIL`, or `BLOCKED_INCONCLUSIVE`.

## Evidence freshness

Acceptance evidence is bound to the exact revision/runtime context it proves. Any material revision change makes earlier acceptance evidence stale and requires fresh verification.

## Default execution flow

`Director -> Researcher when evidence is needed -> Coder -> deterministic checks -> Critic -> Resolver only for bounded failure -> fresh checks -> fresh Critic -> Operator gate when required`

The Director may choose a smaller path when steps are not applicable, but may not remove required independent verification or human gates.

## Runtime state

Project-local run evidence belongs under:

`_studio/.local/runs/<work-id>/`

Candidate layout:

- `plan.md`
- `findings.md`
- `progress.md`
- `handoffs/`
- `evidence/`

Runtime state is project-local and must not become cross-client personal memory.
