from app.agents.base import BaseAgent, AgentInput


class AnalysisAgent(BaseAgent):
    """
    Analysis agent — performs structured comparative analysis on a topic.
    Produces pros/cons, tradeoffs, and recommendations.
    """

    name = "analysis_agent"

    async def build_prompt(self, agent_input: AgentInput) -> tuple[str, str]:
        role_info = ""
        if agent_input.role_context:
            role_info = (
                f"\nActive role: {agent_input.role_context.get('role', 'general')}\n"
                f"Goals: {', '.join(agent_input.role_context.get('goals', []))}\n"
                f"Constraints: {', '.join(agent_input.role_context.get('constraints', []))}\n"
            )

        context_block = ""
        if agent_input.retrieved_context:
            context_block = (
                "\n\nRelevant retrieved context:\n"
                + "\n---\n".join(agent_input.retrieved_context)
                + "\n\nUse the above context to inform your analysis where relevant."
            )

        system_prompt = (
            "You are an analysis agent within the PAI system.\n"
            "Your job is to perform deep, structured analysis on the given topic.\n"
            f"{role_info}\n"
            "Rules:\n"
            "- Identify key dimensions for comparison.\n"
            "- Present pros, cons, and tradeoffs clearly.\n"
            "- Provide actionable recommendations.\n"
            "- Respond with valid JSON matching this schema:\n"
            "  {\n"
            '    "answer": "<structured analysis>",\n'
            '    "dimensions": ["<dimension 1>", "<dimension 2>", ...],\n'
            '    "pros": ["<pro 1>", ...],\n'
            '    "cons": ["<con 1>", ...],\n'
            '    "recommendations": ["<recommendation 1>", ...],\n'
            '    "reasoning": "<analytical methodology>",\n'
            '    "confidence": <0.0-1.0>\n'
            "  }\n"
            "- Do not include any text outside the JSON object.\n"
        )

        user_prompt = f"Analyze the following:\n\n{agent_input.task}{context_block}"

        return system_prompt, user_prompt
