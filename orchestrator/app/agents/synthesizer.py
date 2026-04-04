from app.agents.base import BaseAgent, AgentInput


class SynthesizerAgent(BaseAgent):
    """
    Synthesizer agent — combines outputs from multiple agents into a
    unified, coherent response that captures the best of each.
    """

    name = "synthesizer_agent"

    async def build_prompt(self, agent_input: AgentInput) -> tuple[str, str]:
        role_info = ""
        if agent_input.role_context:
            role_info = (
                f"\nActive role: {agent_input.role_context.get('role', 'general')}\n"
                f"Preferences: {', '.join(agent_input.role_context.get('preferences', []))}\n"
            )

        system_prompt = (
            "You are a synthesizer agent within the PAI system.\n"
            "Your job is to combine multiple agent outputs into a single, unified response.\n"
            f"{role_info}\n"
            "Rules:\n"
            "- Merge the strongest elements from each input.\n"
            "- Resolve conflicts by choosing the most well-supported position.\n"
            "- Produce a coherent, non-redundant output.\n"
            "- Note which sources contributed to each finding.\n"
            "- Respond with valid JSON matching this schema:\n"
            "  {\n"
            '    "answer": "<PLAIN TEXT string with your complete synthesized response written in readable paragraphs>",\n'
            '    "sources_used": ["<agent_name 1>", ...],\n'
            '    "conflicts_resolved": ["<conflict>", ...],\n'
            '    "reasoning": "<synthesis approach>",\n'
            '    "confidence": <0.0-1.0>\n'
            "  }\n"
            "- IMPORTANT: The 'answer' field MUST be a single plain text string (not a dict or list).\n"
            "  Write it as a readable, well-structured response with full sentences.\n"
            "- Do not include any text outside the JSON object.\n"
        )

        agent_outputs = "\n\n---\n\n".join(agent_input.retrieved_context)
        user_prompt = (
            f"Original task: {agent_input.task}\n\n"
            f"Agent outputs to synthesize:\n\n{agent_outputs}"
        )

        return system_prompt, user_prompt
