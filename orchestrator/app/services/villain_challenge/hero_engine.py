"""Hero Engine — HCI calculation, domain scoring, archetype detection, tier management."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.villain_challenge.models import (
    HCI_WEIGHTS, ALL_DOMAINS, HERO_TIERS, ARCHETYPES, COMBAT_READINESS_INPUTS,
    get_tier_for_hci,
)

logger = logging.getLogger("pai.villain.hero")


# ── HCI Calculation ──

def calculate_hci(domain_scores: dict[str, float]) -> float:
    """Calculate Hero Capability Index from domain scores (0-100)."""
    hci = 0.0
    for domain, weight in HCI_WEIGHTS.items():
        hci += domain_scores.get(domain, 0) * weight
    return round(hci, 1)


def calculate_combat_readiness(domain_scores: dict[str, float]) -> float:
    """Calculate combat readiness from key domains (0-100)."""
    if not domain_scores:
        return 0
    vals = [domain_scores.get(d, 0) for d in COMBAT_READINESS_INPUTS]
    return round(sum(vals) / len(vals), 1)


# ── Archetype Detection ──

def detect_archetype(domain_scores: dict[str, float]) -> dict:
    """Detect the hero archetype based on domain score patterns.

    Returns dict with: id, name, description, match_strength
    """
    best_match = None
    best_score = -1

    for arch_id, arch in ARCHETYPES.items():
        thresholds = arch["threshold"]
        met = 0
        total = len(thresholds)
        score_sum = 0

        for domain, threshold in thresholds.items():
            val = domain_scores.get(domain, 0)
            if val >= threshold:
                met += 1
                score_sum += val - threshold
            else:
                score_sum -= (threshold - val) * 0.5

        if total > 0:
            match_pct = met / total
            composite = match_pct * 100 + score_sum
            if composite > best_score:
                best_score = composite
                best_match = {
                    "id": arch_id,
                    "name": arch["name"],
                    "description": arch["description"],
                    "match_strength": round(match_pct * 100, 1),
                    "primary": arch["primary"],
                    "secondary": arch["secondary"],
                }

    if not best_match:
        best_match = {
            "id": "unclassified",
            "name": "Unclassified Recruit",
            "description": "Origin not yet determined. Continue training to reveal your mutant classification.",
            "match_strength": 0,
            "primary": "strength",
            "secondary": "conditioning",
        }

    return best_match


# ── Domain Score Computation from Fitness Data ──

async def compute_domain_scores() -> dict[str, float]:
    """Compute domain scores (0-100) from real fitness data.

    Sources: Whoop (recovery, sleep), Peloton (conditioning), Tonal (strength),
    daily check-ins (nutrition, mobility, body composition).
    """
    scores = {}

    async with async_session() as session:
        # Strength: from Tonal volume/frequency + strength scores in last 14 days
        r = await session.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(total_volume), 0), COALESCE(AVG(total_volume), 0)
            FROM fitness_strength
            WHERE platform = 'tonal'
              AND workout_type NOT IN ('ASSESSMENT', 'SCORE_HISTORY')
              AND start_time > NOW() - INTERVAL '14 days'
        """))
        row = r.fetchone()
        strength_count = row[0] or 0
        strength_vol = row[1] or 0
        strength_avg = row[2] or 0

        # Strength score from latest snapshot
        r2 = await session.execute(text("""
            SELECT strength_scores FROM fitness_strength
            WHERE platform = 'tonal' AND workout_type = 'ASSESSMENT'
            ORDER BY start_time DESC LIMIT 1
        """))
        snap = r2.scalar_one_or_none()
        tonal_overall = 0
        if snap and isinstance(snap, dict):
            regions = snap.get("regions", {})
            tonal_overall = regions.get("Overall", 0)

        # Normalize: 3+ sessions/week = high, strength score maps 0-2000 to 0-100
        freq_score = min(100, (strength_count / 14) * 7 * 33)  # 3/wk = 100
        tonal_score = min(100, tonal_overall / 15)  # 1500 = 100
        scores["strength"] = round((freq_score * 0.4 + tonal_score * 0.6), 1)

        # Conditioning: from Peloton ride frequency + duration in last 14 days
        r = await session.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0)
            FROM fitness_workouts
            WHERE platform = 'peloton'
              AND start_time > NOW() - INTERVAL '14 days'
        """))
        row = r.fetchone()
        cardio_count = row[0] or 0
        cardio_duration = row[1] or 0

        freq_score = min(100, (cardio_count / 14) * 7 * 25)  # 4/wk = 100
        dur_score = min(100, cardio_duration / (14 * 45 * 60) * 100)  # 45min/day avg
        scores["conditioning"] = round((freq_score * 0.5 + dur_score * 0.5), 1)

        # Recovery: from Whoop recovery scores or sleep quality
        r = await session.execute(text("""
            SELECT AVG(recovery_score), AVG(hrv_rmssd), AVG(resting_heart_rate)
            FROM fitness_recovery
            WHERE record_date > CURRENT_DATE - INTERVAL '14 days'
        """))
        row = r.fetchone()
        avg_recovery = row[0] if row[0] else None
        if avg_recovery is not None:
            scores["recovery"] = round(min(100, avg_recovery), 1)
        else:
            # Fallback: use sleep data if no Whoop recovery
            r = await session.execute(text("""
                SELECT AVG(sleep_performance)
                FROM fitness_sleep
                WHERE start_time > NOW() - INTERVAL '14 days'
            """))
            avg_sleep_perf = r.scalar_one_or_none()
            scores["recovery"] = round(min(100, avg_sleep_perf or 40), 1)

        # Consistency: check-in streak + workout regularity over 14 days
        r = await session.execute(text("""
            SELECT COUNT(DISTINCT checkin_date)
            FROM daily_checkins
            WHERE checkin_date > CURRENT_DATE - INTERVAL '14 days'
        """))
        checkin_count = r.scalar_one_or_none() or 0

        r = await session.execute(text("""
            SELECT COUNT(DISTINCT DATE(start_time))
            FROM (
                SELECT start_time FROM fitness_workouts WHERE start_time > NOW() - INTERVAL '14 days'
                UNION ALL
                SELECT start_time FROM fitness_strength WHERE start_time > NOW() - INTERVAL '14 days'
                  AND workout_type NOT IN ('ASSESSMENT', 'SCORE_HISTORY')
            ) combined
        """))
        workout_days = r.scalar_one_or_none() or 0

        checkin_score = min(100, (checkin_count / 14) * 100)
        workout_score = min(100, (workout_days / 10) * 100)  # 10/14 days = 100
        scores["consistency"] = round((checkin_score * 0.4 + workout_score * 0.6), 1)

        # Physique: from check-in body composition trends
        r = await session.execute(text("""
            SELECT body_weight, body_fat_pct
            FROM daily_checkins
            WHERE body_weight IS NOT NULL
            ORDER BY checkin_date DESC LIMIT 5
        """))
        rows = r.fetchall()
        if rows and len(rows) >= 2:
            weights = [r[0] for r in rows if r[0]]
            fats = [r[1] for r in rows if r[1]]
            # Physique based on body fat % (lower is generally better for fitness)
            if fats:
                avg_bf = sum(fats) / len(fats)
                # Map: 10% = 100, 25% = 40, 35%+ = 10
                scores["physique"] = round(max(10, min(100, 140 - avg_bf * 4)), 1)
            else:
                scores["physique"] = 50.0  # Unknown, default mid
        else:
            scores["physique"] = 50.0

        # Nutrition Adherence: from daily check-ins
        r = await session.execute(text("""
            SELECT AVG(nutrition_adherence), COUNT(*)
            FROM daily_checkins
            WHERE checkin_date > CURRENT_DATE - INTERVAL '14 days'
              AND nutrition_adherence > 0
        """))
        row = r.fetchone()
        if row[1] and row[1] > 0:
            scores["nutrition_adherence"] = round(min(100, row[0] or 0), 1)
        else:
            scores["nutrition_adherence"] = 30.0  # Low default if not tracking

        # Mobility: from daily check-ins
        r = await session.execute(text("""
            SELECT COUNT(*) FILTER (WHERE mobility_done = TRUE), COUNT(*)
            FROM daily_checkins
            WHERE checkin_date > CURRENT_DATE - INTERVAL '14 days'
        """))
        row = r.fetchone()
        if row[1] and row[1] > 0:
            scores["mobility"] = round((row[0] / row[1]) * 100, 1)
        else:
            scores["mobility"] = 20.0  # Low default

    # Ensure all domains have scores and are plain floats
    for d in ALL_DOMAINS:
        if d not in scores:
            scores[d] = 30.0
        else:
            scores[d] = float(scores[d])

    return scores


async def get_hero_profile() -> dict:
    """Get or create the hero profile with full computed state."""
    domain_scores = await compute_domain_scores()
    hci = calculate_hci(domain_scores)
    tier = get_tier_for_hci(hci)
    combat_readiness = calculate_combat_readiness(domain_scores)
    archetype = detect_archetype(domain_scores)

    # Get weakest and strongest domains
    sorted_domains = sorted(domain_scores.items(), key=lambda x: x[1])
    weakest = sorted_domains[0] if sorted_domains else ("unknown", 0)
    strongest = sorted_domains[-1] if sorted_domains else ("unknown", 0)

    async with async_session() as session:
        # Get or create profile
        r = await session.execute(text("SELECT * FROM hero_profile LIMIT 1"))
        profile = r.mappings().fetchone()

        if not profile:
            await session.execute(text("""
                INSERT INTO hero_profile (hero_name, archetype, tier, total_xp, level, power_level)
                VALUES ('Recruit', :arch, :tier, 0, 1, 0)
            """), {"arch": archetype["name"], "tier": tier})
            await session.commit()
            r = await session.execute(text("SELECT * FROM hero_profile LIMIT 1"))
            profile = r.mappings().fetchone()

        # Update profile with current computed values
        await session.execute(text("""
            UPDATE hero_profile SET
                archetype = :arch, tier = :tier, power_level = :power,
                updated_at = NOW()
            WHERE id = :id
        """), {
            "arch": archetype["name"], "tier": tier,
            "power": int(hci * 10), "id": profile["id"],
        })
        await session.commit()

        # Update domain scores
        for domain, score in domain_scores.items():
            # Get previous score for trend
            prev = await session.execute(text(
                "SELECT score FROM hero_domain_scores WHERE domain = :d"
            ), {"d": domain})
            prev_score = prev.scalar_one_or_none()
            trend = "stable"
            if prev_score is not None:
                if score > prev_score + 3:
                    trend = "improving"
                elif score < prev_score - 3:
                    trend = "declining"

            await session.execute(text("""
                INSERT INTO hero_domain_scores (domain, score, trend, last_value, updated_at)
                VALUES (:d, :s, :t, CAST(:lv AS REAL), NOW())
                ON CONFLICT (domain) DO UPDATE SET
                    score = :s, trend = :t, last_value = hero_domain_scores.score,
                    updated_at = NOW()
            """), {"d": domain, "s": score, "t": trend, "lv": prev_score if prev_score is not None else score})
        await session.commit()

        # Re-fetch profile
        r = await session.execute(text("SELECT * FROM hero_profile LIMIT 1"))
        profile = dict(r.mappings().fetchone())

    return {
        "profile": profile,
        "domain_scores": domain_scores,
        "hci": hci,
        "tier": tier,
        "combat_readiness": combat_readiness,
        "archetype": archetype,
        "weakest_domain": {"name": weakest[0], "score": weakest[1]},
        "strongest_domain": {"name": strongest[0], "score": strongest[1]},
    }


async def get_domain_scores_dict() -> dict[str, float]:
    """Quick fetch of current domain scores from DB (without recomputing)."""
    async with async_session() as session:
        r = await session.execute(text("SELECT domain, score FROM hero_domain_scores"))
        rows = r.fetchall()
        if rows:
            return {row[0]: row[1] for row in rows}
    # Fallback: compute fresh
    return await compute_domain_scores()
