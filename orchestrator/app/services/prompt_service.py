from app.models.schemas import RoleContext, ResolvedRoles


def _role_block(label: str, role: RoleContext) -> str:
    """Format a single role's context into a prompt block."""
    lines = [
        f"[{label}]",
        f"  Role: {role.role.value}",
        f"  Domain: {role.domain.value}",
    ]
    if role.description:
        lines.append(f"  Description: {role.description}")
    if role.goals:
        lines.append(f"  Goals: {', '.join(role.goals)}")
    if role.preferences:
        lines.append(f"  Preferences: {', '.join(role.preferences)}")
    if role.constraints:
        lines.append(f"  Constraints: {', '.join(role.constraints)}")
    return "\n".join(lines)


def build_system_prompt(roles: ResolvedRoles) -> str:
    """Build a role-aware system prompt that enforces structured JSON output."""
    sections = [
        "You are PAI — a Personal AI assistant.",
        "",
        _role_block("Primary Role", roles.primary),
    ]

    if roles.secondary:
        sections.append("")
        sections.append(_role_block("Secondary Role", roles.secondary))
        sections.append("")
        sections.append(
            "The primary role drives tone and structure. "
            "The secondary role provides supplementary perspective and depth. "
            "Blend both where it adds value, but defer to the primary role on conflicts."
        )

    sections.extend([
        "",
        "Rules:",
        "- Adapt your tone, depth, and recommendations to the active role(s).",
        "- Always respond with valid JSON matching this schema:",
        '  {"answer": "<your response>", "reasoning": "<brief reasoning>", "confidence": <0.0-1.0>}',
        "- Do not include any text outside the JSON object.",
    ])

    return "\n".join(sections)


def build_chat_prompt(roles: ResolvedRoles) -> str:
    """Build a conversational system prompt — plain language, no JSON."""
    sections = [
        "You are PAI — a Personal AI assistant having a conversation.",
        "",
        _role_block("Primary Role", roles.primary),
    ]

    if roles.secondary:
        sections.append("")
        sections.append(_role_block("Secondary Role", roles.secondary))

    sections.extend([
        "",
        "Rules:",
        "- Respond in natural, conversational language. Do NOT output JSON.",
        "- Be helpful, concise, and direct.",
        "- If you do not know the answer, say so honestly. Never invent facts, URLs, recipes, or data.",
        "- If context is provided, use only what is relevant. Ignore context that does not relate to the question.",
        "- If the user asks about a specific source (website, book, etc.) and you don't have that information, say you don't have access to it rather than guessing.",
        "- Adapt your tone and depth to the active role(s).",
    ])

    return "\n".join(sections)
