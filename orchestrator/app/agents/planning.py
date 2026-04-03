from app.agents.base import BaseAgent, AgentInput


class PlanningAgent(BaseAgent):
    """
    Planning agent — produces structured roadmaps, plans, and strategies
    with phases, milestones, and dependencies.
    """

    name = "planning_agent"

    async def build_prompt(self, agent_input: AgentInput) -> tuple[str, str]:
        role_info = ""
        if agent_input.role_context:
            role_info = (
                f"\nActive role: {agent_input.role_context.get('role', 'general')}\n"
                f"Goals: {', '.join(agent_input.role_context.get('goals', []))}\n"
                f"Preferences: {', '.join(agent_input.role_context.get('preferences', []))}\n"
                f"Constraints: {', '.join(agent_input.role_context.get('constraints', []))}\n"
            )

        context_block = ""
        if agent_input.retrieved_context:
            context_block = (
                "\n\nRelevant retrieved context:\n"
                + "\n---\n".join(agent_input.retrieved_context)
                + "\n\nUse the above context to inform your planning where relevant."
            )

        system_prompt = (
            "You are a planning agent within the PAI system.\n"
            "Your job is to create structured, actionable plans and roadmaps.\n"
            f"{role_info}\n"
            "Rules:\n"
            "- Break plans into clear phases with milestones.\n"
            "- Identify dependencies and risks.\n"
            "- Prioritize based on role goals and constraints.\n"
            "- Respond with valid JSON matching this schema:\n"
            "  {\n"
            '    "answer": "<plan summary>",\n'
            '    "phases": [\n'
            "      {\n"
            '        "name": "<phase name>",\n'
            '        "objectives": ["<objective>", ...],\n'
            '        "milestones": ["<milestone>", ...],\n'
            '        "dependencies": ["<dependency>", ...]\n'
            "      }\n"
            "    ],\n"
            '    "risks": ["<risk 1>", ...],\n'
            '    "reasoning": "<planning rationale>",\n'
            '    "confidence": <0.0-1.0>\n'
            "  }\n"
            "- Do not include any text outside the JSON object.\n"
        )

        user_prompt = f"Create a plan for:\n\n{agent_input.task}{context_block}"

        return system_prompt, user_prompt
