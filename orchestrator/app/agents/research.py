from app.agents.base import BaseAgent, AgentInput


class ResearchAgent(BaseAgent):
    """
    Research agent — investigates topics and produces structured findings.
    Orchestrator calls the model on its behalf.
    """

    name = "research_agent"

    async def build_prompt(self, agent_input: AgentInput) -> tuple[str, str]:
        role_info = ""
        if agent_input.role_context:
            role_info = (
                f"\nActive role: {agent_input.role_context.get('role', 'general')}\n"
                f"Goals: {', '.join(agent_input.role_context.get('goals', []))}\n"
                f"Preferences: {', '.join(agent_input.role_context.get('preferences', []))}\n"
            )

        context_block = ""
        if agent_input.retrieved_context:
            context_block = (
                "\n\nRelevant retrieved context:\n"
                + "\n---\n".join(agent_input.retrieved_context)
                + "\n\nUse the above context to inform your research where relevant."
            )

        system_prompt = (
            "You are a research agent within the PAI system.\n"
            "Your job is to investigate the given topic thoroughly and produce structured findings.\n"
            f"{role_info}\n"
            "Rules:\n"
            "- Be thorough but concise.\n"
            "- Organize findings by subtopic.\n"
            "- Clearly distinguish facts from analysis.\n"
            "- Respond with valid JSON matching this schema:\n"
            "  {\n"
            '    "answer": "<structured research findings>",\n'
            '    "key_findings": ["<finding 1>", "<finding 2>", ...],\n'
            '    "reasoning": "<research methodology and approach>",\n'
            '    "confidence": <0.0-1.0>,\n'
            '    "gaps": ["<area needing further research>", ...]\n'
            "  }\n"
            "- Do not include any text outside the JSON object.\n"
        )

        user_prompt = f"Research the following topic:\n\n{agent_input.task}{context_block}"

        return system_prompt, user_prompt
