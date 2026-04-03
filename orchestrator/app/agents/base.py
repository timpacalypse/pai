from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from uuid import UUID


class AgentInput(BaseModel):
    request_id: UUID
    task: str
    role_context: dict = {}
    retrieved_context: list[str] = []


class AgentOutput(BaseModel):
    agent_name: str
    result: str
    reasoning: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    metadata: dict = {}


class BaseAgent(ABC):
    """
    Base class for all PAI agents.

    Agents are specialized reasoning units — not autonomous systems.
    They do NOT call models or tools directly. The orchestrator mediates
    all external interactions and passes results back in.
    """

    name: str = "base_agent"

    @abstractmethod
    async def build_prompt(self, agent_input: AgentInput) -> tuple[str, str]:
        """
        Build the (system_prompt, user_prompt) for this agent's task.
        The orchestrator will call the model on the agent's behalf.
        """
        ...

    def parse_response(self, raw_response: str, agent_input: AgentInput) -> AgentOutput:
        """Parse the raw model response into structured AgentOutput."""
        import json
        try:
            data = json.loads(raw_response)
            return AgentOutput(
                agent_name=self.name,
                result=str(data.get("answer", raw_response)),
                reasoning=str(data.get("reasoning", "")),
                confidence=float(data.get("confidence", 0.5)),
                metadata=data,
            )
        except (json.JSONDecodeError, ValueError):
            return AgentOutput(
                agent_name=self.name,
                result=raw_response,
                reasoning="",
                confidence=0.3,
            )
