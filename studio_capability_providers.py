#!/usr/bin/env python3
"""Declarative capability/provider policy: knowledge + deterministic selection.

Answers one narrow question -- "for this capability need, which known
external provider should be used, if available?" -- as pure data plus a
pure selection function. This is knowledge only: no filesystem scan, no
subprocess call, no network call, and no coupling to real plugin/skill
detection happens anywhere in this module. Availability of any given
provider is always an input the resolver consumes (an injected
`LifecycleState` per provider_id), never something this module discovers
on its own; real detection wiring is a separate, later concern.

Mirrors studio_integrations.py's declarative style (frozen dataclass +
duplicate-rejecting registry + default_*_registry() factory) but solves a
different problem than IntegrationStateMachine/MCPInventoryAdapter there:
this module is about "is a known external tool/skill available, and if
not, what do we honestly fall back to" -- not connection-lifecycle state
for Studio's own configured integrations. It also has no concept of
project enablement (which profile/workflow requires what -- that's
studio_integrations.IntegrationRegistry's job) and no concept of runtime
evidence/certification (that's studio_certify.py/studio_probe.py/
studio_readiness.py's job); this module never imports any of those and
never touches disk or a subprocess.

Deliberately NOT a marketplace, NOT an installer, NOT a new orchestration
engine, and NOT wired into studio_capability_router.py (that module routes
internal agent executors against the manifest's role roster -- a
completely different, internal concern with no notion of external
providers at all).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from enum import Enum, auto

from studio_capability_detection import LifecycleState


class ProviderClassification(Enum):
    """How a registered provider relates to the default execution path.

    OPTIONAL_PROVIDER: a real optional alternative to the built-in path,
    usable by default when present. DISCOVER_IF_PRESENT: opportunistically
    used when detected, otherwise the existing baseline discipline applies
    unchanged. ON_DEMAND: narrow/triggered by a specific need, never a
    default-path provider. REFERENCE_ONLY: knowledge about a collection or
    reference source, never itself a selectable/executable provider.
    """

    OPTIONAL_PROVIDER = auto()
    DISCOVER_IF_PRESENT = auto()
    ON_DEMAND = auto()
    REFERENCE_ONLY = auto()


@dataclass(frozen=True)
class ProviderDefinition:
    """Static identity and selection policy for one known external provider.

    verification_requirement is deliberately left blank ("") for the common
    case where presence alone carries no extra verification caveat beyond
    whatever the capability's own downstream evidence-collection already
    does (out of scope for this module either way); it is only populated
    where a provider materially needs to say more.
    """

    capability_id: str
    provider_id: str
    display_name: str
    classification: ProviderClassification
    preference_rank: int
    trigger: str
    scope: str
    verification_requirement: str = ""
    # PRESENT never means auto-invoke for a provider marked True here: it
    # means "recommend, and require Operator authorization via the existing
    # studio-gate contract before anything runs" (see resolve_provider's
    # dedicated branch below). Default False -- every existing provider
    # entry is unaffected; set True only where that stronger discipline is
    # actually required (e.g. Wayfinder).
    requires_operator_authorization: bool = False

    def __post_init__(self):
        if not isinstance(self.classification, ProviderClassification):
            raise ValueError(
                f"classification must be a ProviderClassification, got {self.classification!r}"
            )


class ProviderCatalog:
    """Holds the known provider definitions, keyed by (capability_id, provider_id)."""

    def __init__(self, definitions: list[ProviderDefinition]):
        keys = [(item.capability_id, item.provider_id) for item in definitions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate (capability_id, provider_id) pair")
        self._definitions = tuple(definitions)

    def providers_for(self, capability_id: str) -> list[ProviderDefinition]:
        return sorted(
            (item for item in self._definitions if item.capability_id == capability_id),
            key=lambda item: item.preference_rank,
        )


# Fallback behavior belongs to the capability, not to any individual
# provider: it is what honestly happens when nothing registered for that
# capability is available. Deliberately not itself a ProviderDefinition --
# a fallback like "Director-native interrogation" is internal system
# behavior, not an external provider.
CAPABILITY_FALLBACKS: dict[str, str] = {
    "requirements_interrogation": "Director-native interrogation.",
    "tdd": "existing Coder discipline.",
    "debugging": "Coder/Resolver baseline.",
    "security_review": "Critic/manual security review.",
    "browser_runtime_verification": (
        "no automated browser runtime verification available; manual check "
        "required, do not treat as equivalent to an automated pass."
    ),
    "worktree_creation": "manual git worktree creation.",
    "modernization": "Coder/Resolver.",
    "interactive_grill_ui": "normal studio-grill interaction.",
    "ui_audit": "narrower manual UI review by Critic/Coder, or explicit gap noted.",
    "test_coverage_audit": "narrower manual coverage review, or explicit gap noted.",
    "memory_curation": "no ship-time MemPalace curation (diary/KG mirror/pruning); mnemo per-project recall only.",
    "assumption_analysis": "narrower manual assumption analysis, or explicit gap noted.",
    "decision_wayfinding": (
        "Director-native staged discovery via requirements_interrogation. "
        "This is REDUCED COVERAGE, not equivalent to a persistent Wayfinder "
        "decision map, when genuine multi-session fog remains."
    ),
    "durable_decision_documentation": (
        "Director-native interrogation with durable decisions recorded in "
        "the owning project's Notion (intent/architecture/roadmap) and/or "
        "GitHub engineering record — never a second roadmap mirror, never "
        "cross-project writes."
    ),
}

# Capabilities where the honest fallback answer is "no automated equivalent
# exists" -- never silently substitute (e.g. "screenshot review is
# equivalent"). resolve_provider() sets execution_mode="unavailable" and a
# non-None explicit_gap for these when falling back.
_NO_AUTOMATED_EQUIVALENT_CAPABILITIES: frozenset[str] = frozenset({"browser_runtime_verification"})

DEFAULT_PROVIDERS: list[ProviderDefinition] = [
    ProviderDefinition(
        capability_id="requirements_interrogation",
        provider_id="matt-pocock-grill-me",
        display_name="grill-me",
        classification=ProviderClassification.OPTIONAL_PROVIDER,
        preference_rank=0,
        trigger="Requirements are ambiguous or under-specified before work begins.",
        scope="Requirements interrogation only.",
    ),
    ProviderDefinition(
        capability_id="durable_decision_documentation",
        provider_id="matt-pocock-grill-with-docs",
        display_name="grill-with-docs",
        classification=ProviderClassification.OPTIONAL_PROVIDER,
        preference_rank=0,
        trigger=(
            "A repository task needs durable decisions captured within one "
            "session (REPO + DURABLE_DECISIONS + ONE_SESSION) — ADRs/glossary "
            "style records in the owning project only."
        ),
        scope=(
            "One-session requirements interrogation that also records durable "
            "decisions. Maps to the existing Notion/GitHub truth model "
            "(Notion = product intent/architecture/roadmap; GitHub = "
            "engineering record). Does not duplicate the Notion roadmap into "
            "GitHub. Project-isolated: no cross-project writes. Not a Master "
            "Plan and not a second planner."
        ),
    ),
    ProviderDefinition(
        capability_id="tdd",
        provider_id="superpowers-tdd",
        display_name="superpowers:test-driven-development",
        classification=ProviderClassification.DISCOVER_IF_PRESENT,
        preference_rank=0,
        trigger="A test-driven-development workflow is applicable to the current task.",
        scope="TDD workflow guidance only.",
    ),
    ProviderDefinition(
        capability_id="debugging",
        provider_id="superpowers-debugging",
        display_name="superpowers:systematic-debugging",
        classification=ProviderClassification.DISCOVER_IF_PRESENT,
        preference_rank=0,
        trigger="A defect needs systematic root-cause debugging.",
        scope="Systematic debugging workflow guidance only.",
    ),
    ProviderDefinition(
        capability_id="security_review",
        provider_id="security-guidance",
        display_name="security-guidance",
        classification=ProviderClassification.DISCOVER_IF_PRESENT,
        preference_rank=0,
        trigger="A change touches security-sensitive surface area.",
        scope="Security review guidance only.",
    ),
    ProviderDefinition(
        capability_id="browser_runtime_verification",
        provider_id="chrome-devtools-mcp",
        display_name="chrome-devtools-mcp",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger="A change needs live browser runtime verification.",
        scope="Browser runtime verification only.",
        verification_requirement=(
            "Presence alone is not verification; the provider must actually "
            "run and report a result for this task before it counts as done."
        ),
    ),
    ProviderDefinition(
        capability_id="worktree_creation",
        provider_id="native-enterworktree",
        display_name="EnterWorktree",
        classification=ProviderClassification.OPTIONAL_PROVIDER,
        preference_rank=0,
        trigger="A worktree needs to be created for isolated work.",
        scope="Worktree creation only.",
    ),
    ProviderDefinition(
        capability_id="worktree_creation",
        provider_id="worktrunk",
        display_name="Worktrunk",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=1,
        trigger="A richer optional worktree-management alternative is desired.",
        scope="Worktree creation only.",
    ),
    ProviderDefinition(
        capability_id="modernization",
        provider_id="code-modernization",
        display_name="Code Modernization",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger="A modernization pass over legacy code is requested.",
        scope="Code modernization only.",
    ),
    ProviderDefinition(
        capability_id="interactive_grill_ui",
        provider_id="grill-with-ui",
        display_name="grill-with-ui",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger="An interactive UI is desired for a studio-grill interrogation session.",
        scope="Interactive grill UI only.",
    ),
    ProviderDefinition(
        capability_id="ui_audit",
        provider_id="gsd-ui-auditor",
        display_name="gsd-ui-auditor",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger=(
            "A frontend implementation has already been built and is being "
            "retroactively audited -- typically spawned by the /gsd-ui-review "
            "orchestrator, not requested ahead of implementation."
        ),
        scope=(
            "Retroactive 6-pillar UI audit (copywriting, visuals, color, "
            "typography, spacing, experience design) against an already-implemented "
            "frontend; also runs a registry-safety scan for suspicious code in "
            "installed third-party shadcn components when present. "
            "Read/Write/Bash/Grep/Glob; cannot Edit/MultiEdit implementation."
        ),
    ),
    ProviderDefinition(
        capability_id="test_coverage_audit",
        provider_id="gsd-nyquist-auditor",
        display_name="gsd-nyquist-auditor",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger=(
            "Specific coverage gaps have already been identified (e.g. by a prior "
            "Nyquist validation pass) and need tests generated and run to fill "
            "them -- not a request to discover which gaps exist."
        ),
        scope=(
            "Generates and runs tests to fill already-identified coverage gaps, "
            "debugging failing tests up to 3 iterations; escalates implementation "
            "bugs rather than fixing them. Does not itself discover or audit "
            "coverage gaps."
        ),
    ),
    ProviderDefinition(
        capability_id="memory_curation",
        provider_id="gsd-mempalace-curator",
        display_name="gsd-mempalace-curator",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger=(
            "Runs automatically at ship:post, once per phase, after verification "
            "has passed -- only when the project's mempalace.enabled config flag "
            "is set. Not an on-demand capability invoked for a stated curation need."
        ),
        scope=(
            "Ship-time MemPalace curation: session diary entry, extract-learnings-"
            "to-KG mirroring, optional cross-project tunnel proposals, and "
            "wing-scoped drawer pruning (deletion-adjacent) -- each independently "
            "best-effort and config-gated; never touches drawers outside this "
            "project's wing."
        ),
    ),
    ProviderDefinition(
        capability_id="assumption_analysis",
        provider_id="gsd-assumptions-analyzer",
        display_name="gsd-assumptions-analyzer",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger="A plan or design needs its assumptions surfaced and analyzed.",
        scope="Assumption analysis only.",
    ),
    ProviderDefinition(
        capability_id="decision_wayfinding",
        provider_id="matt-pocock-wayfinder",
        display_name="Matt Pocock Wayfinder",
        classification=ProviderClassification.ON_DEMAND,
        preference_rank=0,
        trigger=(
            "Requirements interrogation cannot yet state the remaining decision "
            "surface precisely, and/or resolving it plausibly exceeds one "
            "session's context budget -- Wayfinder's own escalation test, never "
            "file count, feature count, prompt length, or a complexity score."
        ),
        scope=(
            "Multi-session decision-space charting only; does not itself imply "
            "tracker mutation authorization, which remains a separate Operator "
            "act even after this recommendation is accepted."
        ),
        verification_requirement=(
            "Presence alone is not authorization; Operator approval via "
            "studio-gate is required before Wayfinder may run, and its actual "
            "tracker/session execution is entirely outside this module's scope."
        ),
        requires_operator_authorization=True,
    ),
    # Reference-only knowledge: illustrative collections, never themselves
    # "the" provider for any capability, and structurally non-selectable
    # by resolve_provider() regardless of any availability input.
    ProviderDefinition(
        capability_id="external_skill_reference",
        provider_id="addy-osmani-agent-skills",
        display_name="Addy Osmani agent-skills",
        classification=ProviderClassification.REFERENCE_ONLY,
        preference_rank=0,
        trigger="Reference knowledge only; not itself selectable.",
        scope="Reference knowledge about this skill collection only.",
    ),
    ProviderDefinition(
        capability_id="external_skill_reference",
        provider_id="ecc",
        display_name="ECC",
        classification=ProviderClassification.REFERENCE_ONLY,
        preference_rank=1,
        trigger="Reference knowledge only; not itself selectable.",
        scope="Reference knowledge about this skill collection only.",
    ),
    ProviderDefinition(
        capability_id="external_skill_reference",
        provider_id="caveman",
        display_name="Caveman",
        classification=ProviderClassification.REFERENCE_ONLY,
        preference_rank=2,
        trigger="Reference knowledge only; not itself selectable.",
        scope="Reference knowledge about this skill collection only.",
    ),
]


def default_provider_catalog() -> ProviderCatalog:
    """The canonical Workflow Kit capability/provider policy catalog: static
    knowledge and selection policy only. No detection, installation, or
    execution wiring belongs here -- those are separate, later concerns.
    """
    return ProviderCatalog(list(DEFAULT_PROVIDERS))


@dataclass(frozen=True)
class ProviderSelection:
    """Result of resolving one capability need to a provider (or fallback)."""

    capability_id: str
    selected_provider_id: str | None
    reason: str
    fallback_used: bool
    fallback_description: str | None
    verification_requirement: str | None
    execution_mode: str  # "automated" | "manual" | "unavailable" | "operator_approval_required"
    explicit_gap: str | None
    # Set only when a candidate is PRESENT but requires_operator_authorization
    # is True: something is surfaced for Operator decision, not yet running,
    # never auto-invoked. Distinct in meaning from selected_provider_id,
    # which must only ever be set when something WILL run automatically.
    # Default None for every existing selection path -- no behavior change
    # to any prior selection.
    recommended_provider_id: str | None = None


def resolve_provider(
    capability_id: str,
    *,
    availability: dict[str, LifecycleState],
    scope: str | None = None,
    catalog: ProviderCatalog | None = None,
) -> ProviderSelection:
    """Deterministically resolve a capability need to a provider, or the
    capability's honest fallback.

    Walks the capability's providers in preference_rank order, skipping any
    REFERENCE_ONLY entry entirely (never selectable). The first candidate
    whose availability is PRESENT is selected. ABSENT and UNKNOWN are both
    treated as not-currently-usable (fail closed on unsupported certainty --
    UNKNOWN is never treated as available). If nothing is PRESENT, the
    capability's CAPABILITY_FALLBACKS entry is used: execution_mode is
    "unavailable" with a non-None explicit_gap for capabilities where no
    automated equivalent exists (e.g. browser_runtime_verification), or
    "manual" for capabilities with a real, if less automated, fallback.

    `scope` is accepted for future narrowing but does not currently affect
    selection; providers are only ever considered from the exact
    capability_id requested, never any other capability's list.
    """
    catalog = catalog or default_provider_catalog()
    candidates = [
        item
        for item in catalog.providers_for(capability_id)
        if item.classification is not ProviderClassification.REFERENCE_ONLY
    ]

    for candidate in candidates:
        state = availability.get(candidate.provider_id, LifecycleState.UNKNOWN)
        if state is LifecycleState.PRESENT:
            if candidate.requires_operator_authorization:
                # PRESENT never means auto-invoke here: surface the
                # recommendation and require explicit Operator authorization
                # via the existing studio-gate contract before anything
                # runs. Nothing is auto-selected (selected_provider_id stays
                # None); the fallback description is populated proactively
                # so a caller who can't get Operator authorization already
                # has the reduced-fallback description without a second call.
                return ProviderSelection(
                    capability_id=capability_id,
                    selected_provider_id=None,
                    reason=(
                        f"WAYFINDER_RECOMMENDED: {candidate.provider_id} is present "
                        f"and preferred (rank {candidate.preference_rank}) but requires "
                        "operator authorization before it may run; "
                        "OPERATOR_APPROVAL_REQUIRED via studio-gate."
                    ),
                    fallback_used=True,
                    fallback_description=CAPABILITY_FALLBACKS.get(capability_id),
                    verification_requirement=candidate.verification_requirement or None,
                    execution_mode="operator_approval_required",
                    explicit_gap=None,
                    recommended_provider_id=candidate.provider_id,
                )
            return ProviderSelection(
                capability_id=capability_id,
                selected_provider_id=candidate.provider_id,
                reason=f"{candidate.provider_id} is present and preferred (rank {candidate.preference_rank}).",
                fallback_used=False,
                fallback_description=None,
                verification_requirement=candidate.verification_requirement or None,
                execution_mode="automated",
                explicit_gap=None,
            )

    fallback_description = CAPABILITY_FALLBACKS.get(capability_id)
    if capability_id in _NO_AUTOMATED_EQUIVALENT_CAPABILITIES:
        return ProviderSelection(
            capability_id=capability_id,
            selected_provider_id=None,
            reason="No registered provider is present; no automated equivalent exists for this capability.",
            fallback_used=True,
            fallback_description=fallback_description,
            verification_requirement=None,
            execution_mode="unavailable",
            explicit_gap=fallback_description,
        )

    return ProviderSelection(
        capability_id=capability_id,
        selected_provider_id=None,
        reason="No registered provider is present; using the capability's fallback.",
        fallback_used=True,
        fallback_description=fallback_description,
        verification_requirement=None,
        execution_mode="manual" if fallback_description else "unavailable",
        explicit_gap=None if fallback_description else "no fallback description registered for this capability.",
    )


class CapabilityProviderCLIError(RuntimeError):
    pass


def _parse_availability(raw: str | None) -> dict[str, LifecycleState]:
    """Parse the --availability-json CLI argument into a
    dict[str, LifecycleState]. Omitted input means no availability is
    known for any provider -- resolve_provider()'s own existing
    .get(id, LifecycleState.UNKNOWN) default already covers that case,
    so an empty dict here is sufficient and no separate default needs
    inventing.
    """
    if raw is None:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapabilityProviderCLIError(f"invalid --availability-json: {exc}") from exc

    if not isinstance(parsed, dict):
        raise CapabilityProviderCLIError("--availability-json must be a JSON object")

    availability: dict[str, LifecycleState] = {}
    for provider_id, state_name in parsed.items():
        if not isinstance(state_name, str):
            raise CapabilityProviderCLIError(
                f"--availability-json value for {provider_id!r} must be a string state name"
            )
        try:
            availability[provider_id] = LifecycleState[state_name]
        except KeyError as exc:
            raise CapabilityProviderCLIError(
                f"unknown LifecycleState {state_name!r} for provider {provider_id!r}"
            ) from exc

    return availability


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability", required=True, help="capability_id to resolve a provider for")
    parser.add_argument(
        "--availability-json",
        default=None,
        help="JSON object mapping provider_id to a LifecycleState name "
             "(PRESENT/ABSENT/NOT_APPLICABLE/UNKNOWN); omitted providers "
             "default to UNKNOWN",
    )
    args = parser.parse_args()

    try:
        availability = _parse_availability(args.availability_json)
        selection = resolve_provider(args.capability, availability=availability)
    except CapabilityProviderCLIError as exc:
        print(f"capability-providers error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(selection), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
