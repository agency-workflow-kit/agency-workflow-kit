#!/usr/bin/env python3
"""Provider-neutral dynamic execution routing.

Selects runtime/provider, model, effort, and capabilities for a task from a
declarative catalog plus deterministic ranking rules.

Honesty rules:
  * Deterministic rules decide first. Ambiguous ties may emit a bounded
    decision_request payload shaped for the specialist-result contract, but
    this module never imports JEV, decision-provider adapters, or vendor SDKs.
  * No blanket model inheritance: ``inherit``, empty, or missing model ids are
    refused. Every selected model must be an explicit catalog pin.
  * Unsupported combinations fail closed (GAP / REFUSED), never silent success.
  * Operator constraints are hard filters, never soft preferences.
  * Certified availability is an injected input; this module does not probe
    the environment or touch the network.

Not wired into Front Door / Factory / renderer / autonomous execution —
INTEGRATION is a separate slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class ExecutionRoutingError(RuntimeError):
    """Fail-closed execution-routing contract violation."""


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

TASK_TYPES = frozenset({
    "research",
    "coding",
    "critique",
    "resolution",
    "orchestration",
    "general",
})

COMPLEXITY_LEVELS = frozenset({"simple", "moderate", "complex"})
RISK_LEVELS = frozenset({"low", "medium", "high"})
PREFERENCE_LEVELS = frozenset({"lowest", "balanced", "quality"})
EFFORT_LEVELS = frozenset({"low", "medium", "high"})

STATUS_SELECTED = "SELECTED"
STATUS_GAP = "GAP"
STATUS_REFUSED = "REFUSED"
STATUS_AMBIGUOUS = "AMBIGUOUS"

OUTCOME_STATUSES = frozenset({
    STATUS_SELECTED,
    STATUS_GAP,
    STATUS_REFUSED,
    STATUS_AMBIGUOUS,
})

ROUTING_MODE_DYNAMIC = "dynamic"
ROUTING_MODE_STATIC = "static"
ROUTING_MODES = frozenset({ROUTING_MODE_DYNAMIC, ROUTING_MODE_STATIC})

FORBIDDEN_MODEL_TOKENS = frozenset({
    "",
    "inherit",
    "default",
    "auto",
    "parent",
})

_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}
_COMPLEXITY_MIN_EFFORT = {
    "simple": "low",
    "moderate": "medium",
    "complex": "high",
}
_COMPLEXITY_MIN_QUALITY = {
    "simple": 1,
    "moderate": 2,
    "complex": 3,
}
_RISK_MIN_QUALITY = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


# ---------------------------------------------------------------------------
# Catalog / constraints / request / result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionOption:
    """One explicit, reviewable execution combination.

    Models must be pinned strings. ``inherit`` and empty model ids are
    structurally rejected at construction time.
    """

    option_id: str
    provider_id: str
    runtime_id: str
    model: str
    effort: str
    capabilities: frozenset[str]
    task_types: frozenset[str]
    cost_tier: int  # 1 = cheapest … 3 = most expensive
    latency_tier: int  # 1 = fastest … 3 = slowest
    quality_tier: int  # 1 = basic … 3 = max
    risk_ceiling: str  # max risk this option may handle

    def __post_init__(self) -> None:
        if not self.option_id.strip():
            raise ExecutionRoutingError("option_id must be a non-empty string")
        if not self.provider_id.strip():
            raise ExecutionRoutingError("provider_id must be a non-empty string")
        if not self.runtime_id.strip():
            raise ExecutionRoutingError("runtime_id must be a non-empty string")
        model = self.model.strip().lower() if isinstance(self.model, str) else ""
        if model in FORBIDDEN_MODEL_TOKENS or not self.model.strip():
            raise ExecutionRoutingError(
                f"option {self.option_id!r} refuses model token {self.model!r}; "
                "explicit pinned model required (no inherit)"
            )
        if self.effort not in EFFORT_LEVELS:
            raise ExecutionRoutingError(
                f"option {self.option_id!r} effort must be one of "
                f"{sorted(EFFORT_LEVELS)}, got {self.effort!r}"
            )
        if self.risk_ceiling not in RISK_LEVELS:
            raise ExecutionRoutingError(
                f"option {self.option_id!r} risk_ceiling must be one of "
                f"{sorted(RISK_LEVELS)}, got {self.risk_ceiling!r}"
            )
        for tier_name, tier in (
            ("cost_tier", self.cost_tier),
            ("latency_tier", self.latency_tier),
            ("quality_tier", self.quality_tier),
        ):
            if tier not in (1, 2, 3):
                raise ExecutionRoutingError(
                    f"option {self.option_id!r} {tier_name} must be 1, 2, or 3"
                )
        unknown_tasks = set(self.task_types) - TASK_TYPES
        if unknown_tasks:
            raise ExecutionRoutingError(
                f"option {self.option_id!r} has unknown task_types: "
                f"{sorted(unknown_tasks)}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "provider_id": self.provider_id,
            "runtime_id": self.runtime_id,
            "model": self.model,
            "effort": self.effort,
            "capabilities": sorted(self.capabilities),
            "task_types": sorted(self.task_types),
            "cost_tier": self.cost_tier,
            "latency_tier": self.latency_tier,
            "quality_tier": self.quality_tier,
            "risk_ceiling": self.risk_ceiling,
        }


@dataclass(frozen=True)
class OperatorConstraints:
    """Hard Operator filters. Absent allow-lists mean no extra restriction."""

    allowed_provider_ids: frozenset[str] | None = None
    allowed_runtime_ids: frozenset[str] | None = None
    allowed_model_ids: frozenset[str] | None = None
    allowed_effort_levels: frozenset[str] | None = None
    forbidden_capabilities: frozenset[str] = frozenset()
    require_certified: bool = True
    max_cost_tier: int | None = None
    min_quality_tier: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_provider_ids": (
                sorted(self.allowed_provider_ids)
                if self.allowed_provider_ids is not None
                else None
            ),
            "allowed_runtime_ids": (
                sorted(self.allowed_runtime_ids)
                if self.allowed_runtime_ids is not None
                else None
            ),
            "allowed_model_ids": (
                sorted(self.allowed_model_ids)
                if self.allowed_model_ids is not None
                else None
            ),
            "allowed_effort_levels": (
                sorted(self.allowed_effort_levels)
                if self.allowed_effort_levels is not None
                else None
            ),
            "forbidden_capabilities": sorted(self.forbidden_capabilities),
            "require_certified": self.require_certified,
            "max_cost_tier": self.max_cost_tier,
            "min_quality_tier": self.min_quality_tier,
        }


@dataclass(frozen=True)
class CertifiedAvailability:
    """Injected certification / capability availability — never discovered here."""

    certified_providers: frozenset[str] = frozenset()
    certified_runtimes: frozenset[str] = frozenset()
    available_capabilities: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {
            "certified_providers": sorted(self.certified_providers),
            "certified_runtimes": sorted(self.certified_runtimes),
            "available_capabilities": sorted(self.available_capabilities),
        }


@dataclass(frozen=True)
class RoutingRequest:
    """Inputs for one execution-routing decision."""

    task_type: str
    complexity: str
    risk: str
    latency_preference: str
    cost_preference: str
    required_capabilities: tuple[str, ...]
    operator_constraints: OperatorConstraints = field(
        default_factory=OperatorConstraints
    )
    certified_availability: CertifiedAvailability = field(
        default_factory=CertifiedAvailability
    )
    request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_type": self.task_type,
            "complexity": self.complexity,
            "risk": self.risk,
            "latency_preference": self.latency_preference,
            "cost_preference": self.cost_preference,
            "required_capabilities": list(self.required_capabilities),
            "operator_constraints": self.operator_constraints.as_dict(),
            "certified_availability": self.certified_availability.as_dict(),
        }
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        return payload


@dataclass
class RoutingResult:
    """Recorded routing outcome — never silent authority."""

    passed: bool
    status: str
    selected_provider: str | None
    selected_runtime: str | None
    selected_model: str | None
    selected_effort: str | None
    selected_option_id: str | None
    selected_capabilities: list[str]
    routing_reason: str
    decision_request: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    eliminated: list[dict[str, str]] = field(default_factory=list)
    routing_mode: str = ROUTING_MODE_DYNAMIC

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status,
            "selected_provider": self.selected_provider,
            "selected_runtime": self.selected_runtime,
            "selected_model": self.selected_model,
            "selected_effort": self.selected_effort,
            "selected_option_id": self.selected_option_id,
            "selected_capabilities": list(self.selected_capabilities),
            "routing_reason": self.routing_reason,
            "routing_mode": self.routing_mode,
            "decision_request": (
                dict(self.decision_request) if self.decision_request else None
            ),
            "notes": list(self.notes),
            "eliminated": [dict(item) for item in self.eliminated],
        }


class ExecutionCatalog:
    """Holds known execution options; rejects duplicate option_ids."""

    def __init__(self, options: Sequence[ExecutionOption]):
        ids = [item.option_id for item in options]
        if len(ids) != len(set(ids)):
            raise ExecutionRoutingError("duplicate option_id in ExecutionCatalog")
        self._options = tuple(options)

    @property
    def options(self) -> tuple[ExecutionOption, ...]:
        return self._options

    def get(self, option_id: str) -> ExecutionOption | None:
        for option in self._options:
            if option.option_id == option_id:
                return option
        return None


# ---------------------------------------------------------------------------
# Default catalog (provider-neutral pins; Claude Code is one certified runtime)
# ---------------------------------------------------------------------------

_ALL_CORE_TASKS = frozenset({
    "research",
    "coding",
    "critique",
    "resolution",
    "orchestration",
    "general",
})

DEFAULT_EXECUTION_OPTIONS: tuple[ExecutionOption, ...] = (
    ExecutionOption(
        option_id="claude-code-haiku-low",
        provider_id="claude-code",
        runtime_id="claude-code-cli",
        model="claude-haiku-4.5",
        effort="low",
        capabilities=frozenset({"text_generation", "tool_use"}),
        task_types=frozenset({"research", "coding", "general"}),
        cost_tier=1,
        latency_tier=1,
        quality_tier=1,
        risk_ceiling="low",
    ),
    ExecutionOption(
        option_id="claude-code-sonnet-medium",
        provider_id="claude-code",
        runtime_id="claude-code-cli",
        model="claude-sonnet-5",
        effort="medium",
        capabilities=frozenset({"text_generation", "tool_use", "code_edit"}),
        task_types=frozenset({"research", "coding", "resolution", "general"}),
        cost_tier=2,
        latency_tier=2,
        quality_tier=2,
        risk_ceiling="medium",
    ),
    ExecutionOption(
        option_id="claude-code-sonnet-high",
        provider_id="claude-code",
        runtime_id="claude-code-cli",
        model="claude-sonnet-5",
        effort="high",
        capabilities=frozenset({"text_generation", "tool_use", "code_edit"}),
        task_types=frozenset({"coding", "resolution", "general"}),
        cost_tier=2,
        latency_tier=2,
        quality_tier=2,
        risk_ceiling="medium",
    ),
    ExecutionOption(
        option_id="claude-code-opus-high",
        provider_id="claude-code",
        runtime_id="claude-code-cli",
        model="claude-opus-5",
        effort="high",
        capabilities=frozenset({
            "text_generation",
            "tool_use",
            "code_edit",
            "critique",
            "orchestration",
        }),
        task_types=_ALL_CORE_TASKS,
        cost_tier=3,
        latency_tier=3,
        quality_tier=3,
        risk_ceiling="high",
    ),
    ExecutionOption(
        option_id="local-deterministic-low",
        provider_id="local-deterministic",
        runtime_id="local-fake",
        model="local-deterministic",
        effort="low",
        capabilities=frozenset({"text_generation"}),
        task_types=frozenset({"general"}),
        cost_tier=1,
        latency_tier=1,
        quality_tier=1,
        risk_ceiling="low",
    ),
)


def default_execution_catalog() -> ExecutionCatalog:
    return ExecutionCatalog(DEFAULT_EXECUTION_OPTIONS)


# ---------------------------------------------------------------------------
# Validation / filtering / scoring
# ---------------------------------------------------------------------------


def validate_routing_request(request: RoutingRequest) -> None:
    """Validate closed vocabularies. Raises ExecutionRoutingError."""
    if request.task_type not in TASK_TYPES:
        raise ExecutionRoutingError(
            f"task_type must be one of {sorted(TASK_TYPES)}, got {request.task_type!r}"
        )
    if request.complexity not in COMPLEXITY_LEVELS:
        raise ExecutionRoutingError(
            f"complexity must be one of {sorted(COMPLEXITY_LEVELS)}, "
            f"got {request.complexity!r}"
        )
    if request.risk not in RISK_LEVELS:
        raise ExecutionRoutingError(
            f"risk must be one of {sorted(RISK_LEVELS)}, got {request.risk!r}"
        )
    if request.latency_preference not in PREFERENCE_LEVELS:
        raise ExecutionRoutingError(
            f"latency_preference must be one of {sorted(PREFERENCE_LEVELS)}, "
            f"got {request.latency_preference!r}"
        )
    if request.cost_preference not in PREFERENCE_LEVELS:
        raise ExecutionRoutingError(
            f"cost_preference must be one of {sorted(PREFERENCE_LEVELS)}, "
            f"got {request.cost_preference!r}"
        )
    for cap in request.required_capabilities:
        if not isinstance(cap, str) or not cap.strip():
            raise ExecutionRoutingError(
                "required_capabilities entries must be non-empty strings"
            )
    constraints = request.operator_constraints
    if constraints.allowed_effort_levels is not None:
        bad = set(constraints.allowed_effort_levels) - EFFORT_LEVELS
        if bad:
            raise ExecutionRoutingError(
                f"operator allowed_effort_levels contains unknown values: "
                f"{sorted(bad)}"
            )
    if constraints.max_cost_tier is not None and constraints.max_cost_tier not in (1, 2, 3):
        raise ExecutionRoutingError("operator max_cost_tier must be 1, 2, or 3")
    if constraints.min_quality_tier is not None and constraints.min_quality_tier not in (
        1,
        2,
        3,
    ):
        raise ExecutionRoutingError("operator min_quality_tier must be 1, 2, or 3")


def _eliminate(
    eliminated: list[dict[str, str]],
    option: ExecutionOption,
    reason: str,
) -> None:
    eliminated.append({"option_id": option.option_id, "reason": reason})


def _passes_operator(
    option: ExecutionOption,
    constraints: OperatorConstraints,
    eliminated: list[dict[str, str]],
) -> bool:
    if (
        constraints.allowed_provider_ids is not None
        and option.provider_id not in constraints.allowed_provider_ids
    ):
        _eliminate(eliminated, option, "provider outside Operator allow-list")
        return False
    if (
        constraints.allowed_runtime_ids is not None
        and option.runtime_id not in constraints.allowed_runtime_ids
    ):
        _eliminate(eliminated, option, "runtime outside Operator allow-list")
        return False
    if (
        constraints.allowed_model_ids is not None
        and option.model not in constraints.allowed_model_ids
    ):
        _eliminate(eliminated, option, "model outside Operator allow-list")
        return False
    if (
        constraints.allowed_effort_levels is not None
        and option.effort not in constraints.allowed_effort_levels
    ):
        _eliminate(eliminated, option, "effort outside Operator allow-list")
        return False
    if option.capabilities & constraints.forbidden_capabilities:
        _eliminate(
            eliminated,
            option,
            "option capabilities intersect Operator forbidden_capabilities",
        )
        return False
    if (
        constraints.max_cost_tier is not None
        and option.cost_tier > constraints.max_cost_tier
    ):
        _eliminate(eliminated, option, "cost_tier exceeds Operator max_cost_tier")
        return False
    if (
        constraints.min_quality_tier is not None
        and option.quality_tier < constraints.min_quality_tier
    ):
        _eliminate(eliminated, option, "quality_tier below Operator min_quality_tier")
        return False
    return True


def _passes_availability(
    option: ExecutionOption,
    request: RoutingRequest,
    eliminated: list[dict[str, str]],
) -> bool:
    constraints = request.operator_constraints
    availability = request.certified_availability
    if constraints.require_certified:
        if option.provider_id not in availability.certified_providers:
            _eliminate(eliminated, option, "provider not in certified availability")
            return False
        if option.runtime_id not in availability.certified_runtimes:
            _eliminate(eliminated, option, "runtime not in certified availability")
            return False
    missing_caps = [
        cap
        for cap in request.required_capabilities
        if cap not in option.capabilities
        or cap not in availability.available_capabilities
    ]
    if missing_caps:
        _eliminate(
            eliminated,
            option,
            "capability gap: " + ", ".join(sorted(missing_caps)),
        )
        return False
    return True


def filter_viable_options(
    request: RoutingRequest,
    catalog: ExecutionCatalog,
) -> tuple[list[ExecutionOption], list[dict[str, str]]]:
    """Apply hard filters. Returns (survivors, elimination ledger)."""
    eliminated: list[dict[str, str]] = []
    survivors: list[ExecutionOption] = []
    min_effort = _COMPLEXITY_MIN_EFFORT[request.complexity]
    min_quality = max(
        _COMPLEXITY_MIN_QUALITY[request.complexity],
        _RISK_MIN_QUALITY[request.risk],
    )
    risk_needed = _RISK_RANK[request.risk]

    for option in catalog.options:
        if request.task_type not in option.task_types:
            _eliminate(eliminated, option, "task_type unsupported by option")
            continue
        if _RISK_RANK[option.risk_ceiling] < risk_needed:
            _eliminate(eliminated, option, "risk exceeds option risk_ceiling")
            continue
        if _EFFORT_RANK[option.effort] < _EFFORT_RANK[min_effort]:
            _eliminate(
                eliminated,
                option,
                f"effort below complexity floor ({min_effort})",
            )
            continue
        if option.quality_tier < min_quality:
            _eliminate(
                eliminated,
                option,
                f"quality_tier below complexity/risk floor ({min_quality})",
            )
            continue
        if not _passes_operator(option, request.operator_constraints, eliminated):
            continue
        if not _passes_availability(option, request, eliminated):
            continue
        survivors.append(option)
    return survivors, eliminated


def _preference_weight(preference: str, tier: int, *, invert: bool) -> int:
    """Lower score is better. invert=True means prefer higher tier values."""
    if preference == "balanced":
        return abs(tier - 2)
    if preference == "lowest":
        return tier if not invert else (4 - tier)
    return (4 - tier) if invert else tier


def score_option(option: ExecutionOption, request: RoutingRequest) -> tuple[int, ...]:
    """Deterministic total-order key. Lower is better."""
    cost_score = _preference_weight(
        request.cost_preference, option.cost_tier, invert=False
    )
    latency_score = _preference_weight(
        request.latency_preference, option.latency_tier, invert=False
    )
    if request.cost_preference == "quality" or request.latency_preference == "quality":
        quality_score = 4 - option.quality_tier
    elif request.complexity == "complex" or request.risk == "high":
        quality_score = 4 - option.quality_tier
    else:
        quality_score = abs(option.quality_tier - 2)

    effort_target = _COMPLEXITY_MIN_EFFORT[request.complexity]
    effort_score = abs(_EFFORT_RANK[option.effort] - _EFFORT_RANK[effort_target])

    return (
        cost_score,
        latency_score,
        quality_score,
        effort_score,
        option.cost_tier,
        option.latency_tier,
        -option.quality_tier,
        option.option_id,
    )


def _score_without_identity(
    option: ExecutionOption, request: RoutingRequest
) -> tuple[int, ...]:
    """Scoring tuple used to detect genuine ambiguity (drops option_id)."""
    return score_option(option, request)[:-1]


def build_decision_request(
    request: RoutingRequest,
    candidates: Sequence[ExecutionOption],
) -> dict[str, Any]:
    """Emit a bounded decision_request dict without importing decision providers.

    Shape matches specialist-result.schema.json decision_request. Callers may
    hand this to an authorized surface; this module never consults a provider.
    """
    if len(candidates) < 2:
        raise ExecutionRoutingError(
            "decision_request requires at least two viable candidates"
        )
    hard_requirements = [
        f"task_type={request.task_type}",
        f"complexity={request.complexity}",
        f"risk={request.risk}",
        f"latency_preference={request.latency_preference}",
        f"cost_preference={request.cost_preference}",
    ]
    if request.required_capabilities:
        hard_requirements.append(
            "required_capabilities="
            + ",".join(sorted(request.required_capabilities))
        )
    payload: dict[str, Any] = {
        "question": (
            "Which certified execution option best satisfies the stated "
            "routing hard requirements?"
        ),
        "decision_class": "candidate_selection",
        "candidates": [
            {
                "id": option.option_id,
                "summary": (
                    f"provider={option.provider_id} runtime={option.runtime_id} "
                    f"model={option.model} effort={option.effort} "
                    f"cost_tier={option.cost_tier} quality_tier={option.quality_tier}"
                ),
            }
            for option in candidates
        ],
        "hard_requirements": hard_requirements,
        "fallback": (
            "continue with normal reasoning using no selected execution option; "
            "do not invent a provider/model/effort; no degradation beyond the "
            "explicit GAP"
        ),
    }
    if request.request_id is not None:
        payload["request_id"] = f"exec-route:{request.request_id}"
    return payload


def _selected_result(
    option: ExecutionOption,
    *,
    reason: str,
    eliminated: list[dict[str, str]],
    notes: Sequence[str] | None = None,
    routing_mode: str = ROUTING_MODE_DYNAMIC,
) -> RoutingResult:
    if routing_mode not in ROUTING_MODES:
        raise ExecutionRoutingError(f"unknown routing_mode {routing_mode!r}")
    return RoutingResult(
        passed=True,
        status=STATUS_SELECTED,
        selected_provider=option.provider_id,
        selected_runtime=option.runtime_id,
        selected_model=option.model,
        selected_effort=option.effort,
        selected_option_id=option.option_id,
        selected_capabilities=sorted(option.capabilities),
        routing_reason=reason,
        decision_request=None,
        notes=list(notes or ()),
        eliminated=list(eliminated),
        routing_mode=routing_mode,
    )


def _fail_result(
    *,
    status: str,
    reason: str,
    eliminated: list[dict[str, str]],
    decision_request: dict[str, Any] | None = None,
    notes: Sequence[str] | None = None,
    routing_mode: str = ROUTING_MODE_DYNAMIC,
) -> RoutingResult:
    if status not in OUTCOME_STATUSES:
        raise ExecutionRoutingError(f"unknown routing status {status!r}")
    if status == STATUS_SELECTED:
        raise ExecutionRoutingError("SELECTED must use _selected_result")
    if routing_mode not in ROUTING_MODES:
        raise ExecutionRoutingError(f"unknown routing_mode {routing_mode!r}")
    return RoutingResult(
        passed=False,
        status=status,
        selected_provider=None,
        selected_runtime=None,
        selected_model=None,
        selected_effort=None,
        selected_option_id=None,
        selected_capabilities=[],
        routing_reason=reason,
        decision_request=decision_request,
        notes=list(notes or ()),
        eliminated=list(eliminated),
        routing_mode=routing_mode,
    )


def route_execution(
    request: RoutingRequest,
    *,
    catalog: ExecutionCatalog | None = None,
) -> RoutingResult:
    """Select provider/runtime/model/effort deterministically; fail closed.

    Returns SELECTED when exactly one best option wins under deterministic
    scoring. Returns AMBIGUOUS with a bounded decision_request when two or
    more options remain tied after scoring (provider not consulted here).
    Returns GAP when no certified option satisfies hard requirements.
    """
    validate_routing_request(request)
    active_catalog = catalog if catalog is not None else default_execution_catalog()

    survivors, eliminated = filter_viable_options(request, active_catalog)

    if not survivors:
        gap_notes = [
            "No certified execution option satisfied hard filters; fail closed.",
        ]
        missing_global = [
            cap
            for cap in request.required_capabilities
            if cap not in request.certified_availability.available_capabilities
        ]
        if missing_global:
            reason = (
                "capability gap: required capabilities unavailable in certified "
                f"availability: {', '.join(sorted(missing_global))}"
            )
        else:
            reason = (
                "no viable execution option after Operator, certification, "
                "task/risk/complexity, and capability filters"
            )
        return _fail_result(
            status=STATUS_GAP,
            reason=reason,
            eliminated=eliminated,
            notes=gap_notes,
        )

    ranked = sorted(survivors, key=lambda opt: score_option(opt, request))
    best = ranked[0]
    best_key = _score_without_identity(best, request)
    tied = [opt for opt in ranked if _score_without_identity(opt, request) == best_key]

    if len(tied) == 1:
        reason = (
            f"deterministic rules selected {best.option_id}: "
            f"provider={best.provider_id} model={best.model} effort={best.effort} "
            f"(task_type={request.task_type}, complexity={request.complexity}, "
            f"risk={request.risk}, cost_preference={request.cost_preference}, "
            f"latency_preference={request.latency_preference})"
        )
        return _selected_result(
            best,
            reason=reason,
            eliminated=eliminated,
            notes=["Deterministic ranking produced a unique winner."],
        )

    decision_request = build_decision_request(request, tied)
    return _fail_result(
        status=STATUS_AMBIGUOUS,
        reason=(
            f"ambiguous routing among {len(tied)} equally scored options; "
            "bounded decision_request emitted without consulting a provider"
        ),
        eliminated=eliminated,
        decision_request=decision_request,
        notes=[
            "Deterministic rules exhausted without a unique winner.",
            "decision_request is evidence-shaped only; not authority.",
            "No JEV or decision-provider import performed.",
        ],
    )


def parse_routing_request(raw: Mapping[str, Any] | RoutingRequest) -> RoutingRequest:
    """Parse a mapping into RoutingRequest. Raises on malformed input."""
    if isinstance(raw, RoutingRequest):
        return raw
    if not isinstance(raw, Mapping):
        raise ExecutionRoutingError("routing request must be a mapping")

    def _req_str(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ExecutionRoutingError(f"{key} must be a non-empty string")
        return value.strip()

    caps_raw = raw.get("required_capabilities", ())
    if caps_raw is None:
        caps_raw = ()
    if not isinstance(caps_raw, Sequence) or isinstance(caps_raw, (str, bytes)):
        raise ExecutionRoutingError("required_capabilities must be an array")
    required_capabilities = tuple(
        str(item).strip() for item in caps_raw if str(item).strip()
    )

    constraints_raw = raw.get("operator_constraints") or {}
    if not isinstance(constraints_raw, Mapping):
        raise ExecutionRoutingError("operator_constraints must be an object")

    def _opt_frozenset(key: str) -> frozenset[str] | None:
        if key not in constraints_raw or constraints_raw[key] is None:
            return None
        value = constraints_raw[key]
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ExecutionRoutingError(f"operator_constraints.{key} must be an array")
        return frozenset(str(item).strip() for item in value if str(item).strip())

    forbidden = constraints_raw.get("forbidden_capabilities") or ()
    if not isinstance(forbidden, Sequence) or isinstance(forbidden, (str, bytes)):
        raise ExecutionRoutingError(
            "operator_constraints.forbidden_capabilities must be an array"
        )

    constraints = OperatorConstraints(
        allowed_provider_ids=_opt_frozenset("allowed_provider_ids"),
        allowed_runtime_ids=_opt_frozenset("allowed_runtime_ids"),
        allowed_model_ids=_opt_frozenset("allowed_model_ids"),
        allowed_effort_levels=_opt_frozenset("allowed_effort_levels"),
        forbidden_capabilities=frozenset(
            str(item).strip() for item in forbidden if str(item).strip()
        ),
        require_certified=_strict_require_certified(
            constraints_raw.get("require_certified", True)
        ),
        max_cost_tier=constraints_raw.get("max_cost_tier"),
        min_quality_tier=constraints_raw.get("min_quality_tier"),
    )

    availability_raw = raw.get("certified_availability") or {}
    if not isinstance(availability_raw, Mapping):
        raise ExecutionRoutingError("certified_availability must be an object")

    def _fs(key: str) -> frozenset[str]:
        value = availability_raw.get(key) or ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ExecutionRoutingError(f"certified_availability.{key} must be an array")
        return frozenset(str(item).strip() for item in value if str(item).strip())

    availability = CertifiedAvailability(
        certified_providers=_fs("certified_providers"),
        certified_runtimes=_fs("certified_runtimes"),
        available_capabilities=_fs("available_capabilities"),
    )

    request_id = raw.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ExecutionRoutingError("request_id must be a non-empty string")
        request_id = request_id.strip()

    return RoutingRequest(
        task_type=_req_str("task_type"),
        complexity=_req_str("complexity"),
        risk=_req_str("risk"),
        latency_preference=_req_str("latency_preference"),
        cost_preference=_req_str("cost_preference"),
        required_capabilities=required_capabilities,
        operator_constraints=constraints,
        certified_availability=availability,
        request_id=request_id,
    )

def _strict_require_certified(value: Any) -> bool:
    """Strict boolean for require_certified.

    Missing defaults to True (fail-safe: certification still required).
    Invalid / non-boolean values also fail-safe to True — they never mean
    "treat as certified / skip certification".
    """
    if value is True:
        return True
    if value is False:
        return False
    # Missing already defaulted by caller; treat anything else as fail-safe True.
    return True


def create_routing_request(
    *,
    task_type: str,
    complexity: str,
    risk: str,
    latency_preference: str = "balanced",
    cost_preference: str = "balanced",
    required_capabilities: Sequence[str] = (),
    operator_constraints: OperatorConstraints | Mapping[str, Any] | None = None,
    certified_availability: CertifiedAvailability | Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> RoutingRequest:
    """Build a RoutingRequest when provider/model/effort selection is required.

    Execution-path callers use this instead of inventing a silent static pin.
    """
    if isinstance(operator_constraints, OperatorConstraints):
        constraints = operator_constraints
    elif operator_constraints is None:
        constraints = OperatorConstraints()
    elif isinstance(operator_constraints, Mapping):
        constraints = parse_routing_request(
            {
                "task_type": task_type,
                "complexity": complexity,
                "risk": risk,
                "latency_preference": latency_preference,
                "cost_preference": cost_preference,
                "required_capabilities": list(required_capabilities),
                "operator_constraints": operator_constraints,
                "certified_availability": (
                    certified_availability.as_dict()
                    if isinstance(certified_availability, CertifiedAvailability)
                    else (certified_availability or {})
                ),
            }
        ).operator_constraints
    else:
        raise ExecutionRoutingError("operator_constraints must be OperatorConstraints or mapping")

    if isinstance(certified_availability, CertifiedAvailability):
        availability = certified_availability
    elif certified_availability is None:
        availability = CertifiedAvailability()
    elif isinstance(certified_availability, Mapping):
        availability = parse_routing_request(
            {
                "task_type": task_type,
                "complexity": complexity,
                "risk": risk,
                "latency_preference": latency_preference,
                "cost_preference": cost_preference,
                "required_capabilities": list(required_capabilities),
                "operator_constraints": constraints.as_dict(),
                "certified_availability": certified_availability,
            }
        ).certified_availability
    else:
        raise ExecutionRoutingError(
            "certified_availability must be CertifiedAvailability or mapping"
        )

    caps = tuple(
        str(item).strip()
        for item in required_capabilities
        if isinstance(item, str) and item.strip()
    )
    request = RoutingRequest(
        task_type=task_type,
        complexity=complexity,
        risk=risk,
        latency_preference=latency_preference,
        cost_preference=cost_preference,
        required_capabilities=caps,
        operator_constraints=constraints,
        certified_availability=availability,
        request_id=request_id,
    )
    validate_routing_request(request)
    return request


def static_route_selection(
    *,
    provider_id: str,
    runtime_id: str,
    model: str,
    effort: str,
    option_id: str | None = None,
    capabilities: Sequence[str] | None = None,
    reason: str | None = None,
) -> RoutingResult:
    """Intentional static pin — telemetry marks routing_mode=static.

    Refuse inherit/default/auto/parent/empty model tokens. Never use this as a
    silent fallback when dynamic selection was required; callers that needed
    dynamic selection must call route_execution / select_execution_route instead.
    """
    provider = provider_id.strip() if isinstance(provider_id, str) else ""
    runtime = runtime_id.strip() if isinstance(runtime_id, str) else ""
    model_text = model.strip() if isinstance(model, str) else ""
    effort_text = effort.strip() if isinstance(effort, str) else ""
    if not provider or not runtime or not model_text or not effort_text:
        return _fail_result(
            status=STATUS_REFUSED,
            reason="static routing requires non-empty provider/runtime/model/effort",
            eliminated=[],
            notes=["Static routing refused malformed pin."],
            routing_mode=ROUTING_MODE_STATIC,
        )
    if model_text.lower() in FORBIDDEN_MODEL_TOKENS:
        return _fail_result(
            status=STATUS_REFUSED,
            reason=(
                f"static routing refuses model token {model!r}; "
                "explicit pinned model required (no inherit/default/auto/parent/empty)"
            ),
            eliminated=[],
            notes=["Static routing refused forbidden model token."],
            routing_mode=ROUTING_MODE_STATIC,
        )
    if effort_text not in EFFORT_LEVELS:
        return _fail_result(
            status=STATUS_REFUSED,
            reason=f"static routing effort must be one of {sorted(EFFORT_LEVELS)}",
            eliminated=[],
            notes=["Static routing refused unknown effort."],
            routing_mode=ROUTING_MODE_STATIC,
        )
    caps = sorted({str(c).strip() for c in (capabilities or ()) if str(c).strip()})
    oid = (option_id or f"static:{provider}:{runtime}:{model_text}:{effort_text}").strip()
    return RoutingResult(
        passed=True,
        status=STATUS_SELECTED,
        selected_provider=provider,
        selected_runtime=runtime,
        selected_model=model_text,
        selected_effort=effort_text,
        selected_option_id=oid,
        selected_capabilities=caps,
        routing_reason=reason
        or (
            f"intentional static routing selected provider={provider} "
            f"runtime={runtime} model={model_text} effort={effort_text}"
        ),
        decision_request=None,
        notes=["routing_mode=static", "Intentional static pin; not a dynamic fallback."],
        eliminated=[],
        routing_mode=ROUTING_MODE_STATIC,
    )


def select_execution_route(
    raw_request: Mapping[str, Any] | RoutingRequest,
    *,
    catalog: ExecutionCatalog | None = None,
    require_dynamic: bool = True,
) -> RoutingResult:
    """Execution-path entry for dynamic provider/model/effort selection.

    When ``require_dynamic`` is True (default), never silently falls back to a
    static pin — unsupported / ambiguous / gap outcomes stay fail-closed.
    """
    if not require_dynamic:
        raise ExecutionRoutingError(
            "select_execution_route is the dynamic path; for intentional static "
            "pins call static_route_selection so telemetry records routing_mode=static"
        )
    request = parse_routing_request(raw_request)
    result = route_execution(request, catalog=catalog)
    # Ensure dynamic telemetry even on failure paths.
    if result.routing_mode != ROUTING_MODE_DYNAMIC:
        result.routing_mode = ROUTING_MODE_DYNAMIC
    if "routing_mode=dynamic" not in result.notes:
        result.notes.append("routing_mode=dynamic")
    return result
