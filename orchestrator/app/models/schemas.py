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
