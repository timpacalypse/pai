"""Narrative Engine — Marvel-style narrative generation for villain challenge events."""

import logging

from app.services.ollama_service import generate
from app.services.villain_challenge.models import NARRATIVE_TONES

logger = logging.getLogger("pai.villain.narrative")


def _get_tone(tone_id: str) -> dict:
    """Get tone config, default to shield_tactical."""
    return NARRATIVE_TONES.get(tone_id, NARRATIVE_TONES["shield_tactical"])


async def generate_monday_briefing(
    challenge: dict,
    hero_data: dict,
    tone: str = "shield_tactical",
) -> str:
    """Generate the Monday villain assignment briefing."""
    t = _get_tone(tone)
    villain = challenge.get("villain_name", "Unknown Threat")
    villain_desc = challenge.get("description", "")
    tier = challenge.get("tier", "Unknown")
    difficulty = challenge.get("difficulty_rating", 50)
    hero_hci = hero_data.get("hci", 50)
    hero_tier = hero_data.get("tier", "Street Level")
    archetype = hero_data.get("archetype", {}).get("name", "Unclassified")
    weakness = challenge.get("weakness_text", "")
    objectives = challenge.get("objectives", [])

    obj_text = "\n".join(f"  - {o['description']} (target: {o['target_value']})" for o in objectives)

    prompt = f"""You are {t['name']}, a {t['style']} AI narrator for a Marvel X-Men themed fitness challenge system.

Generate a Monday mission briefing. The hero has been assigned a new villain to face this week.

CONTEXT:
- Hero Tier: {hero_tier} (HCI: {hero_hci})
- Hero Archetype: {archetype}
- Villain: {villain} ({tier}, Difficulty: {difficulty})
- Villain Intel: {villain_desc}
- Villain Weakness: {weakness}
- Weekly Objectives:
{obj_text}

RULES:
- Open with: "{t['opener']}"
- Use section header: "{t['mission_prefix']}"
- Keep it under 200 words
- Reference the villain by name and their threat assessment
- Mention the hero's archetype naturally
- List the mission objectives as tactical targets
- End with a motivational line appropriate to {t['name']}'s character
- Use Marvel X-Men universe language and references
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Speak in character at all times.",
        model=None,
    )


async def generate_daily_update(
    battle_status: dict,
    challenge: dict,
    hero_data: dict,
    tone: str = "shield_tactical",
) -> str:
    """Generate a daily battle status update."""
    t = _get_tone(tone)
    prob = battle_status.get("probability", 50)
    status = battle_status.get("status", "Contested")
    villain = challenge.get("villain_name", "Unknown")
    days_left = battle_status.get("days_remaining", 0)
    completed = battle_status.get("completed_objectives", 0)
    total = battle_status.get("total_objectives", 0)
    advantage = battle_status.get("advantage_text", "")
    actions = battle_status.get("recommended_actions", [])
    actions_text = "\n".join(f"  - {a}" for a in actions)

    prompt = f"""You are {t['name']}, narrating an ongoing weekly fitness battle.

SITUATION:
- Villain: {villain}
- Battle Status: {status}
- Days Remaining: {days_left}
- Objectives Completed: {completed}/{total}
- Intelligence: {advantage}
- Recommended Actions:
{actions_text}

Generate a brief daily status update (under 100 words).
- Match {t['name']}'s voice and intensity
- Reference the battle status ({status}) dramatically
- If status is Danger or Critical, increase urgency
- If status is Dominating or Advantage, be confident but not complacent
- Mention specific recommended actions if the hero is behind
- Do NOT reveal the exact probability number
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Brief tactical updates only.",
        model=None,
    )


async def generate_battle_report(
    outcome: dict,
    challenge: dict,
    hero_data: dict,
    xp_result: dict,
    tone: str = "shield_tactical",
) -> str:
    """Generate the end-of-week battle resolution narrative."""
    t = _get_tone(tone)
    villain = outcome.get("villain_name", "Unknown")
    outcome_name = outcome.get("name", "Stalemate")
    is_victory = outcome.get("is_victory", False)
    xp_awarded = xp_result.get("awarded", 0)
    leveled_up = xp_result.get("leveled_up", False)
    new_level = xp_result.get("level", 1)
    hero_tier = hero_data.get("tier", "Street Level")

    # Get thematic text
    result_text = challenge.get("victory_text", "") if is_victory else challenge.get("defeat_text", "")

    prompt = f"""You are {t['name']}, delivering a weekly battle resolution report.

BATTLE RESULT:
- Villain: {villain}
- Outcome: {outcome_name}
- Victory: {is_victory}
- Context: {result_text}
- XP Awarded: {xp_awarded}
- Level Up: {leveled_up} (now level {new_level})
- Hero Tier: {hero_tier}

Generate a dramatic battle resolution narrative (under 150 words).
- Announce the {outcome_name} dramatically
- Reference the villain's defeat or the hero's setback
- Mention XP gained
- If leveled up, celebrate the new level
- If defeated, be encouraging — frame it as training data
- Match {t['name']}'s exact voice
- End with a teaser about next week's threat
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Deliver battle reports with gravitas.",
        model=None,
    )


async def generate_nemesis_alert(
    villain_name: str,
    losses: int,
    debuff_active: bool,
    tone: str = "shield_tactical",
) -> str:
    """Generate a nemesis activation or warning narrative."""
    t = _get_tone(tone)

    if debuff_active:
        scenario = f"{villain_name} has defeated you {losses} times. NEMESIS DEBUFF ACTIVE — all XP gains reduced until you defeat them."
    else:
        scenario = f"{villain_name} has defeated you {losses} times and is now classified as your NEMESIS. They will return with increased difficulty."

    prompt = f"""You are {t['name']}, issuing a nemesis alert.

NEMESIS SITUATION:
{scenario}

Generate a dramatic nemesis alert (under 80 words).
- This is serious — treat it as a priority threat
- Reference the villain's repeated victories
- If debuff is active, convey the urgency of breaking the cycle
- {t['name']}'s voice, maximum intensity
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Nemesis alerts are highest priority.",
        model=None,
    )


async def generate_power_surge_notification(
    surge: dict,
    tone: str = "shield_tactical",
) -> str:
    """Generate a power surge activation notification."""
    t = _get_tone(tone)

    prompt = f"""You are {t['name']}, announcing a power surge activation.

POWER SURGE DETECTED:
- Name: {surge.get('surge_name', 'Unknown')}
- Effect: {surge.get('description', '')}
- Duration: {surge.get('duration_days', 0)} days

Generate a brief power surge announcement (under 60 words).
- Treat this as a positive tactical advantage
- Reference the surge name dramatically
- Mention the XP bonus
- {t['name']}'s voice
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Power surge notifications are exciting.",
        model=None,
    )


async def generate_tier_unlock(
    new_tier: str,
    hero_data: dict,
    tone: str = "shield_tactical",
) -> str:
    """Generate a tier promotion narrative."""
    t = _get_tone(tone)
    hci = hero_data.get("hci", 0)
    archetype = hero_data.get("archetype", {}).get("name", "Unclassified")

    prompt = f"""You are {t['name']}, announcing a tier promotion.

TIER PROMOTION:
- New Tier: {new_tier}
- HCI: {hci}
- Archetype: {archetype}

Generate a dramatic tier unlock announcement (under 80 words).
- This is a major achievement — celebrate appropriately
- Reference the new tier classification in X-Men universe terms
- Higher tiers face more dangerous villains
- {t['name']}'s voice
- Do NOT use hashtags or emoji"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Tier promotions are monumental.",
        model=None,
    )


async def generate_battle_recap(
    last_battle: dict,
    new_villain_name: str,
    hero_data: dict,
    tone: str = "shield_tactical",
) -> str:
    """Generate a narrative recap of the last battle result, transitioning into the new week's villain."""
    t = _get_tone(tone)
    villain = last_battle.get("villain_name", "Unknown")
    outcome = last_battle.get("outcome", "Unknown")
    score = last_battle.get("battle_score", 0)
    hero_hci = last_battle.get("hero_hci", 0)
    is_victory = "victory" in outcome.lower() or "domination" in outcome.lower()
    hero_tier = hero_data.get("tier", "Street Level") if hero_data else "Street Level"
    archetype = hero_data.get("archetype", "Recruit") if hero_data else "Recruit"

    prompt = f"""You are {t['name']}, delivering a weekly battle recap that bridges last week's result into a new threat.

LAST WEEK'S BATTLE:
- Villain Faced: {villain}
- Outcome: {outcome}
- Battle Score: {score:.1f}
- Hero HCI at Resolution: {hero_hci:.1f}
- Result: {"VICTORY" if is_victory else "DEFEAT"}

THIS WEEK'S THREAT:
- New Villain: {new_villain_name}

HERO STATUS:
- Tier: {hero_tier}
- Archetype: {archetype}

Generate a narrative recap (150-200 words) that:
- Opens with a dramatic summary of last week's battle against {villain}
- If victory: celebrate the win, note the hero's growing power
- If defeat: acknowledge the setback, frame it as fuel for the next fight
- Transition dramatically into the arrival of {new_villain_name} as the new week's threat
- Build anticipation and tension for the coming battle
- Match {t['name']}'s voice and style
- Use Marvel X-Men universe language
- Do NOT use hashtags or emoji
- Do NOT list objectives (those come separately)"""

    return await generate(
        prompt=prompt,
        system_prompt=f"You are {t['name']}. Bridge past battles into future threats with gravitas.",
        model=None,
    )


async def format_hero_status(hero_data: dict, challenge: dict | None, battle_status: dict | None) -> str:
    """Format a quick hero status summary (non-LLM, deterministic)."""
    lines = []
    lines.append(f"**{hero_data.get('archetype', {}).get('name', 'Recruit')}** — {hero_data.get('tier', 'Street Level')}")
    lines.append(f"HCI: {hero_data.get('hci', 0):.1f} | Level {hero_data.get('level', 1)} | XP: {hero_data.get('total_xp', 0)}")

    # Domain scores
    ds = hero_data.get("domain_scores", {})
    if ds:
        domain_line = " | ".join(f"{d[:3].upper()}: {s:.0f}" for d, s in sorted(ds.items()))
        lines.append(f"Domains: {domain_line}")

    # Active challenge
    if challenge:
        villain = challenge.get("villain_name", "None")
        status = challenge.get("status", "active")
        lines.append(f"Current Villain: {villain} ({status})")

        if battle_status:
            bs = battle_status.get("status", "Unknown")
            lines.append(f"Battle Status: {bs}")
            remaining = battle_status.get("days_remaining", 0)
            completed = battle_status.get("completed_objectives", 0)
            total = battle_status.get("total_objectives", 0)
            lines.append(f"Objectives: {completed}/{total} | {remaining} days remaining")
    else:
        lines.append("No active challenge — awaiting Monday assignment.")

    return "\n".join(lines)
