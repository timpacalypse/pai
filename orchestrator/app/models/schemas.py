from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from datetime import datetime
from enum import Enum


class DomainType(str, Enum):
    professional = "professional"
    personal = "personal"
    family = "family"
    intellectual_growth = "intellectual_growth"


class RoleType(str, Enum):
    # Professional
    cybersecurity_executive = "cybersecurity_executive"
    ai_cybersecurity_strategist = "ai_cybersecurity_strategist"
    ai_governance_practitioner = "ai_governance_practitioner"
    educator_scholar = "educator_scholar"
    solutions_architect = "solutions_architect"
    proposal_strategist = "proposal_strategist"
    # Personal
    fitness_longevity_optimist = "fitness_longevity_optimist"
    aesthetics_focused_builder = "aesthetics_focused_builder"
    family_chef = "family_chef"
    # Family
    family_activity_coordinator = "family_activity_coordinator"
    parent = "parent"
    # Intellectual Growth
    polymath_in_training = "polymath_in_training"


ROLE_DOMAIN_MAP: dict[RoleType, DomainType] = {
    RoleType.cybersecurity_executive: DomainType.professional,
    RoleType.ai_cybersecurity_strategist: DomainType.professional,
    RoleType.ai_governance_practitioner: DomainType.professional,
    RoleType.educator_scholar: DomainType.professional,
    RoleType.solutions_architect: DomainType.professional,
    RoleType.proposal_strategist: DomainType.professional,
    RoleType.fitness_longevity_optimist: DomainType.personal,
    RoleType.aesthetics_focused_builder: DomainType.personal,
    RoleType.family_chef: DomainType.personal,
    RoleType.family_activity_coordinator: DomainType.family,
    RoleType.parent: DomainType.family,
    RoleType.polymath_in_training: DomainType.intellectual_growth,
}


class RoleContext(BaseModel):
    role: RoleType
    domain: DomainType
    description: str = ""
    goals: list[str] = []
    preferences: list[str] = []
    constraints: list[str] = []


class ResolvedRoles(BaseModel):
    primary: RoleContext
    secondary: RoleContext | None = None


class TaskRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=10000)
    role: RoleType | None = None
    secondary_role: RoleType | None = None
    context: dict | None = None
    request_id: UUID = Field(default_factory=uuid4)


class CompetitionRequest(TaskRequest):
    agents: list[str] = Field(default=["research", "analysis"], min_length=2)
    strategy: str = "best_score"  # best_score | weighted | synthesize


class WebResearchRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=500)
    max_results: int = Field(default=10, ge=1, le=30)
    time_filter: str = Field(default="m", pattern=r"^[dwmy]$")
    auto_ingest: bool = True
    min_score: float = Field(default=0.2, ge=0.0, le=1.0)
    role: RoleType | None = None
    request_id: UUID = Field(default_factory=uuid4)


class RankedArticle(BaseModel):
    title: str
    url: str
    snippet: str
    body_preview: str = ""
    source: str = ""
    score: dict = {}


class WebResearchResponse(BaseModel):
    request_id: UUID
    topic: str
    articles: list[RankedArticle]
    total_found: int
    ingested_count: int = 0
    duration_ms: float


class ChatMessage(BaseModel):
    role_name: str = Field(default="user")
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)
    role: RoleType | None = None
    secondary_role: RoleType | None = None
    conversation_id: UUID = Field(default_factory=uuid4)
    history: list[ChatMessage] = []
    request_id: UUID = Field(default_factory=uuid4)
    user_id: int | None = None


class ChatResponse(BaseModel):
    request_id: UUID
    conversation_id: UUID
    role: str
    secondary_role: str | None = None
    domain: str
    content: str
    intent: str = "conversation"
    duration_ms: float


class OrchestratorDecision(BaseModel):
    request_id: UUID
    roles: ResolvedRoles
    model: str
    workflow: str = "direct_response"


class TaskResponse(BaseModel):
    request_id: UUID
    role: str
    secondary_role: str | None = None
    domain: str
    model: str
    content: str
    structured_output: dict | None = None
    workflow: str = "direct_response"
    intent: str = "question"
    duration_ms: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ── Meal Planning Schemas ───────────────────────────────────────


class FamilyMemberRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    age_group: str = Field(default="adult", pattern=r"^(toddler|child|teen|adult)$")
    dietary_restrictions: list[str] = []
    notes: str = ""


class PreferenceRequest(BaseModel):
    family_member_id: int
    item: str = Field(..., min_length=1, max_length=255)
    item_type: str = Field(default="dish", pattern=r"^(dish|ingredient|cuisine|cooking_method)$")
    sentiment: str = Field(..., pattern=r"^(love|like|neutral|dislike|hate|allergy)$")
    notes: str = ""


class MealRatingRequest(BaseModel):
    meal_name: str = Field(..., min_length=1, max_length=255)
    family_member_id: int
    rating: int = Field(..., ge=1, le=5)
    would_repeat: bool = True
    meal_plan_id: int | None = None
    day_of_week: str = ""
    notes: str = ""


class MealPlanRequest(BaseModel):
    week_label: str | None = None
    extra_instructions: str = ""


# ── Home Knowledge Base Schemas ─────────────────────────────────


class HomeTellRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class HomeItemRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    category: str = Field(default="general", pattern=r"^(appliance|hvac|plumbing|electrical|outdoor|vehicle|general)$")
    location: str = ""
    brand: str = ""
    model_info: str = ""
    notes: str = ""


class HomeTaskCompleteRequest(BaseModel):
    task_id: int
    notes: str = ""
    cost: float = 0.0


class HomeDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    doc_type: str = Field(default="manual", pattern=r"^(manual|warranty|receipt|notes|reference)$")
    home_item_id: int | None = None
    source: str = ""


# ── Document Ingestion Schemas ──────────────────────────────────


class IngestURLRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)


class IngestTextRequest(BaseModel):
    text: str = Field(..., min_length=1)
    title: str = ""
    source: str = ""


# ── Medical History Schemas ─────────────────────────────────────


class MedicalTellRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class MedicalRecordRequest(BaseModel):
    family_member_id: int
    date: str = ""  # YYYY-MM-DD
    category: str = Field(
        default="other",
        pattern=r"^(checkup|dental|vision|specialist|emergency|lab|vaccination|prescription|surgery|mental_health|other)$",
    )
    provider: str = ""
    summary: str = Field(..., min_length=1)
    details: str = ""
    follow_up: str = ""
    medications: list[str] = []


# ── Recipe Schemas ──────────────────────────────────────────────


class RecipeRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    ingredients: list[str] = []
    instructions: list[str] = []
    source: str = ""
    source_url: str = ""
    cuisine: str = ""
    prep_time_min: int = 0
    cook_time_min: int = 0
    servings: int = 0
    tags: list[str] = []
    notes: str = ""


class RecipeRateRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)


# ── Calendar Schemas ────────────────────────────────────────────


class CalendarTellRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class CalendarEventRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    event_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    event_time: str = ""
    end_time: str = ""
    category: str = Field(
        default="other",
        pattern=r"^(birthday|appointment|school|activity|holiday|travel|deadline|reminder|other)$",
    )
    family_member_name: str = "family"
    location: str = ""
    recurrence: str = Field(default="none", pattern=r"^(none|weekly|monthly|yearly)$")
    notes: str = ""


# ── Process Engine ──────────────────────────────────────────────


class ProcessStepDef(BaseModel):
    id: str
    type: str = Field(..., pattern=r"^(skill|agent|decision|gate)$")
    name: str = ""
    skill_id: str | None = None
    agent: str | None = None
    inputs: dict = {}
    outputs: list[str] = []
    parallel_group: str | None = None
    condition: str | None = None          # decision steps
    branches: dict | None = None          # decision: {"true": "step_id", "false": "step_id"}
    gate_message: str | None = None       # gate steps


class ProcessDefinitionCreate(BaseModel):
    process_id: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    roles: list[str] = []
    trigger_config: dict = {}
    steps: list[ProcessStepDef] = []


class ProcessDefinitionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    roles: list[str] | None = None
    trigger_config: dict | None = None
    steps: list[ProcessStepDef] | None = None
    is_active: bool | None = None


class ProcessStartRequest(BaseModel):
    process_id: str = Field(..., min_length=1)
    role: str | None = None
    params: dict = {}


class GateResponse(BaseModel):
    decision: str = Field(..., pattern=r"^(approve|reject|modify)$")
    message: str = ""
    modifications: dict = {}


# ── Workout Schemas ─────────────────────────────────────────


class WorkoutTellRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class WorkoutLogRequest(BaseModel):
    activity: str = Field(..., min_length=1, max_length=100)
    duration_minutes: int = Field(default=0, ge=0)
    notes: str = ""
    metrics: dict = {}
