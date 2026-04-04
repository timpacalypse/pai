from app.agents.base import BaseAgent, AgentInput, AgentOutput


class CriticAgent(BaseAgent):
    """
    Critic / Red Team agent — reviews another agent's output and identifies
    weaknesses, gaps, risks, and areas for improvement.
    """

    name = "critic_agent"

    async def build_prompt(self, agent_input: AgentInput) -> tuple[str, str]:
        system_prompt = (
            "You are a critic / red team agent within the PAI system.\n"
            "Your job is to critically evaluate a piece of work and identify weaknesses.\n"
            "\n"
            "Rules:\n"
            "- Be constructive but thorough.\n"
            "- Identify logical gaps, unsupported claims, and missing perspectives.\n"
            "- Assess risks and failure modes.\n"
            "- Suggest specific improvements.\n"
            "- Respond with valid JSON matching this schema:\n"
            "  {\n"
            '    "answer": "<PLAIN TEXT string with your overall critique summary written in readable paragraphs>",\n'
            '    "strengths": ["<strength 1>", ...],\n'
            '    "weaknesses": ["<weakness 1>", ...],\n'
            '    "risks": ["<risk 1>", ...],\n'
            '    "improvements": ["<improvement 1>", ...],\n'
            '    "reasoning": "<critique methodology>",\n'
            '    "confidence": <0.0-1.0>\n'
            "  }\n"
            "- IMPORTANT: The 'answer' field MUST be a single plain text string (not a dict or list).\n"
            "  Write it as a readable, well-structured response with full sentences.\n"
            "- Do not include any text outside the JSON object.\n"
        )

        user_prompt = f"Critically evaluate the following:\n\n{agent_input.task}"

        # If there's retrieved context (the output being critiqued), include it
        if agent_input.retrieved_context:
            user_prompt += (
                "\n\nOutputs to critique:\n"
                + "\n---\n".join(agent_input.retrieved_context)
            )

        return system_prompt, user_prompt
