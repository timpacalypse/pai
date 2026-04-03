import logging
from enum import Enum

from app.services.intent_service import IntentType

logger = logging.getLogger("pai.workflow")


class WorkflowType(str, Enum):
    direct_response = "direct_response"
    retrieval_augmented = "retrieval_augmented"
    agent_research = "agent_research"
    agent_planning = "agent_planning"
    execution = "execution"


# Intent → Workflow mapping (config-driven, easily extensible)
_INTENT_WORKFLOW_MAP: dict[IntentType, WorkflowType] = {
    IntentType.question: WorkflowType.direct_response,
    IntentType.conversation: WorkflowType.direct_response,
    IntentType.creative: WorkflowType.direct_response,
    IntentType.analysis: WorkflowType.retrieval_augmented,
    IntentType.research: WorkflowType.agent_research,
    IntentType.planning: WorkflowType.agent_planning,
    IntentType.execution: WorkflowType.execution,
}


def route_workflow(intent: IntentType) -> WorkflowType:
    """Map an intent to the appropriate workflow type."""
    workflow = _INTENT_WORKFLOW_MAP.get(intent, WorkflowType.direct_response)
    logger.info("workflow_routed", extra={"intent": intent.value, "workflow": workflow.value})
    return workflow
