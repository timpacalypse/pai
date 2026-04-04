"""Home knowledge base service — CRUD for items, tasks, documents, and NLP intake."""

import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.home_kb")


# ── NLP Intake (natural language → structured data) ─────────────

INTAKE_SYSTEM_PROMPT = """\
You are a home maintenance data extractor.
The user will tell you something about their home in natural language.
Extract structured data from their statement.

Respond ONLY with valid JSON matching this schema (no other text):

{
  "intent": "maintenance" | "info" | "document",
  "item": {
    "name": "name of the item or system (e.g. 'HVAC air filter', 'dishwasher')",
    "category": "appliance" | "hvac" | "plumbing" | "electrical" | "outdoor" | "vehicle" | "general",
    "location": "where in the home (e.g. 'kitchen', 'garage', 'whole house')",
    "brand": "brand name if mentioned, else empty string",
    "model_info": "model number if mentioned, else empty string"
  },
  "task": {
    "description": "what needs to be done (e.g. 'replace air filter')",
    "recurrence_days": 0,
    "just_completed": true,
    "priority": "low" | "normal" | "high" | "critical",
    "notes": "any extra context"
  },
  "document": {
    "title": "document title if storing a manual/reference",
    "doc_type": "manual" | "warranty" | "receipt" | "notes" | "reference",
    "content": "the text content to store"
  }
}

Rules:
- "maintenance" intent: user is telling you about a maintenance task (replacing filters, changing oil, etc.)
- "info" intent: user is sharing general info about a home item (purchased date, location, etc.)
- "document" intent: user is storing reference material (manuals, instructions, etc.)
- Convert recurrence to days: "every 3 months" = 90, "every 6 months" = 180, "yearly" = 365, "monthly" = 30, "weekly" = 7
- If user says they "just did" or "replaced" or "changed" something, set just_completed = true
- Only include fields that are mentioned or clearly implied
- Set empty strings for fields not mentioned, 0 for numeric fields not mentioned
"""


async def process_natural_input(user_text: str, http_client=None) -> dict:
    """
    Take unstructured natural language about the home and produce structured records.
    Returns a summary of what was created/updated.
    """
    raw = await generate(
        prompt=user_text,
        system_prompt=INTAKE_SYSTEM_PROMPT,
        http_client=http_client,
    )

    parsed = _parse_json(raw)
    if parsed.get("parse_error"):
        return {"error": "Could not parse your input. Try rephrasing.", "raw": raw}

    intent = parsed.get("intent", "info")
    result = {"intent": intent, "actions": []}

    # Upsert the item
    item_data = parsed.get("item", {})
    item_name = item_data.get("name", "").strip()
    if item_name:
        item = await upsert_home_item(
            name=item_name,
            category=item_data.get("category", "general"),
            location=item_data.get("location", ""),
            brand=item_data.get("brand", ""),
            model_info=item_data.get("model_info", ""),
        )
        result["item"] = item
        result["actions"].append(f"Tracked item: {item_name}")

        # Handle maintenance task
        if intent == "maintenance":
            task_data = parsed.get("task", {})
            desc = task_data.get("description", "").strip()
            if desc:
                recurrence = task_data.get("recurrence_days", 0)
                just_completed = task_data.get("just_completed", False)
                priority = task_data.get("priority", "normal")
                notes = task_data.get("notes", "")

                task = await upsert_home_task(
                    home_item_id=item["id"],
                    description=desc,
                    recurrence_days=recurrence,
                    priority=priority,
                    notes=notes,
                )
                result["task"] = task
                result["actions"].append(f"Created task: {desc}")

                if recurrence > 0:
                    result["actions"].append(f"Recurring every {recurrence} days")

                if just_completed:
                    log = await complete_task(task["id"], notes="Initial completion via natural input")
                    result["completion"] = log
                    result["actions"].append(f"Marked as just completed — next due: {task.get('next_due_at', 'N/A')}")

        # Handle document storage
        elif intent == "document":
            doc_data = parsed.get("document", {})
            doc_title = doc_data.get("title", "").strip()
            doc_content = doc_data.get("content", "").strip()
            if doc_title and doc_content:
                doc = await add_home_document(
                    home_item_id=item["id"],
                    title=doc_title,
                    doc_type=doc_data.get("doc_type", "notes"),
                    content=doc_content,
                )
                result["document"] = doc
                result["actions"].append(f"Stored document: {doc_title}")

    return result


def _parse_json(raw: str) -> dict:
    """Extract JSON from LLM response."""
    text_clean = raw.strip()
    if text_clean.startswith("```"):
        lines = text_clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text_clean = "\n".join(lines)
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        start = text_clean.find("{")
        end = text_clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text_clean[start:end])
            except json.JSONDecodeError:
                pass
        return {"parse_error": True, "raw": raw}


# ── Home Items CRUD ─────────────────────────────────────────────


async def upsert_home_item(
    name: str,
    category: str = "general",
    location: str = "",
    brand: str = "",
    model_info: str = "",
    purchase_date: str | None = None,
    notes: str = "",
) -> dict:
    """Add or update a home item by name."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO home_items (name, category, location, brand, model_info, purchase_date, notes) "
                "VALUES (:name, :category, :location, :brand, :model_info, :purchase_date, :notes) "
                "ON CONFLICT DO NOTHING "
                "RETURNING id, name, category, location, brand, model_info, purchase_date, notes, created_at"
            ),
            {
                "name": name.strip(),
                "category": category,
                "location": location,
                "brand": brand,
                "model_info": model_info,
                "purchase_date": purchase_date,
                "notes": notes,
            },
        )
        row = result.mappings().fetchone()
        if row:
            await session.commit()
            return dict(row)

        # Item exists — fetch it, optionally update non-empty fields
        existing = await session.execute(
            text("SELECT id, name, category, location, brand, model_info, purchase_date, notes, created_at "
                 "FROM home_items WHERE name = :name"),
            {"name": name.strip()},
        )
        existing_row = existing.mappings().fetchone()
        if existing_row:
            item = dict(existing_row)
            updates = {}
            if category and category != "general" and item.get("category") == "general":
                updates["category"] = category
            if location and not item.get("location"):
                updates["location"] = location
            if brand and not item.get("brand"):
                updates["brand"] = brand
            if model_info and not item.get("model_info"):
                updates["model_info"] = model_info

            if updates:
                set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                updates["id"] = item["id"]
                await session.execute(
                    text(f"UPDATE home_items SET {set_clause}, updated_at = NOW() WHERE id = :id"),
                    updates,
                )
                await session.commit()
                item.update(updates)
            return item

        await session.commit()
        return {"name": name, "error": "unexpected state"}


async def get_home_items(category: str | None = None) -> list[dict]:
    """List all home items, optionally filtered by category."""
    async with async_session() as session:
        if category:
            result = await session.execute(
                text("SELECT id, name, category, location, brand, model_info, purchase_date, notes, created_at "
                     "FROM home_items WHERE category = :cat ORDER BY name"),
                {"cat": category},
            )
        else:
            result = await session.execute(
                text("SELECT id, name, category, location, brand, model_info, purchase_date, notes, created_at "
                     "FROM home_items ORDER BY name")
            )
        return [dict(r) for r in result.mappings()]


async def delete_home_item(item_id: int) -> bool:
    """Delete a home item (cascades to tasks)."""
    async with async_session() as session:
        result = await session.execute(
            text("DELETE FROM home_items WHERE id = :id RETURNING id"),
            {"id": item_id},
        )
        deleted = result.scalar() is not None
        await session.commit()
        return deleted


# ── Home Tasks CRUD ─────────────────────────────────────────────


async def upsert_home_task(
    home_item_id: int,
    description: str,
    recurrence_days: int = 0,
    priority: str = "normal",
    notes: str = "",
    alert_days_before: int = 7,
) -> dict:
    """Create a maintenance task for a home item."""
    now = datetime.now(timezone.utc)
    next_due = None
    if recurrence_days > 0:
        next_due = now + timedelta(days=recurrence_days)

    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO home_tasks "
                "(home_item_id, description, recurrence_days, next_due_at, alert_days_before, priority, notes) "
                "VALUES (:item_id, :desc, :recurrence, :next_due, :alert, :priority, :notes) "
                "RETURNING id, home_item_id, description, recurrence_days, last_completed_at, "
                "  next_due_at, alert_days_before, priority, notes"
            ),
            {
                "item_id": home_item_id,
                "desc": description.strip(),
                "recurrence": recurrence_days,
                "next_due": next_due,
                "alert": alert_days_before,
                "priority": priority,
                "notes": notes,
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        return dict(row)


async def get_home_tasks(
    overdue_only: bool = False,
    upcoming_days: int | None = None,
    home_item_id: int | None = None,
) -> list[dict]:
    """Get home tasks with optional filtering."""
    conditions = []
    params: dict = {}

    if overdue_only:
        conditions.append("t.next_due_at < NOW()")
    elif upcoming_days is not None:
        conditions.append("t.next_due_at <= NOW() + INTERVAL ':days days'")
        # Can't parameterize interval, use explicit
        conditions.pop()
        conditions.append(f"t.next_due_at <= NOW() + INTERVAL '{int(upcoming_days)} days'")

    if home_item_id is not None:
        conditions.append("t.home_item_id = :item_id")
        params["item_id"] = home_item_id

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT t.id, t.home_item_id, i.name AS item_name, "
                f"  t.description, t.recurrence_days, t.last_completed_at, "
                f"  t.next_due_at, t.alert_days_before, t.priority, t.notes, "
                f"  CASE WHEN t.next_due_at < NOW() THEN 'overdue' "
                f"       WHEN t.next_due_at < NOW() + INTERVAL '7 days' THEN 'upcoming' "
                f"       ELSE 'ok' END AS status "
                f"FROM home_tasks t "
                f"JOIN home_items i ON i.id = t.home_item_id "
                f"{where} "
                f"ORDER BY t.next_due_at ASC NULLS LAST"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


async def complete_task(task_id: int, notes: str = "", cost: float = 0.0) -> dict:
    """Mark a task as completed. If recurring, reset the next_due_at."""
    now = datetime.now(timezone.utc)

    async with async_session() as session:
        # Get the task
        result = await session.execute(
            text("SELECT id, recurrence_days, description FROM home_tasks WHERE id = :id"),
            {"id": task_id},
        )
        task = result.mappings().fetchone()
        if not task:
            return {"error": "Task not found"}

        # Log the completion
        await session.execute(
            text(
                "INSERT INTO home_task_log (home_task_id, completed_at, notes, cost) "
                "VALUES (:task_id, :completed_at, :notes, :cost)"
            ),
            {"task_id": task_id, "completed_at": now, "notes": notes, "cost": cost},
        )

        # Update the task
        recurrence = task["recurrence_days"]
        next_due = None
        if recurrence > 0:
            next_due = now + timedelta(days=recurrence)

        await session.execute(
            text(
                "UPDATE home_tasks SET "
                "  last_completed_at = :completed, "
                "  next_due_at = :next_due, "
                "  updated_at = NOW() "
                "WHERE id = :id"
            ),
            {"completed": now, "next_due": next_due, "id": task_id},
        )
        await session.commit()

        return {
            "task_id": task_id,
            "description": task["description"],
            "completed_at": now.isoformat(),
            "next_due_at": next_due.isoformat() if next_due else None,
        }


async def get_task_history(task_id: int) -> list[dict]:
    """Get completion history for a specific task."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, home_task_id, completed_at, notes, cost "
                "FROM home_task_log WHERE home_task_id = :id "
                "ORDER BY completed_at DESC"
            ),
            {"id": task_id},
        )
        return [dict(r) for r in result.mappings()]


# ── Home Documents CRUD ─────────────────────────────────────────


async def add_home_document(
    title: str,
    content: str,
    doc_type: str = "manual",
    home_item_id: int | None = None,
    source: str = "",
    metadata: dict | None = None,
) -> dict:
    """Store a home document (manual, warranty, notes, etc.)."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO home_documents (home_item_id, title, doc_type, content, source, metadata) "
                "VALUES (:item_id, :title, :doc_type, :content, :source, :metadata) "
                "RETURNING id, home_item_id, title, doc_type, source, created_at"
            ),
            {
                "item_id": home_item_id,
                "title": title.strip(),
                "doc_type": doc_type,
                "content": content,
                "source": source,
                "metadata": json.dumps(metadata or {}),
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        return dict(row)


async def get_home_documents(
    home_item_id: int | None = None,
    doc_type: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """List/search home documents."""
    conditions = []
    params: dict = {}

    if home_item_id is not None:
        conditions.append("d.home_item_id = :item_id")
        params["item_id"] = home_item_id
    if doc_type:
        conditions.append("d.doc_type = :doc_type")
        params["doc_type"] = doc_type
    if search:
        conditions.append("(d.title ILIKE :search OR d.content ILIKE :search)")
        params["search"] = f"%{search}%"

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT d.id, d.home_item_id, i.name AS item_name, "
                f"  d.title, d.doc_type, d.source, "
                f"  LEFT(d.content, 200) AS preview, "
                f"  d.created_at "
                f"FROM home_documents d "
                f"LEFT JOIN home_items i ON i.id = d.home_item_id "
                f"{where} "
                f"ORDER BY d.created_at DESC"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


async def get_home_document(doc_id: int) -> dict | None:
    """Get a full document by ID."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT d.id, d.home_item_id, i.name AS item_name, "
                "  d.title, d.doc_type, d.content, d.source, d.metadata, d.created_at "
                "FROM home_documents d "
                "LEFT JOIN home_items i ON i.id = d.home_item_id "
                "WHERE d.id = :id"
            ),
            {"id": doc_id},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None


async def delete_home_document(doc_id: int) -> bool:
    """Delete a home document."""
    async with async_session() as session:
        result = await session.execute(
            text("DELETE FROM home_documents WHERE id = :id RETURNING id"),
            {"id": doc_id},
        )
        deleted = result.scalar() is not None
        await session.commit()
        return deleted


# ── Alerts / Due Tasks ──────────────────────────────────────────


async def get_alerts() -> dict:
    """Get overdue and upcoming maintenance tasks."""
    overdue = await get_home_tasks(overdue_only=True)
    upcoming = await get_home_tasks(upcoming_days=14)
    # Filter upcoming to exclude the overdue ones
    overdue_ids = {t["id"] for t in overdue}
    upcoming_only = [t for t in upcoming if t["id"] not in overdue_ids and t.get("status") != "overdue"]

    return {
        "overdue": overdue,
        "upcoming": upcoming_only,
        "overdue_count": len(overdue),
        "upcoming_count": len(upcoming_only),
    }
