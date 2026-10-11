"""The API contract. FROZEN.

Mirrors the response format in the participant guide section 6.1 exactly,
because the judges run identical tests against every team.

This file and store/schema.sql are the only two files shared by all four
workstreams. Change either one by agreement only, and tell everyone when you
do -- a silent edit here breaks three people at once.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

# --------------------------------------------------------------------------
# Shared value types
# --------------------------------------------------------------------------


def _ordered(v: tuple[float, float]) -> tuple[float, float]:
    low, high = v
    if low > high:
        raise ValueError(f"range is inverted: [{low}, {high}]")
    return v


# Annex A: savings and CO2e are always a low and a high value, never a point
# estimate. Serialises as a two-element JSON array, as section 6.1 shows.
Range = Annotated[tuple[float, float], AfterValidator(_ordered)]


class AnswerType(StrEnum):
    DECOMPOSITION = "decomposition"
    RECOMMENDATION = "recommendation"
    CALCULATED = "calculated"
    NOT_IN_DATA = "not_in_data"
    CLARIFICATION_NEEDED = "clarification_needed"
    REFUSED = "refused"


class Cause(StrEnum):
    """The five bridge causes, spelled as section 6.1 spells them.

    Note `product_mix`, not `mix`: the generator and the analysis code may use
    the short name internally, but the wire format does not.
    """

    TARIFF = "tariff"
    PRODUCT_MIX = "product_mix"
    VOLUME = "volume"
    WEATHER = "weather"
    WASTE = "waste"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Ease(StrEnum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"


class TableName(StrEnum):
    METER_15MIN = "meter_15min"
    TARIFF = "tariff"
    PRODUCTION = "production"
    WEATHER = "weather"


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------


class BridgeItem(BaseModel):
    cause: Cause
    pct_points: float
    range: Range
    amount: float | None = Field(
        default=None, description="Optional: the same effect in currency.")


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------


class EstimatedImpact(BaseModel):
    saving_kwh_per_year: Range
    saving_money_per_year: Range
    saving_co2e_t_per_year: Range
    payback_months: Range
    ease: Ease


class Recommendation(BaseModel):
    """What estimate_savings() returns and what /analyze emits.

    Section 6.1's sample puts the impact fields flat on the recommendation
    while the surrounding prose names `estimated_impact`. We hold one source
    of truth and emit both, so a judge's test passes whichever it looks for.
    """

    action: str
    rationale: str
    evidence_refs: list[str] = Field(
        min_length=1,
        description="At least one. A recommendation with no evidence is a "
                    "number the LLM invented.")
    confidence: Confidence
    estimated_impact: EstimatedImpact
    requires_approval: bool = True

    @computed_field
    @property
    def saving_kwh_per_year(self) -> Range:
        return self.estimated_impact.saving_kwh_per_year

    @computed_field
    @property
    def saving_money_per_year(self) -> Range:
        return self.estimated_impact.saving_money_per_year

    @computed_field
    @property
    def saving_co2e_t_per_year(self) -> Range:
        return self.estimated_impact.saving_co2e_t_per_year

    @computed_field
    @property
    def payback_months(self) -> Range:
        return self.estimated_impact.payback_months

    @computed_field
    @property
    def ease(self) -> Ease:
        return self.estimated_impact.ease


class Finding(BaseModel):
    """What detect_leaks() hands to estimate_savings().

    Internal rather than part of the wire contract, but it crosses the
    waste.py -> savings.py -> graph boundary, so it lives with the schemas.
    """

    finding_id: str
    asset: str
    kind: Literal["leak", "idle", "step_change", "schedule"]
    description: str
    kwh_per_year: float
    evidence_refs: list[str] = Field(min_length=1)
    confidence: Confidence
    detected_from: str | None = Field(
        default=None, description="Date the fault appears to start, if datable.")


# --------------------------------------------------------------------------
# POST /analyze
# --------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    question: str = Field(min_length=1)
    period: str | None = Field(default=None, examples=["2026-Q3"])
    compare_to: str | None = Field(default=None, examples=["2026-Q2"])


class AnalyzeResponse(BaseModel):
    run_id: str
    answer_type: AnswerType
    summary: str
    bridge: list[BridgeItem] = []
    recommendations: list[Recommendation] = []
    assumptions: list[str] = []
    limitations: list[str] = []
    total_pct_points: float | None = Field(
        default=None, description="The total change the bridge decomposes.")

    @model_validator(mode="after")
    def _bridge_must_add_up(self) -> AnalyzeResponse:
        """Additivity, enforced at the boundary as well as in the maths.

        The tolerance is deliberately loose. This is here to catch a dropped
        cause or a sign error, not to re-litigate rounding -- a guard that
        fires on the second decimal place during live judging is worse than
        no guard at all.
        """
        if self.bridge and self.total_pct_points is not None:
            total = sum(item.pct_points for item in self.bridge)
            if abs(total - self.total_pct_points) > 0.5:
                raise ValueError(
                    f"bridge sums to {total:.2f} pp but the total change is "
                    f"{self.total_pct_points:.2f} pp")
        return self


# --------------------------------------------------------------------------
# POST /ingest
# --------------------------------------------------------------------------


class IngestResponse(BaseModel):
    table: TableName
    rows_loaded: int
    rows_rejected: int
    status: Literal["ok", "partial", "failed"]
    dataset_id: str | None = None
    messages: list[str] = []


# --------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------


class ComponentHealth(BaseModel):
    status: Literal["ok", "degraded", "down"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    api: ComponentHealth
    data_store: ComponentHealth
    llm: ComponentHealth
    dataset_id: str | None = None
    mock_llm: bool = False


# --------------------------------------------------------------------------
# GET /trace/{run_id}
# --------------------------------------------------------------------------


class RegistryValue(BaseModel):
    """One row of the number registry.

    The writer LLM sees these and nothing else, and every numeral in the brief
    is checked back against them.
    """

    key: str
    value: float
    unit: str
    source_tool: str
    evidence_ref: str


class TraceStep(BaseModel):
    """Annex C's step shape. `check` is optional because the writer step in
    the reference trace reports a check rather than an output."""

    model_config = ConfigDict(extra="allow")

    agent: str
    tool: str
    seq: int | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    check: str | None = None
    ms: float | None = None


class TraceResponse(BaseModel):
    run_id: str
    question: str
    answer_type: AnswerType | None = None
    steps: list[TraceStep] = []
    llm_calls: int = 0
    tokens: int = 0
    latency_ms: float | None = None
    registry: list[RegistryValue] = []
