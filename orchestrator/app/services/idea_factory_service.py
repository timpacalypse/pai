"""Idea Factory — capture, challenge, and evolve ideas with LLM assistance."""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.idea_factory")

STAGES = ("spark", "exploring", "validating", "building", "shipped", "killed")


# ── Core CRUD ──────────────────────────────────────────────────────────────────

async def capture_idea(title: str, description: str = "", tags: list[str] | None = None) -> dict:
    """Capture a new idea into the factory."""
    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO ideas (title, description, tags)
            VALUES (:title, :desc, :tags)
            RETURNING id, title, stage, created_at
        """), {"title": title, "desc": description, "tags": tags or []})
        await session.commit()
        row = r.mappings().fetchone()
        return dict(row)


async def list_ideas(stage: str | None = None, limit: int = 20) -> list[dict]:
    """List ideas, optionally filtered by stage."""
    async with async_session() as session:
        if stage:
            r = await session.execute(text("""
                SELECT id, title, description, stage, tags, created_at, updated_at
                FROM ideas WHERE stage = :stage
                ORDER BY updated_at DESC LIMIT :lim
            """), {"stage": stage, "lim": limit})
        else:
            r = await session.execute(text("""
                SELECT id, title, description, stage, tags, created_at, updated_at
                FROM ideas WHERE stage NOT IN ('shipped', 'killed')
                ORDER BY updated_at DESC LIMIT :lim
            """), {"lim": limit})
        return [dict(row) for row in r.mappings().fetchall()]


async def get_idea(idea_id: int) -> dict | None:
    """Get a single idea by ID."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT id, title, description, stage, tags, challenge_output, created_at, updated_at
            FROM ideas WHERE id = :id
        """), {"id": idea_id})
        row = r.mappings().fetchone()
        return dict(row) if row else None


async def advance_idea(idea_id: int, new_stage: str) -> dict | None:
    """Move an idea to a new stage."""
    if new_stage not in STAGES:
        return None
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE ideas SET stage = :stage, updated_at = NOW()
            WHERE id = :id RETURNING id, title, stage
        """), {"stage": new_stage, "id": idea_id})
        await session.commit()
        row = r.mappings().fetchone()
        if row:
            await _log_interaction(idea_id, "advance", f"Moved to {new_stage}", session)
            await session.commit()
        return dict(row) if row else None


async def kill_idea(idea_id: int) -> dict | None:
    """Kill an idea (mark as killed)."""
    return await advance_idea(idea_id, "killed")


async def _log_interaction(idea_id: int, itype: str, content: str, session=None):
    """Log an interaction against an idea."""
    if session:
        await session.execute(text("""
            INSERT INTO idea_interactions (idea_id, interaction_type, content)
            VALUES (:idea_id, :itype, :content)
        """), {"idea_id": idea_id, "itype": itype, "content": content})
    else:
        async with async_session() as s:
            await s.execute(text("""
                INSERT INTO idea_interactions (idea_id, interaction_type, content)
                VALUES (:idea_id, :itype, :content)
            """), {"idea_id": idea_id, "itype": itype, "content": content})
            await s.commit()


# ── Challenge Mode ─────────────────────────────────────────────────────────────

async def challenge_idea(idea_text: str, idea_id: int | None = None) -> str:
    """Run an idea through the challenge gauntlet via LLM."""
    prompt = f"""You are a sharp, experienced startup advisor and product strategist.

The user has an idea they want stress-tested. Run it through this gauntlet:

IDEA: {idea_text}

Evaluate across these 5 dimensions (use these exact headers):

**Who needs this?**
Identify the specific audience. Be brutally honest about market size and urgency.

**What exists already?**
Name real competitors or adjacent solutions. What's the gap?

**What's the cheapest test?**
Propose the fastest, cheapest way to validate demand (hours/days, not weeks).

**Why you, why now?**
What unique advantage does the creator have? What timing factor makes this relevant now?

**What kills it?**
Pre-mortem: the most likely reason this fails.

End with a one-line **Verdict**: rate as 🔥 (pursue aggressively), 👀 (worth exploring), or 💀 (kill it) with a one-sentence justification.

Keep each section to 2-3 sentences. Be direct, no fluff."""

    result = await generate(
        prompt=prompt,
        system_prompt="You are a direct, experienced product strategist. No hedging, no pleasantries.",
        model=None,
    )

    # Store the challenge output if we have an idea_id
    if idea_id:
        async with async_session() as session:
            await session.execute(text("""
                UPDATE ideas SET challenge_output = :output, updated_at = NOW()
                WHERE id = :id
            """), {"output": result, "id": idea_id})
            await session.commit()
        await _log_interaction(idea_id, "challenge", result)

    return result


# ── Daily Spark ────────────────────────────────────────────────────────────────

async def generate_daily_spark() -> str:
    """Generate a creative daily spark prompt for the briefing email."""
    # Gather existing ideas for context
    ideas = await list_ideas(limit=10)
    idea_context = ""
    if ideas:
        idea_titles = [f"- {i['title']} ({i['stage']})" for i in ideas[:5]]
        idea_context = f"\n\nThe user's current ideas in progress:\n" + "\n".join(idea_titles)

    prompt = f"""Generate a single creative "spark" prompt to stimulate new product/project thinking.

Requirements:
- One provocative "what if" question OR one creative constraint challenge
- Should be actionable and specific, not vague
- Can reference technology, personal data, market gaps, or cross-domain mashups
- Keep it to 1-2 sentences max
- Do NOT use hashtags or emoji
- Make it feel like a challenge worth thinking about during the day{idea_context}

Output ONLY the spark prompt, nothing else."""

    return await generate(
        prompt=prompt,
        system_prompt="You are a creative technologist who sees opportunities everywhere. One spark only.",
        model="qwen3:4b",
    )


# ── Idea Digest (for Monday emails) ───────────────────────────────────────────

async def generate_idea_digest() -> dict:
    """Generate the weekly idea digest — resurface and connect dormant ideas."""
    ideas = await list_ideas(limit=20)
    if not ideas:
        return {"has_ideas": False, "digest": "", "ideas": []}

    idea_list = "\n".join(
        f"- [{i['id']}] \"{i['title']}\" (stage: {i['stage']}, "
        f"age: {(datetime.now(timezone.utc) - i['created_at']).days}d)"
        for i in ideas
    )

    prompt = f"""You are reviewing a user's idea backlog for their weekly digest.

IDEAS:
{idea_list}

Pick 2-3 ideas that deserve attention this week. For each:
- Name the idea
- Say why it's worth revisiting NOW (connect to timing, momentum, or staleness)
- Suggest one specific next action

Keep the whole digest under 150 words. Be direct and actionable.
Do NOT use hashtags or emoji."""

    digest = await generate(
        prompt=prompt,
        system_prompt="You are a concise creative director reviewing an idea pipeline.",
        model=None,
    )

    return {"has_ideas": True, "digest": digest, "ideas": ideas[:5]}


# ── Retrospective ─────────────────────────────────────────────────────────────

async def generate_retrospective() -> str:
    """Generate a monthly idea retrospective."""
    async with async_session() as session:
        # Get all ideas with interaction counts
        r = await session.execute(text("""
            SELECT i.id, i.title, i.stage, i.created_at, i.updated_at,
                   COUNT(ii.id) as interaction_count
            FROM ideas i
            LEFT JOIN idea_interactions ii ON ii.idea_id = i.id
            GROUP BY i.id
            ORDER BY i.created_at DESC
        """))
        all_ideas = [dict(row) for row in r.mappings().fetchall()]

    if not all_ideas:
        return "No ideas captured yet. Start dropping ideas and I'll track your creative momentum."

    stages = {}
    for idea in all_ideas:
        stages.setdefault(idea["stage"], []).append(idea)

    summary = f"Total ideas: {len(all_ideas)}\n"
    for stage in STAGES:
        count = len(stages.get(stage, []))
        if count:
            summary += f"  {stage}: {count}\n"

    dormant = [i for i in all_ideas if i["stage"] in ("spark", "exploring")
               and (datetime.now(timezone.utc) - i["updated_at"]).days > 14]

    idea_list = "\n".join(f"- \"{i['title']}\" ({i['stage']}, {i['interaction_count']} interactions)" for i in all_ideas[:15])
    dormant_list = "\n".join(f"- \"{i['title']}\" (dormant {(datetime.now(timezone.utc) - i['updated_at']).days}d)" for i in dormant[:5])

    prompt = f"""You are reviewing a creator's monthly idea retrospective.

STATS:
{summary}

ALL IDEAS (recent first):
{idea_list}

DORMANT IDEAS (no activity 14+ days):
{dormant_list if dormant_list else "None — everything is active."}

Generate a retrospective (under 200 words):
- Comment on creative momentum (volume, variety, follow-through)
- Call out the "best idea being ignored" if any dormant idea has potential
- Note patterns in what types of ideas they gravitate toward
- End with one actionable suggestion for next month
- Be honest and direct
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt="You are a creative director giving a monthly review. Direct, insightful, no fluff.",
        model=None,
    )


# ── Chat Handler Helpers ───────────────────────────────────────────────────────

def parse_idea_command(message: str) -> dict:
    """Parse the user's message to determine idea factory intent."""
    lower = message.lower().strip()

    # Challenge mode: "challenge: <idea>" or "challenge idea: <text>"
    challenge_match = re.match(r'challenge[:\s]+(.+)', lower, re.DOTALL)
    if challenge_match:
        return {"action": "challenge", "text": challenge_match.group(1).strip()}

    # Capture: "idea: <title>" or "new idea: <title>"
    capture_match = re.match(r'(?:new\s+)?idea[:\s]+(.+)', lower, re.DOTALL)
    if capture_match:
        return {"action": "capture", "text": capture_match.group(1).strip()}

    # List/status
    if re.search(r'\b(list|show|my)\s+ideas?\b', lower):
        return {"action": "list", "text": ""}

    # Advance: "advance idea 3 to exploring"
    advance_match = re.match(r'advance\s+(?:idea\s+)?(\d+)\s+(?:to\s+)?(\w+)', lower)
    if advance_match:
        return {"action": "advance", "id": int(advance_match.group(1)), "stage": advance_match.group(2)}

    # Kill: "kill idea 3"
    kill_match = re.match(r'kill\s+(?:idea\s+)?(\d+)', lower)
    if kill_match:
        return {"action": "kill", "id": int(kill_match.group(1))}

    # Retrospective
    if re.search(r'\b(retro|retrospective)\b', lower):
        return {"action": "retrospective", "text": ""}

    # Default: treat as capture
    return {"action": "capture", "text": message.strip()}
