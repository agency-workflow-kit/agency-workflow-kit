# Studio Core Skill Contract

Each generated skill is a bounded procedure. Skills never own model selection, tool permissions, delegation authority, credential policy, production authority, or source-of-truth precedence. Those remain agent/policy contracts.

## studio-intake
Trigger: new request or material re-scope.
Executor: Director.
Procedure: normalize objective, constraints, unknowns, acceptance criteria and required evidence; route research/implementation/review work; surface Operator gates.
Output: work-order-ready intake summary.

## studio-lead
Trigger: qualification/acquisition evidence needed.
Executor: Researcher → Director.
Procedure: gather verifiable fit/problem/context evidence; distinguish facts from inference; Director synthesizes.
Output: qualification evidence and recommendation. Pricing, outreach and commitments require Operator authority.

## studio-discovery-call
Trigger: discovery preparation, capture or synthesis.
Executor: Researcher → Director.
Procedure: prepare questions from known evidence; capture requirements/constraints/decisions; identify unresolved assumptions; synthesize into actionable discovery record.
Output: discovery record suitable for work-order/proposal creation.

## studio-proposal
Trigger: proposal or scope draft requested.
Executor: Director + Researcher.
Procedure: ground scope in discovery/evidence; state deliverables, assumptions, exclusions, acceptance and risks; separate draft commercial fields from authorized commitments.
Output: proposal/scope draft. Price, contract, deadline and external send require Operator.

## studio-brand-voice
Trigger: content must follow an approved brand voice.
Executor: Researcher/Director.
Procedure: extract current approved voice constraints/examples; produce reusable content constraints; flag missing authority rather than inventing brand rules.
Output: bounded brand/content brief.

## studio-design-critique
Trigger: independent design review is required.
Executor: Critic; Researcher may supply references.
Procedure: evaluate against stated goals, usability/accessibility constraints and approved design decisions; separate objective defects from subjective preferences.
Output: PASS/FAIL/BLOCKED-INCONCLUSIVE findings tied to evidence where acceptance is being gated.

## studio-email
Trigger: client/internal email drafting or preparation.
Executor: Director.
Procedure: draft from approved facts and constraints; identify commitments requiring approval; never grant sending authority.
Output: send-ready draft plus pending approval notes where applicable.

## studio-linear-sync
Status: DEPRECATED / HISTORICAL; not generated.
Trigger: authorized read-only inspection of historical Linear data during migration/reconciliation.
Executor: Director.
Precondition: verified project binding.
Procedure: compare current authoritative Notion/GitHub state with the retained historical Linear representation; never write or delete Linear data and never infer a binding.
Output: reconciliation record and unresolved conflicts.

## studio-retainer-report
Trigger: retainer/client report for a requested reporting period.
Executor: Director + Researcher.
Procedure: gather evidence for the requested reporting period, summarize completed/in-progress/blocked work and next priorities; never imply scheduler behavior.
Output: evidence-backed report draft.
Alias policy: `studio-weekly-retainer` is retired and MUST NOT be generated.

## studio-maintenance-audit
Trigger: on-demand operational/technical maintenance audit.
Executor: Researcher + Critic.
Procedure: inspect state, collect evidence, evaluate against criteria; do not silently mutate. Fixes become separately authorized Coder/Resolver work orders.
Output: findings, severity, evidence and recommended next actions.
Alias policy: `studio-weekly-audit` is retired and MUST NOT be generated.

## studio-grill
Trigger: planning on-ramp state `MATERIAL_AMBIGUITY` (`studio_planning_onramp.classify_planning_onramp`) — the incoming task is ambiguous or greenfield and lacks an explicit acceptance criterion, scope boundary, or a single clear work order — not a bounded, already-specified (`CLEAR`) task.
Executor: Director.
Procedure: before authoring any decomposition, if and only if running from a checkout of the Workflow Kit repository itself -- `studio_capability_providers.py` is never installed into any activated project's `.claude/`, so this step can never resolve and must never be attempted from one -- run `python3 studio_capability_providers.py --capability requirements_interrogation --availability-json <whatever is currently knowable>` via Bash and read `selected_provider_id`/`execution_mode` from its JSON output -- this is the authoritative decision, never re-derive it yourself. From an activated external project, skip straight to the fallback below; this is not a defect, only the expected shape when the CLI is unreachable. Until live provider detection is wired (a later, not-yet-authorized slice), no availability signal will typically be available even inside the Workflow Kit repository itself, so the CLI's own safe default-to-UNKNOWN correctly and expectedly falls through to the native path today -- this is expected, not a defect. If `execution_mode == "automated"` (grill-me selected/PRESENT), use/delegate to Matt Pocock's `grill-me` skill for the interrogation. Otherwise (fallback), the Director performs the same goal directly: surface unstated assumptions and force explicit answers on scope, non-goals and acceptance criteria; stop once the objective is unambiguous rather than open-ended -- this is the same provider-agnostic goal either way, never a duplicated methodology, and must never claim Matt's methodology was used when it wasn't. Never substitute for Operator authority on any commitment this uncovers. After that outcome, if the remaining decision surface still cannot be stated precisely and/or plausibly exceeds one session's context budget -- Wayfinder's own escalation test, never file count, feature count, prompt length, or a complexity score -- consult `python3 studio_capability_providers.py --capability decision_wayfinding` the same way; if the result's `recommended_provider_id` is set, surface `WAYFINDER_RECOMMENDED` with its `reason` and treat it exactly as studio-gate's `OPERATOR_APPROVAL_REQUIRED` output. The Director must never auto-invoke Wayfinder itself under any circumstance, `disable-model-invocation` on the real skill must never be bypassed, and only explicit Operator authorization may proceed to invoke it (entirely outside this contract's scope).
Output: a discovery record populated directly into the resulting work order's own `objective`, `scope`, `non_goals` and `acceptance_criteria` fields -- no new schema, no new file format.

### Planning on-ramp procedures (not generated skills)

`studio-grill-with-docs` and `studio-wayfinder` are planning on-ramp
procedure labels from `studio_planning_onramp.py` (`GRILL_WITH_DOCS`,
`WAYFINDER`). They are **not** entries in
`catalog.core_generated_skills`. After either completes, return to
`studio_front_door.route_entry`.

- **studio-grill-with-docs** — trigger: `REPO + DURABLE_DECISIONS + ONE_SESSION`. Resolve via `python3 studio_capability_providers.py --capability durable_decision_documentation`. PRESENT selects `matt-pocock-grill-with-docs`; ABSENT/UNKNOWN uses the capability fallback. Durable decisions stay in the owning project under the Notion/GitHub truth model; no cross-project writes; no Master Plan.
- **studio-wayfinder** — trigger: `HUGE / FOGGY / MULTI_SESSION` selected automatically by `classify_planning_onramp` (planning-state classifier). Resolve provider via `--capability decision_wayfinding` when that CLI is reachable. PRESENT stays `WAYFINDER_RECOMMENDED` / `OPERATOR_APPROVAL_REQUIRED` (never auto-selected as an automated capability provider; Operator gates unchanged). Executable `next_action` is the Workflow Kit bounded wrapper. Produce a decision map (`decisions` + `dependencies` + `next_route`); use the owning project's verified work/dependency tracker and leave the destination unresolved until that binding is known; `executes_work=false`.


## studio-gate
Trigger: action may cross a governed boundary.
Executor: Director checks policy; Operator supplies authority where required.
Procedure: classify requested action against human gates and standing authorization; block ambiguous authority.
Output: ALLOWED, OPERATOR_APPROVAL_REQUIRED or BLOCKED with reason.
Governance: never SkillOpt-trainable.

## studio-verify
Trigger: acceptance/release/runtime verification.
Executor: fresh Critic.
Procedure: verify exact revision/context against explicit acceptance criteria; use deterministic checks first and runtime evidence when behavior is user-facing/runtime-affecting; Reticle when applicable; invalidate evidence after revision changes. For a state-dependent measurement (a value that depends on an interactive, animated or asynchronous state such as focus, hover, open, loading or a transition), first prove the intended target state is active and settled with a deterministic signal appropriate to the surface (for example animation or transition completion, an explicit readiness state, observed task completion, an application-defined settled signal, or the same value across repeated observations), and record that settle proof with the measurement; a fixed timeout or sleep alone is not settle proof. If settle proof is missing, transitional or indeterminate, that criterion is BLOCKED_INCONCLUSIVE, never PASS or FAIL from that measurement.
Output: critic verdict contract with PASS, FAIL or BLOCKED_INCONCLUSIVE.
Governance: first SkillOpt candidate only after objective reward, trajectories, held-out fixtures, false-PASS benchmark and human promotion path exist.

## studio-run-engine
Trigger: the work requires a formal, Critic-approved and Operator-authorized release verdict before it can be considered done -- concretely, you are marking at least one node's `operator_gate_required: true` in a multi-node work-order decomposition (the same decomposition structure regardless of how many nodes it actually has). A real production pilot proved that parallel branches needing to converge/join, and ordinary sequential chains, are already handled correctly by native Agent(...) dispatch alone -- so neither dependency shape nor task-step count ever justifies this trigger by itself; only an explicit governance requirement does. A single-step task, independent fan-out, a sequential chain, or any fan-out-with-join that carries no such requirement never reaches this trigger at all -- decomposition is not required merely because a task has more than one step, an explicit ordering, or a convergence point.
Executor: the plain Front Door session runs the entry classification below; only a `"director"` delegation then runs the installed Factory governed driver (ADR 0009), unchanged, via Bash.
Precondition: none beyond the decomposition itself to run the classification below. Only a `"director"` delegation requires its own linked worktree and real governance evidence (a fresh Critic session's verdict, and Operator authorization when the Operator has actually given it) -- never invented here.
Procedure: per ADR 0004, entry classification runs at the Front Door, before any Director exists -- write the decomposition to a file, then run `python3 "$CLAUDE_PROJECT_DIR/.claude/runtime/studio_front_door.py" --decomposition <file>` via Bash (the installed path, exactly as `studio_settings_merge.GATE_COMMAND` already resolves the worktree gate hook's own script via `$CLAUDE_PROJECT_DIR` -- `studio_front_door.py` is part of the Front Door runtime closure the installer ships to every activated project's `.claude/runtime/`) and read `path` and `delegations` from its JSON output -- this is the authoritative decision, never re-derive it from the decomposition's shape yourself. `path: "direct"` (empty decomposition) means do the work in the current session with no delegation at all. `path: "delegate"` (no node declares `operator_gate_required: true`) means dispatch each returned delegation directly via native `Agent(...)`, honoring that delegation's own `isolation` (a write-class specialist -- agency-coder, agency-resolver -- carries `isolation: "worktree"` and must be spawned `Agent(..., isolation: "worktree")`: it has no EnterWorktree tool and cannot self-isolate, and studio_worktree_gate.py now blocks its Bash at runtime if it is not isolated) and its `dependencies` (never start a delegation before every work_id in its own `dependencies` list has completed). `path: "director"` (at least one node declares `operator_gate_required: true`) means spawn one `Agent(agency-director, ...)` per returned delegation -- concurrently for independent delegations, since each already covers its own disjoint, weakly-connected component -- always `isolation: "worktree"`. Each spawned Director (ADR 0009) does the work through native `Agent(...)` delegation to the approved specialists (write-class ones with `isolation: "worktree"`), commissions a fresh `agency-critic` against the exact result, then writes one JSON payload and runs `python3 "$CLAUDE_PROJECT_DIR/.claude/runtime/studio_front_door.py" --execute-json <file>` via Bash -- the installed Factory driver; no Workflow Kit checkout, GitHub repository or extra module is needed. The payload carries: `task_id`; `cwd`; the gated decomposition as `proposed_decomposition`; `worktrees` mapping each work_id to the worktree holding the result (`path`, `branch`, `head`, `index_path`); deterministic `CODE` `execute_phases`/`verify_phases` that run there (the real test command, `git_diff`); and `done_supplemental` with `residual_uncertainty`, an `authorization_action`, `critic_evidence` bound to that `task_id`, the `work_order_id`, the Critic's verdict, its real session/source and `verified_revision` (the full git HEAD SHA of that work_id's worktree the Critic reviewed; DONE fails closed when it is missing or differs from that worktree's HEAD at DONE), and `operator_authorization` (`authorized`, `authorized_by`, `task_id`, `action`) only when the Operator actually authorized that exact prepared action. Read `state` from the JSON: `"GATED"` with `prepared_gate_action` means stop and report that exact action for Operator authorization; `"BLOCKED"`/an `error` means report the blocker; `"DONE"` carries the provenance-bearing `evidence`. Non-zero exit means a CLI/input-contract failure -- read stderr, never infer a result from it. Never reimplement CapabilityRouter, admission, join, governance, or execution-path-selection logic here; never fabricate Critic or Operator evidence.
Output: for `"direct"`, the in-session result; for `"delegate"`, the direct Agent(...) results; for `"director"`, each spawned Director's Factory result (`state`, `prepared_gate_action` or `evidence`) or a clear CLI failure report.
