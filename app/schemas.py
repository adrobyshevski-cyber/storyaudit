import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ScanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    url: str | None = None
    text: str = Field(min_length=1, max_length=500_000)


class SourceCreate(BaseModel):
    """Low-level corpus input (internal helper, used by tests)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    type: str = Field(default="website", pattern="^website$")
    documents: list[DocumentCreate] = Field(min_length=1, max_length=100)


class WebsiteSourceCreate(BaseModel):
    """Input: a site URL; pages are fetched server-side.

    `role="competitor"` is the landscape input and requires a selection reason:
    the report must explain why this alternative was chosen.
    """
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=300)
    max_pages: int = Field(default=5, ge=1, le=20)
    role: Literal["client", "competitor"] = "client"
    selection_reason: str | None = Field(default=None, max_length=500)


class ReanalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    object_type: str
    object_id: uuid.UUID
    issue_id: uuid.UUID
    client_request_id: uuid.UUID


class ReviewActor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class RegisterPatch(ReviewActor):
    changes: dict[str, Any] = Field(min_length=1)


class ClientBriefUpdate(ReviewActor):
    brief: dict[str, Any]


class OpenQuestionCreate(ReviewActor):
    question: str = Field(min_length=1, max_length=2000)
    target: Literal["client", "analyst"] = "client"


class OpenQuestionPatch(ReviewActor):
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    target: Literal["client", "analyst"] | None = None
    status: Literal["open", "answered", "dropped"] | None = None


class ReviewAction(ReviewActor):
    action: Literal["start", "approve", "reject", "reopen", "needs_review"]


class StageGateDecision(ReviewActor):
    decision: Literal["ready", "conditional_ready", "not_ready"]
    rationale: str = Field(min_length=1, max_length=4000)
    blockers: list[str] = Field(default_factory=list, max_length=20)
    required_validations: list[str] = Field(default_factory=list, max_length=20)


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)


class A1Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    propositions: list["A1Proposition"]


class A1Proposition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_id: uuid.UUID
    text: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class A2Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer: Label
    problem: Label
    object: Label
    promise: Label


class Conflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    present: bool
    confidence: float = Field(ge=0, le=1)
    description: str = Field(max_length=1000)


class A3Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hero: Label
    brand_role: Label
    conflict: Conflict


class A4Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    higher_order_meanings: list[Label] = Field(max_length=5)
    before: Label
    after: Label


# Batch variants: one provider call annotates a whole chunk of propositions.
# The per-proposition payload stays identical, so profiles, metrics and the report
# do not depend on the batching.


class A2Item(A2Result):
    proposition_id: uuid.UUID


class A3Item(A3Result):
    proposition_id: uuid.UUID


class A4Item(A4Result):
    proposition_id: uuid.UUID


class A2BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[A2Item] = Field(min_length=1, max_length=200)


class A3BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[A3Item] = Field(min_length=1, max_length=200)


class A4BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[A4Item] = Field(min_length=1, max_length=200)


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strength: float = Field(ge=0, le=1)
    evidence_proposition_ids: list[uuid.UUID] = Field(max_length=5)


class A6Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: dict[str, Feature]


A1Result.model_rebuild()


class ProductItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    audience: str = Field(default="не определена", max_length=500)
    trigger_situation: str | None = Field(default=None, max_length=1000)
    promise: str | None = Field(default=None, max_length=1000)
    format: str | None = Field(default=None, max_length=200)
    price: str | None = Field(default=None, max_length=200)
    procedure: str | None = Field(default=None, max_length=1000)
    proof: str | None = Field(default=None, max_length=1000)
    boundary: str | None = Field(default=None, max_length=1000)
    status: Literal["current", "secondary", "legacy", "future", "unknown"] = "current"
    is_primary: bool = False
    source_codes: list[str] = Field(default_factory=list, max_length=10)


class ProductsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: list[ProductItem] = Field(default_factory=list, max_length=20)


class ClaimItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=1000)
    relevance: Literal["high", "medium", "low"] = "medium"
    confidence: Literal["high", "medium", "low"] = "medium"
    source_codes: list[str] = Field(default_factory=list, max_length=5)


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ClaimItem] = Field(default_factory=list, max_length=50)


class LanguageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=300)
    brand: str | None = Field(default=None, max_length=1000)
    professional: str | None = Field(default=None, max_length=1000)
    lived: str | None = Field(default=None, max_length=1000)
    match_status: Literal["match", "partial", "gap", "no_data"] = "no_data"
    is_illustration: bool = False
    source_codes: list[str] = Field(default_factory=list, max_length=5)


class LanguageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codes: list[LanguageItem] = Field(default_factory=list, max_length=20)


class ActantRoleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["subject", "object", "sender", "receiver", "helper", "opponent"]
    object_level: Literal["functional", "emotional", "transformational"] | None = None
    reconstruction: str = Field(min_length=1, max_length=1000)
    status: Literal["F", "I", "H", "U"]
    alternative: str | None = Field(default=None, max_length=1000)
    source_codes: list[str] = Field(default_factory=list, max_length=5)
    evidence_codes: list[str] = Field(default_factory=list, max_length=5)


class ActantMapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narrative_arc: str | None = Field(default=None, max_length=1000)
    roles: list[ActantRoleItem] = Field(default_factory=list, max_length=12)


class JourneyItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Literal["trigger", "interpretation", "search", "comparison", "trust",
                  "decision", "experience", "referral"]
    public_state: str = Field(default="нет данных", max_length=1000)
    obstacle: str | None = Field(default=None, max_length=1000)
    check_method: str | None = Field(default=None, max_length=500)
    source_codes: list[str] = Field(default_factory=list, max_length=5)
    is_observed: bool = False


class JourneyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[JourneyItem] = Field(default_factory=list, max_length=8)


class ConsistencyItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actant_role: Literal["subject", "opponent", "object", "helper"]
    dimension: Literal["communication", "service", "experience", "evidence"]
    status: Literal["confirmed", "partial", "conflict", "no_data"]
    note: str | None = Field(default=None, max_length=500)
    source_codes: list[str] = Field(default_factory=list, max_length=5)
    counterexample: str | None = Field(default=None, max_length=1000)


class ConsistencyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cells: list[ConsistencyItem] = Field(default_factory=list, max_length=16)


class FindingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["gap", "missing_link", "weak_evidence"]
    statement: str = Field(min_length=1, max_length=1000)
    basis_codes: list[str] = Field(min_length=1, max_length=10)
    consequence: str | None = Field(default=None, max_length=1000)
    alternative_explanation: str = Field(min_length=10, max_length=1000)
    verification: str | None = Field(default=None, max_length=1000)
    priority: Literal["P1", "P2", "P3", "blocker"] = "P2"
    confidence: Literal["high", "medium", "low"] = "medium"


class FindingsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[FindingItem] = Field(default_factory=list, max_length=12)


class CriticItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=2, max_length=12)
    alternative_explanation: str = Field(min_length=10, max_length=1000)
    needs_review: bool = False
    note: str | None = Field(default=None, max_length=1000)


class CriticResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CriticItem] = Field(default_factory=list, max_length=20)


class ScorecardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: Literal["clarity", "consistency", "credibility", "resonance"]
    criterion: str = Field(min_length=1, max_length=200)
    score: float | None = Field(default=None, ge=0, le=10)
    na_reason: str | None = Field(default=None, max_length=500)
    confidence: Literal["high", "medium", "low"] | None = None
    rationale: str = Field(min_length=1, max_length=1000)
    source_codes: list[str] = Field(default_factory=list, max_length=5)


class ScorecardResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ScorecardItem] = Field(default_factory=list, max_length=20)


class KpiPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    definition: str = Field(min_length=1, max_length=1000)
    baseline: str = Field(default="unknown", max_length=200)
    period: str | None = Field(default=None, max_length=100)
    data_source: str | None = Field(default=None, max_length=300)
    owner: str | None = Field(default=None, max_length=200)
    target: str = Field(default="определить после baseline", max_length=300)
    limitations: str | None = Field(default=None, max_length=1000)


class RecommendationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change: str = Field(min_length=1, max_length=1000)
    rationale: str = Field(min_length=1, max_length=1000)
    expected_effect: str | None = Field(default=None, max_length=1000)
    impact: Literal["high", "medium", "low"] = "medium"
    evidence_confidence: Literal["high", "medium", "low"] = "medium"
    urgency: Literal["high", "medium", "low"] = "medium"
    effort: Literal["high", "medium", "low"] = "medium"
    priority: Literal["P1", "P2", "P3", "blocker"] = "P2"
    decision_class: Literal["keep", "fix", "test", "stop"] = "fix"
    owner: str | None = Field(default=None, max_length=200)
    finding_codes: list[str] = Field(default_factory=list, max_length=5)
    kpi: KpiPlan


class RoadmapItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    dependencies: str | None = Field(default=None, max_length=500)
    actions: str | None = Field(default=None, max_length=1000)
    artifact: str | None = Field(default=None, max_length=500)
    owner: str | None = Field(default=None, max_length=200)
    done_criteria: str | None = Field(default=None, max_length=500)


class GateCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str = Field(min_length=1, max_length=200)
    result: str = Field(min_length=1, max_length=100)
    required: str = Field(default="", max_length=500)


class GatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["ready", "conditional_ready", "not_ready"]
    rationale: str = Field(min_length=1, max_length=2000)
    criteria: list[GateCriterion] = Field(default_factory=list, max_length=10)
    blockers: list[str] = Field(default_factory=list, max_length=10)
    required_validations: list[str] = Field(default_factory=list, max_length=10)


class HypothesisPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=1000)
    how_to_verify: str = Field(min_length=1, max_length=1000)


class PlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recommendations: list[RecommendationItem] = Field(default_factory=list, max_length=12)
    roadmap: list[RoadmapItem] = Field(default_factory=list, max_length=10)
    gate: GatePlan
    hypotheses: list[HypothesisPlan] = Field(default_factory=list, max_length=8)


class NarrativeParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    status: Literal["F", "I", "H", "R", "U"] | None = None
    refs: list[str] = Field(default_factory=list, max_length=10)


class NarrativeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: dict[str, list[NarrativeParagraph]]


class LandscapeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    source_code: str = Field(min_length=2, max_length=12)
    audience: str | None = Field(default=None, max_length=500)
    opponent: str | None = Field(default=None, max_length=1000)
    object: str | None = Field(default=None, max_length=1000)
    helper_role: str | None = Field(default=None, max_length=1000)
    promise: str | None = Field(default=None, max_length=1000)
    proof: str | None = Field(default=None, max_length=1000)
    price: str | None = Field(default=None, max_length=200)
    inclusion_reason: str = Field(min_length=1, max_length=500)
    geographic_scope: str | None = Field(default=None, max_length=300)
    territory_notes: str | None = Field(default=None, max_length=1000)


class LandscapeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[LandscapeItem] = Field(default_factory=list, max_length=5)
