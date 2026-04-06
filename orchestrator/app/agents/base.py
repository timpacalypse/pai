from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from uuid import UUID


class AgentInput(BaseModel):
    request_id: UUID
    task: str
    role_context: dict = {}
    retrieved_context: list[str] = []
    prompt_override: str = ""


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

    async def build_prompt_with_override(self, agent_input: AgentInput) -> tuple[str, str]:
        """Build prompt, then apply any active prompt override."""
        system_prompt, user_prompt = await self.build_prompt(agent_input)
        if agent_input.prompt_override:
            system_prompt = (
                f"{system_prompt}\n\n"
                f"── Active Learning Override ──\n"
                f"{agent_input.prompt_override}\n"
                f"── End Override ──"
            )
        return system_prompt, user_prompt

    def parse_response(self, raw_response: str, agent_input: AgentInput) -> AgentOutput:
        """Parse the raw model response into structured AgentOutput."""
        import json

        # Strip markdown code fences if present
        text = raw_response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # drop opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            return AgentOutput(
                agent_name=self.name,
                result=self._extract_readable_content(data, raw_response),
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

    @staticmethod
    def _extract_readable_content(data: dict, fallback: str) -> str:
        """Extract readable text from a parsed JSON response."""
        import json

        answer = data.get("answer")

        # Best case: answer is a plain text string
        if isinstance(answer, str) and len(answer.strip()) > 10:
            return answer.strip()

        # answer is a dict/list — try to build readable text from all fields
        parts = []

        # Use answer if it's a short string
        if isinstance(answer, str) and answer.strip():
            parts.append(answer.strip())

        # Collect readable content from known result fields
        text_fields = [
            "answer", "summary", "plan_summary", "analysis",
            "synthesized_response", "methodology",
        ]
        list_fields = [
            "key_findings", "findings", "recommendations",
            "pros", "cons", "gaps", "risks",
            "sources_used", "conflicts_resolved",
        ]
        structured_list_fields = [
            "structured_findings", "subtopics", "phases",
            "dimensions", "update_methodologies",
            "autonomous_learning_methodologies",
        ]

        for field in text_fields:
            val = data.get(field)
            if isinstance(val, str) and val.strip() and field != "answer":
                parts.append(f"**{field.replace('_', ' ').title()}**: {val.strip()}")
            elif isinstance(val, dict) and field == "answer":
                # answer is a dict — recursively extract
                for k, v in val.items():
                    label = k.replace('_', ' ').title()
                    if isinstance(v, str):
                        parts.append(f"**{label}**: {v}")
                    elif isinstance(v, list):
                        items = ", ".join(str(i) for i in v)
                        parts.append(f"**{label}**: {items}")

        for field in list_fields:
            items = data.get(field)
            if isinstance(items, list) and items:
                label = field.replace('_', ' ').title()
                bullet_items = []
                for item in items:
                    if isinstance(item, str):
                        bullet_items.append(f"- {item}")
                    elif isinstance(item, dict):
                        # e.g. {"finding": "...", "subtopic": "..."}
                        text = item.get("finding") or item.get("description") or item.get("title") or str(item)
                        bullet_items.append(f"- {text}")
                if bullet_items:
                    parts.append(f"\n**{label}**:\n" + "\n".join(bullet_items))

        for field in structured_list_fields:
            items = data.get(field)
            if isinstance(items, list) and items:
                label = field.replace('_', ' ').title()
                bullet_items = []
                for item in items:
                    if isinstance(item, str):
                        bullet_items.append(f"- {item}")
                    elif isinstance(item, dict):
                        name = item.get("name") or item.get("title") or ""
                        desc = item.get("description") or item.get("finding") or ""
                        if name and desc:
                            bullet_items.append(f"- **{name}**: {desc}")
                        elif name:
                            bullet_items.append(f"- {name}")
                        elif desc:
                            bullet_items.append(f"- {desc}")
                if bullet_items:
                    parts.append(f"\n**{label}**:\n" + "\n".join(bullet_items))

        if parts:
            return "\n\n".join(parts)

        # Last resort: pretty-print the JSON
        return json.dumps(data, indent=2)
