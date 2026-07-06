"""Panda-style planner service: monthly goals, weekly goals, daily priorities, and reviews."""

import logging
import re
from datetime import date, timedelta

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.planner")


def _week_start(d: date | None = None) -> date:
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def _month_start(d: date | None = None) -> date:
    d = d or date.today()
    return d.replace(day=1)


def _normalize_text(value: str) -> str:
    """Normalize planner text for fuzzy matching."""
    return re.sub(r"[^a-z0-9\s]+", " ", (value or "").lower()).strip()


async def ensure_planner_tables() -> None:
    """Create planner tables if they don't exist yet."""
    async with async_session() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS planner_monthly_goals (
                id SERIAL PRIMARY KEY,
                month_start DATE NOT NULL,
                slot INTEGER NOT NULL,
                title TEXT NOT NULL,
                notes TEXT DEFAULT '',
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(month_start, slot)
            )
        """))
        await session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_planner_monthly_month_start
            ON planner_monthly_goals (month_start DESC)
        """))

        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS planner_weekly_goals (
                id SERIAL PRIMARY KEY,
                week_start DATE NOT NULL,
                week_end DATE NOT NULL,
                slot INTEGER NOT NULL,
                title TEXT NOT NULL,
                notes TEXT DEFAULT '',
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(week_start, slot)
            )
        """))
        await session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_planner_weekly_week_start
            ON planner_weekly_goals (week_start DESC)
        """))

        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS planner_daily_priorities (
                id SERIAL PRIMARY KEY,
                plan_date DATE NOT NULL,
                slot INTEGER NOT NULL,
                title TEXT NOT NULL,
                notes TEXT DEFAULT '',
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(plan_date, slot)
            )
        """))
        await session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_planner_daily_plan_date
            ON planner_daily_priorities (plan_date DESC)
        """))

        await session.commit()


async def _next_open_slot(table: str, date_column: str, d: date, max_slot: int) -> int | None:
    async with async_session() as session:
        r = await session.execute(text(f"""
            SELECT slot FROM {table}
            WHERE {date_column} = :d
            ORDER BY slot ASC
        """), {"d": d})
        used = {row[0] for row in r.fetchall()}
    for i in range(1, max_slot + 1):
        if i not in used:
            return i
    return None


async def set_monthly_goal(title: str, slot: int | None = None, notes: str = "") -> dict:
    await ensure_planner_tables()
    month = _month_start()
    if slot is None:
        slot = await _next_open_slot("planner_monthly_goals", "month_start", month, 5)
    if slot is None:
        return {"error": "All 5 monthly goal slots are full. Use slot 1-5 to overwrite."}

    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO planner_monthly_goals (month_start, slot, title, notes, completed)
            VALUES (:month_start, :slot, :title, :notes, FALSE)
            ON CONFLICT (month_start, slot)
            DO UPDATE SET
                title = EXCLUDED.title,
                notes = EXCLUDED.notes,
                completed = FALSE,
                updated_at = NOW()
            RETURNING id, month_start, slot, title, completed
        """), {
            "month_start": month,
            "slot": slot,
            "title": title.strip(),
            "notes": notes.strip(),
        })
        await session.commit()
        return dict(r.mappings().fetchone())


async def set_weekly_goal(title: str, slot: int | None = None, notes: str = "") -> dict:
    await ensure_planner_tables()
    week = _week_start()
    week_end = week + timedelta(days=6)
    if slot is None:
        slot = await _next_open_slot("planner_weekly_goals", "week_start", week, 5)
    if slot is None:
        return {"error": "All 5 weekly goal slots are full. Use slot 1-5 to overwrite."}

    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO planner_weekly_goals (week_start, week_end, slot, title, notes, completed)
            VALUES (:week_start, :week_end, :slot, :title, :notes, FALSE)
            ON CONFLICT (week_start, slot)
            DO UPDATE SET
                title = EXCLUDED.title,
                notes = EXCLUDED.notes,
                completed = FALSE,
                updated_at = NOW()
            RETURNING id, week_start, week_end, slot, title, completed
        """), {
            "week_start": week,
            "week_end": week_end,
            "slot": slot,
            "title": title.strip(),
            "notes": notes.strip(),
        })
        await session.commit()
        return dict(r.mappings().fetchone())


async def set_weekly_goals_batch(items: list[str]) -> dict:
    """Set multiple weekly goals at once from a list of titles."""
    await ensure_planner_tables()
    week = _week_start()
    week_end = week + timedelta(days=6)
    saved = []
    skipped = []
    for title in items:
        title = title.strip()
        if not title:
            continue
        slot = await _next_open_slot("planner_weekly_goals", "week_start", week, 5)
        if slot is None:
            skipped.append(title)
            continue
        async with async_session() as session:
            r = await session.execute(text("""
                INSERT INTO planner_weekly_goals (week_start, week_end, slot, title, notes, completed)
                VALUES (:week_start, :week_end, :slot, :title, '', FALSE)
                ON CONFLICT (week_start, slot) DO NOTHING
                RETURNING id, slot, title
            """), {"week_start": week, "week_end": week_end, "slot": slot, "title": title})
            await session.commit()
            row = r.mappings().fetchone()
            if row:
                saved.append(dict(row))
    return {"saved": saved, "skipped": skipped}


async def set_daily_priority(title: str, slot: int | None = None, notes: str = "") -> dict:
    await ensure_planner_tables()
    today = date.today()
    if slot is None:
        slot = await _next_open_slot("planner_daily_priorities", "plan_date", today, 3)
    if slot is None:
        return {"error": "All 3 daily priority slots are full. Use slot 1-3 to overwrite."}

    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO planner_daily_priorities (plan_date, slot, title, notes, completed)
            VALUES (:plan_date, :slot, :title, :notes, FALSE)
            ON CONFLICT (plan_date, slot)
            DO UPDATE SET
                title = EXCLUDED.title,
                notes = EXCLUDED.notes,
                completed = FALSE,
                updated_at = NOW()
            RETURNING id, plan_date, slot, title, completed
        """), {
            "plan_date": today,
            "slot": slot,
            "title": title.strip(),
            "notes": notes.strip(),
        })
        await session.commit()
        return dict(r.mappings().fetchone())


async def set_daily_priority_for_date(
    title: str,
    for_date: date,
    slot: int | None = None,
    notes: str = "",
) -> dict:
    """Set one daily priority for a specific date."""
    await ensure_planner_tables()
    if slot is None:
        slot = await _next_open_slot("planner_daily_priorities", "plan_date", for_date, 3)
    if slot is None:
        return {"error": "All 3 daily priority slots are full. Use slot 1-3 to overwrite."}

    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO planner_daily_priorities (plan_date, slot, title, notes, completed)
            VALUES (:plan_date, :slot, :title, :notes, FALSE)
            ON CONFLICT (plan_date, slot)
            DO UPDATE SET
                title = EXCLUDED.title,
                notes = EXCLUDED.notes,
                completed = FALSE,
                updated_at = NOW()
            RETURNING id, plan_date, slot, title, completed
        """), {
            "plan_date": for_date,
            "slot": slot,
            "title": title.strip(),
            "notes": notes.strip(),
        })
        await session.commit()
        return dict(r.mappings().fetchone())


async def replace_daily_priorities_for_date(items: list[str], for_date: date) -> dict:
    """Replace all daily priorities for a date with up to 3 provided items."""
    await ensure_planner_tables()
    clean = [i.strip() for i in items if i and i.strip()]
    if not clean:
        return {"saved": 0, "truncated": 0, "items": []}

    kept = clean[:3]
    truncated = max(0, len(clean) - len(kept))

    async with async_session() as session:
        await session.execute(text("""
            DELETE FROM planner_daily_priorities
            WHERE plan_date = :plan_date
        """), {"plan_date": for_date})

        saved = []
        for i, text_item in enumerate(kept, start=1):
            r = await session.execute(text("""
                INSERT INTO planner_daily_priorities (plan_date, slot, title, notes, completed)
                VALUES (:plan_date, :slot, :title, '', FALSE)
                RETURNING id, plan_date, slot, title, completed
            """), {
                "plan_date": for_date,
                "slot": i,
                "title": text_item,
            })
            saved.append(dict(r.mappings().fetchone()))

        await session.commit()

    return {"saved": len(saved), "truncated": truncated, "items": saved}


async def get_daily_priorities(for_date: date) -> list[dict]:
    """Get daily priorities for a specific date."""
    await ensure_planner_tables()
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT slot, title, completed
            FROM planner_daily_priorities
            WHERE plan_date = :plan_date
            ORDER BY slot ASC
        """), {"plan_date": for_date})
        return [dict(row) for row in r.mappings().fetchall()]


async def complete_daily_priority(slot: int) -> bool:
    await ensure_planner_tables()
    today = date.today()
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE planner_daily_priorities
            SET completed = TRUE, updated_at = NOW()
            WHERE plan_date = :plan_date AND slot = :slot
            RETURNING id
        """), {"plan_date": today, "slot": slot})
        await session.commit()
        return r.scalar() is not None


async def complete_weekly_goal(slot: int) -> bool:
    await ensure_planner_tables()
    week = _week_start()
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE planner_weekly_goals
            SET completed = TRUE, updated_at = NOW()
            WHERE week_start = :week_start AND slot = :slot
            RETURNING id
        """), {"week_start": week, "slot": slot})
        await session.commit()
        return r.scalar() is not None


async def complete_monthly_goal(slot: int) -> bool:
    """Mark a monthly goal complete for the current month."""
    await ensure_planner_tables()
    month = _month_start()
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE planner_monthly_goals
            SET completed = TRUE, updated_at = NOW()
            WHERE month_start = :month_start AND slot = :slot
            RETURNING id
        """), {"month_start": month, "slot": slot})
        await session.commit()
        return r.scalar() is not None


async def complete_item_by_text(query: str, for_date: date | None = None) -> dict:
    """Mark a planner item complete by natural-language text match."""
    await ensure_planner_tables()
    anchor = for_date or date.today()
    week = _week_start(anchor)
    month = _month_start(anchor)
    qn = _normalize_text(query)
    q_tokens = {t for t in qn.split() if t}

    if not qn:
        return {"matched": False, "reason": "empty_query"}

    async with async_session() as session:
        candidates = []

        r = await session.execute(text("""
            SELECT 'daily' AS kind, id, slot, title, completed
            FROM planner_daily_priorities
            WHERE plan_date = :d
        """), {"d": anchor})
        candidates.extend([dict(row) for row in r.mappings().fetchall()])

        r = await session.execute(text("""
            SELECT 'weekly' AS kind, id, slot, title, completed
            FROM planner_weekly_goals
            WHERE week_start = :ws
        """), {"ws": week})
        candidates.extend([dict(row) for row in r.mappings().fetchall()])

        r = await session.execute(text("""
            SELECT 'monthly' AS kind, id, slot, title, completed
            FROM planner_monthly_goals
            WHERE month_start = :ms
        """), {"ms": month})
        candidates.extend([dict(row) for row in r.mappings().fetchall()])

        def _score(title: str) -> float:
            tn = _normalize_text(title)
            t_tokens = {t for t in tn.split() if t}
            if not tn:
                return 0.0
            if qn == tn:
                return 1.0
            if qn in tn or tn in qn:
                return 0.9
            if not q_tokens or not t_tokens:
                return 0.0
            overlap = len(q_tokens & t_tokens)
            return overlap / max(len(q_tokens), len(t_tokens))

        best = None
        best_score = 0.0
        for c in candidates:
            s = _score(c["title"])
            if c.get("kind") == "daily":
                s += 0.05
            if s > best_score:
                best_score = s
                best = c

        if not best or best_score < 0.35:
            return {"matched": False, "reason": "no_match"}

        if best["completed"]:
            return {
                "matched": True,
                "already_completed": True,
                "kind": best["kind"],
                "slot": best["slot"],
                "title": best["title"],
            }

        table = {
            "daily": "planner_daily_priorities",
            "weekly": "planner_weekly_goals",
            "monthly": "planner_monthly_goals",
        }[best["kind"]]

        await session.execute(text(f"""
            UPDATE {table}
            SET completed = TRUE, updated_at = NOW()
            WHERE id = :id
        """), {"id": best["id"]})
        await session.commit()

    return {
        "matched": True,
        "already_completed": False,
        "kind": best["kind"],
        "slot": best["slot"],
        "title": best["title"],
    }


async def auto_seed_weekly_goals(for_date: date | None = None) -> dict:
    """Seed weekly goals from monthly goals when the current week is empty."""
    await ensure_planner_tables()
    anchor = for_date or date.today()
    week = _week_start(anchor)
    week_end = week + timedelta(days=6)
    month = _month_start(anchor)

    async with async_session() as session:
        r = await session.execute(text("""
            SELECT COUNT(*)
            FROM planner_weekly_goals
            WHERE week_start = :week_start
        """), {"week_start": week})
        existing_weekly_count = r.scalar() or 0

        if existing_weekly_count > 0:
            return {"seeded": False, "count": 0, "reason": "weekly_already_exists"}

        r = await session.execute(text("""
            SELECT slot, title
            FROM planner_monthly_goals
            WHERE month_start = :month_start
            ORDER BY slot ASC
            LIMIT 5
        """), {"month_start": month})
        monthly_goals = [dict(row) for row in r.mappings().fetchall()]

        if not monthly_goals:
            return {"seeded": False, "count": 0, "reason": "no_monthly_goals"}

        seeded_count = 0
        for goal in monthly_goals:
            await session.execute(text("""
                INSERT INTO planner_weekly_goals (week_start, week_end, slot, title, notes, completed)
                VALUES (:week_start, :week_end, :slot, :title, :notes, FALSE)
                ON CONFLICT (week_start, slot) DO NOTHING
            """), {
                "week_start": week,
                "week_end": week_end,
                "slot": goal["slot"],
                "title": goal["title"],
                "notes": f"Auto-seeded from monthly goal M{goal['slot']}",
            })
            seeded_count += 1

        await session.commit()

    logger.info("planner_weekly_seeded", extra={"week_start": str(week), "count": seeded_count})
    return {"seeded": True, "count": seeded_count, "reason": "seeded_from_monthly"}


async def get_current_plan(for_date: date | None = None) -> dict:
    await ensure_planner_tables()
    today = for_date or date.today()
    month = _month_start(today)
    week = _week_start(today)

    await auto_seed_weekly_goals(for_date=today)

    async with async_session() as session:
        r = await session.execute(text("""
            SELECT slot, title, completed
            FROM planner_monthly_goals
            WHERE month_start = :month_start
            ORDER BY slot ASC
        """), {"month_start": month})
        monthly = [dict(row) for row in r.mappings().fetchall()]

        r = await session.execute(text("""
            SELECT slot, title, completed
            FROM planner_weekly_goals
            WHERE week_start = :week_start
            ORDER BY slot ASC
        """), {"week_start": week})
        weekly = [dict(row) for row in r.mappings().fetchall()]

        r = await session.execute(text("""
            SELECT slot, title, completed
            FROM planner_daily_priorities
            WHERE plan_date = :plan_date
            ORDER BY slot ASC
        """), {"plan_date": today})
        daily = [dict(row) for row in r.mappings().fetchall()]

    return {
        "month_start": month,
        "week_start": week,
        "week_end": week + timedelta(days=6),
        "today": today,
        "monthly": monthly,
        "weekly": weekly,
        "daily": daily,
    }


async def suggest_daily_priorities(for_date: date | None = None, limit: int = 3) -> dict:
    """Recommend today's priorities from unfinished weekly/monthly goals and completion state."""
    plan = await get_current_plan(for_date=for_date)

    incomplete_daily = [d for d in plan["daily"] if not d.get("completed")]
    incomplete_weekly = [w for w in plan["weekly"] if not w.get("completed")]
    incomplete_monthly = [m for m in plan["monthly"] if not m.get("completed")]

    recommendations = []

    # If the user already has active daily priorities, keep momentum first.
    for d in incomplete_daily:
        recommendations.append({
            "text": d["title"],
            "source": "daily_carryover",
            "rationale": "already on today's list and not completed",
        })
        if len(recommendations) >= limit:
            break

    daily_titles_norm = {_normalize_text(d["title"]) for d in plan["daily"]}
    weekly_titles_norm = {_normalize_text(w["title"]) for w in plan["weekly"]}

    if len(recommendations) < limit:
        for w in incomplete_weekly:
            wn = _normalize_text(w["title"])
            if wn in daily_titles_norm:
                continue
            recommendations.append({
                "text": f"Advance W{w['slot']}: {w['title']}",
                "source": "weekly_goal",
                "rationale": "unfinished this week",
            })
            if len(recommendations) >= limit:
                break

    if len(recommendations) < limit:
        for m in incomplete_monthly:
            mn = _normalize_text(m["title"])
            if mn in daily_titles_norm or mn in weekly_titles_norm:
                continue
            recommendations.append({
                "text": f"Move M{m['slot']} forward: {m['title']}",
                "source": "monthly_goal",
                "rationale": "monthly goal with no explicit weekly focus",
            })
            if len(recommendations) >= limit:
                break

    if not recommendations:
        recommendations.append({
            "text": "Define 1 meaningful priority for today",
            "source": "fallback",
            "rationale": "no open goals found",
        })

    return {
        "today": plan["today"],
        "weekly_open": len(incomplete_weekly),
        "monthly_open": len(incomplete_monthly),
        "recommendations": recommendations[:limit],
    }


async def get_weekly_review(for_date: date | None = None) -> dict:
    await ensure_planner_tables()
    anchor = for_date or date.today()
    week = _week_start(anchor)
    week_end = week + timedelta(days=6)

    async with async_session() as session:
        r = await session.execute(text("""
            SELECT slot, title, completed
            FROM planner_weekly_goals
            WHERE week_start = :week_start
            ORDER BY slot ASC
        """), {"week_start": week})
        weekly_goals = [dict(row) for row in r.mappings().fetchall()]

        r = await session.execute(text("""
            SELECT COUNT(*) FROM planner_daily_priorities
            WHERE plan_date BETWEEN :ws AND :we
        """), {"ws": week, "we": week_end})
        priorities_total = r.scalar() or 0

        r = await session.execute(text("""
            SELECT COUNT(*) FROM planner_daily_priorities
            WHERE plan_date BETWEEN :ws AND :we
              AND completed = TRUE
        """), {"ws": week, "we": week_end})
        priorities_done = r.scalar() or 0

        r = await session.execute(text("""
            SELECT AVG(sleep_performance) FROM fitness_sleep
            WHERE start_time >= CAST(:ws AS timestamp)
              AND start_time < CAST(:we AS timestamp) + INTERVAL '1 day'
        """), {"ws": week, "we": week_end})
        avg_sleep = r.scalar()

        r = await session.execute(text("""
            SELECT AVG(recovery_score) FROM fitness_recovery
            WHERE record_date BETWEEN :ws AND :we
        """), {"ws": week, "we": week_end})
        avg_recovery = r.scalar()

    return {
        "week_start": week,
        "week_end": week_end,
        "weekly_goals": weekly_goals,
        "weekly_done": len([g for g in weekly_goals if g.get("completed")]),
        "weekly_total": len(weekly_goals),
        "priorities_done": priorities_done,
        "priorities_total": priorities_total,
        "avg_sleep": float(avg_sleep) if avg_sleep is not None else None,
        "avg_recovery": float(avg_recovery) if avg_recovery is not None else None,
    }


async def get_monthly_review(for_date: date | None = None) -> dict:
    await ensure_planner_tables()
    anchor = for_date or date.today()
    month = _month_start(anchor)
    if month.month == 12:
        month_end = date(month.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(month.year, month.month + 1, 1) - timedelta(days=1)

    async with async_session() as session:
        r = await session.execute(text("""
            SELECT slot, title, completed
            FROM planner_monthly_goals
            WHERE month_start = :month_start
            ORDER BY slot ASC
        """), {"month_start": month})
        monthly_goals = [dict(row) for row in r.mappings().fetchall()]

        r = await session.execute(text("""
            SELECT COUNT(*) FROM planner_weekly_goals
            WHERE week_start >= :month_start
              AND week_start <= :month_end
        """), {"month_start": month, "month_end": month_end})
        weekly_total = r.scalar() or 0

        r = await session.execute(text("""
            SELECT COUNT(*) FROM planner_weekly_goals
            WHERE week_start >= :month_start
              AND week_start <= :month_end
              AND completed = TRUE
        """), {"month_start": month, "month_end": month_end})
        weekly_done = r.scalar() or 0

    return {
        "month_start": month,
        "month_end": month_end,
        "monthly_goals": monthly_goals,
        "monthly_done": len([g for g in monthly_goals if g.get("completed")]),
        "monthly_total": len(monthly_goals),
        "weekly_done": weekly_done,
        "weekly_total": weekly_total,
    }


def parse_planner_command(message: str) -> dict:
    """Parse planner commands for monthly/weekly/daily planning + reviews."""
    lower = message.lower().strip()

    day_for_query = None
    if re.search(r"\btomorrow(?:'s|s)?\b", lower):
        day_for_query = "tomorrow"
    elif re.search(r"\btoday(?:'s|s)?\b", lower):
        day_for_query = "today"

    # Batch daily goals entry, e.g. "planner goals for tomorrow: a, b, c"
    set_daily_batch = re.match(
        r"(?:planner\s+)?goals?\s+for\s+(today|tomorrow)(?:'s|s)?\s*[:\-]\s*(.+)",
        lower,
        re.DOTALL,
    )
    if set_daily_batch:
        return {
            "action": "set_daily_batch",
            "day": set_daily_batch.group(1),
            "text": set_daily_batch.group(2).strip(),
        }

    # Daily goal batch: "daily goal: a, b, c" or "dg: a, b, c"
    d_goal = re.match(r"(?:daily\s+goal|dg)(?:\s+\d)?[:\s]+(.+)", lower, re.DOTALL)
    if d_goal:
        raw = d_goal.group(1).strip()
        items = parse_goal_items(raw)
        if len(items) > 1:
            return {"action": "set_daily_batch", "day": "today", "text": raw}
        return {"action": "set_daily", "slot": None, "text": raw}

    # Monthly goal batch: "monthly goal: a, b, c"
    m_goal = re.match(r"(?:monthly\s+goal|month\s+goal|mg)(?:\s+(\d))?[:\s]+(.+)", lower, re.DOTALL)
    if m_goal:
        slot = int(m_goal.group(1)) if m_goal.group(1) else None
        raw_text = m_goal.group(2).strip()
        items = parse_goal_items(raw_text)
        if len(items) > 1:
            return {"action": "set_monthly_batch", "items": items}
        return {"action": "set_monthly", "slot": slot, "text": raw_text}

    if re.search(r"\b(weekly\s+review|review\s+week)\b", lower):
        return {"action": "weekly_review"}

    if re.search(r"\b(monthly\s+review|review\s+month)\b", lower):
        return {"action": "monthly_review"}

    if re.search(r"\b(recommend|suggest)\b.*\b(priority|priorities|today)\b", lower):
        return {"action": "recommend_priorities"}

    show_day_goals = re.search(r"\b(show|what(?:\s+are|'s)?|list)\b.*\bgoals?\b.*\b(today|tomorrow)(?:'s|s)?\b", lower)
    if show_day_goals:
        return {"action": "show_day_goals", "day": show_day_goals.group(2)}

    if day_for_query and re.search(r"\bgoals?\b", lower):
        return {"action": "show_day_goals", "day": day_for_query}

    if re.search(r"\b(show\s+planner|my\s+plan|today\s+plan|planner\s+status|planner)\b", lower):
        return {"action": "show"}

    done_daily = re.search(r"\b(done|complete)\s+(?:priority\s*)?(\d)\b", lower)
    if done_daily:
        return {"action": "complete_daily", "slot": int(done_daily.group(2))}

    done_weekly = re.search(r"\b(done|complete)\s+weekly\s*(\d)\b", lower)
    if done_weekly:
        return {"action": "complete_weekly", "slot": int(done_weekly.group(2))}

    done_monthly = re.search(r"\b(done|complete)\s+monthly\s*(\d)\b", lower)
    if done_monthly:
        return {"action": "complete_monthly", "slot": int(done_monthly.group(2))}

    # Only match complete_by_text when no colon is present (to avoid misidentifying "goal: a, complete b" as completion)
    done_by_text = re.match(r"(?:done|finished|check\s*off|checked\s*off|mark\s+done)\s+(.+)", lower)
    if done_by_text:
        return {"action": "complete_by_text", "text": done_by_text.group(1).strip()}
    # "complete X" only triggers completion when there's no colon (goal entry uses colons)
    complete_no_colon = re.match(r"complete\s+(.+)", lower)
    if complete_no_colon and ":" not in lower:
        return {"action": "complete_by_text", "text": complete_no_colon.group(1).strip()}

    w = re.match(r"(?:weekly\s+goal|week\s+goal|wg)(?:\s+(\d))?[:\s]+(.+)", lower, re.DOTALL)
    if w:
        slot = int(w.group(1)) if w.group(1) else None
        raw_text = w.group(2).strip()
        items = parse_goal_items(raw_text)
        if len(items) > 1:
            return {"action": "set_weekly_batch", "items": items}
        return {"action": "set_weekly", "slot": slot, "text": raw_text}

    d = re.match(r"(?:today\s+priority|priority|daily\s+priority|p)(?:\s+(\d))?[:\s]+(.+)", lower, re.DOTALL)
    if d:
        slot = int(d.group(1)) if d.group(1) else None
        return {"action": "set_daily", "slot": slot, "text": d.group(2).strip()}

    d_tomorrow = re.match(r"(?:tomorrow\s+priority)(?:\s+(\d))?[:\s]+(.+)", lower, re.DOTALL)
    if d_tomorrow:
        slot = int(d_tomorrow.group(1)) if d_tomorrow.group(1) else None
        return {"action": "set_daily_for_day", "day": "tomorrow", "slot": slot, "text": d_tomorrow.group(2).strip()}

    d_today = re.match(r"(?:today\s+goals?)(?:\s+(\d))?[:\s]+(.+)", lower, re.DOTALL)
    if d_today:
        slot = int(d_today.group(1)) if d_today.group(1) else None
        return {"action": "set_daily_for_day", "day": "today", "slot": slot, "text": d_today.group(2).strip()}

    return {"action": "show"}


def parse_goal_items(text: str) -> list[str]:
    """Parse a freeform goals string into individual items."""
    raw = (text or "").strip()
    if not raw:
        return []

    # Split by numbered bullets or commas/semicolons.
    if re.search(r"\b\d+[\.)]\s*", raw):
        parts = re.split(r"\b\d+[\.)]\s*", raw)
        items = [p.strip(" -\n\t") for p in parts if p.strip(" -\n\t")]
    else:
        items = [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]

    return items
