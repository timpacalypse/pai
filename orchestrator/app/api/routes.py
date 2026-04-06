import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.models.schemas import (
    TaskRequest, TaskResponse, CompetitionRequest, RoleType, DomainType, ROLE_DOMAIN_MAP,
    WebResearchRequest, WebResearchResponse, RankedArticle,
    ChatRequest, ChatResponse,
    FamilyMemberRequest, PreferenceRequest, MealRatingRequest, MealPlanRequest,
    HomeTellRequest, HomeItemRequest, HomeTaskCompleteRequest, HomeDocumentRequest,
    IngestURLRequest, IngestTextRequest,
    MedicalTellRequest, MedicalRecordRequest,
    RecipeRequest, RecipeRateRequest,
    CalendarTellRequest, CalendarEventRequest,
    ProcessDefinitionCreate, ProcessDefinitionUpdate, ProcessStartRequest, GateResponse,
)
from app.core.orchestrator import handle_task, handle_competition
from app.core.config import settings
from app.memory.episodic import log_episodic
from app.services.role_service import get_all_roles, resolve_roles
from app.services.web_search_service import search_and_extract
from app.services.content_ranker import rank_articles
from app.services.article_dedup import mark_article_seen, filter_new_articles, get_ledger_stats
from app.services.scheduler import run_scheduled_research
from app.memory.semantic import store_semantic
from app.services.ollama_service import generate
from app.services.prompt_service import build_system_prompt

logger = logging.getLogger("pai.api")

router = APIRouter()


@router.post("/task", response_model=TaskResponse)
async def create_task(task: TaskRequest, request: Request) -> TaskResponse:
    """Accept a task, orchestrate it, and return a structured response."""
    response = await handle_task(
        request=task,
        http_client=request.app.state.http_client,
    )

    # Persist to episodic memory (non-blocking best-effort)
    await log_episodic(task, response)

    return response


@router.post("/compete", response_model=TaskResponse)
async def compete_task(comp: CompetitionRequest, request: Request) -> TaskResponse:
    """Run multi-agent competition on a task with explicit agent and strategy selection."""
    response = await handle_competition(
        request=comp,
        http_client=request.app.state.http_client,
    )

    await log_episodic(comp, response)

    return response


@router.get("/roles")
async def list_roles():
    """List all available roles grouped by domain."""
    roles = get_all_roles()
    grouped: dict[str, list[dict]] = {}
    for role_ctx in roles:
        domain = role_ctx.domain.value
        if domain not in grouped:
            grouped[domain] = []
        grouped[domain].append({
            "role": role_ctx.role.value,
            "description": role_ctx.description,
            "goals": role_ctx.goals,
            "preferences": role_ctx.preferences,
            "constraints": role_ctx.constraints,
        })
    return {"domains": grouped, "total_roles": len(roles)}


@router.post("/skills/web-research", response_model=WebResearchResponse)
async def web_research(req: WebResearchRequest, request: Request):
    """Search the web for articles on a topic, score/rank them, deduplicate, and optionally ingest."""
    start = time.perf_counter()

    # 1. Search and extract
    results = await search_and_extract(
        query=req.topic,
        max_results=req.max_results,
        time_filter=req.time_filter,
        http_client=request.app.state.http_client,
        extract_bodies=True,
        max_extract=min(req.max_results, 8),
    )

    # 2. Deduplicate — filter out previously seen articles
    all_urls = [r.url for r in results if r.url]
    new_urls = await filter_new_articles(all_urls)
    new_results = [r for r in results if r.url in new_urls]

    # 3. Score and rank (only new articles)
    ranked = rank_articles(new_results, query=req.topic, min_score=req.min_score)

    # 4. Build response articles and record in ledger
    articles = []
    for result, score in ranked:
        await mark_article_seen(
            url=result.url,
            title=result.title,
            source=result.source,
            topic=req.topic,
            score=score.total,
        )
        articles.append(RankedArticle(
            title=result.title,
            url=result.url,
            snippet=result.snippet,
            body_preview=result.body[:500] if result.body else "",
            source=result.source,
            score=score.to_dict(),
        ))

    # 5. Auto-ingest top articles into semantic memory
    ingested_count = 0
    if req.auto_ingest and articles:
        for article in articles[:5]:
            content_to_store = f"[{article.title}] ({article.url})\n{article.snippet}"
            if article.body_preview:
                content_to_store += f"\n\n{article.body_preview}"
            row_id = await store_semantic(
                content=content_to_store,
                source=article.url,
                metadata={
                    "type": "web_research",
                    "topic": req.topic,
                    "title": article.title,
                    "score": article.score.get("total", 0),
                },
                http_client=request.app.state.http_client,
            )
            if row_id > 0:
                ingested_count += 1

    duration_ms = (time.perf_counter() - start) * 1000

    return WebResearchResponse(
        request_id=req.request_id,
        topic=req.topic,
        articles=articles,
        total_found=len(results),
        ingested_count=ingested_count,
        duration_ms=round(duration_ms, 2),
    )


@router.post("/skills/research-now")
async def research_now():
    """Manually trigger a scheduled research run (same as cron job)."""
    summary = await run_scheduled_research()
    return summary


@router.get("/skills/research-stats")
async def research_stats():
    """Get article ledger stats and scheduler status."""
    stats = await get_ledger_stats()
    from app.core.config import settings
    stats["schedule_hours"] = settings.research_schedule_hours
    stats["gmail_configured"] = bool(settings.gmail_address and settings.gmail_app_password)
    return stats


@router.get("/skills/research-articles")
async def research_articles(
    limit: int = 200,
    offset: int = 0,
    topic: str | None = None,
    min_score: float = 0.0,
):
    """Retrieve all collected articles from the ledger, newest first."""
    from sqlalchemy import text as sql_text
    from app.core.database import async_session

    query = "SELECT id, url, title, source, topic, score, discovered_at FROM article_ledger"
    params: dict = {}
    conditions = []

    if topic:
        conditions.append("topic ILIKE :topic")
        params["topic"] = f"%{topic}%"
    if min_score > 0:
        conditions.append("score >= :min_score")
        params["min_score"] = min_score

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY discovered_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset

    async with async_session() as session:
        result = await session.execute(sql_text(query), params)
        rows = []
        for row in result.mappings():
            rows.append({
                "id": row["id"],
                "url": row["url"],
                "title": row["title"],
                "source": row["source"],
                "topic": row["topic"],
                "score": round(row["score"], 3),
                "discovered_at": row["discovered_at"].isoformat(),
            })

    # Count total
    count_query = "SELECT COUNT(*) FROM article_ledger"
    if conditions:
        count_query += " WHERE " + " AND ".join(conditions)
    count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    async with async_session() as session:
        total = await session.execute(sql_text(count_query), count_params)
        total_count = total.scalar()

    return {"articles": rows, "total": total_count, "limit": limit, "offset": offset}


@router.post("/skills/research-digest")
async def send_digest_now(min_score: float = 0.0, days: int = 7):
    """Send a Gmail digest of all articles from the last N days."""
    from sqlalchemy import text as sql_text
    from app.core.database import async_session
    from app.services.gmail_service import send_research_digest

    query = (
        "SELECT url, title, source, topic, score, discovered_at "
        "FROM article_ledger "
        "WHERE discovered_at > NOW() - INTERVAL ':days days' "
        "AND score >= :min_score "
        "ORDER BY score DESC"
    )
    async with async_session() as session:
        result = await session.execute(
            sql_text(
                "SELECT url, title, source, topic, score, discovered_at "
                "FROM article_ledger "
                f"WHERE discovered_at > NOW() - INTERVAL '{days} days' "
                "AND score >= :min_score "
                "ORDER BY score DESC"
            ),
            {"min_score": min_score},
        )
        articles = []
        for row in result.mappings():
            articles.append({
                "title": row["title"],
                "url": row["url"],
                "source": row["source"],
                "topic": row["topic"],
                "snippet": "",
                "score": {"total": round(row["score"], 3)},
            })

    if not articles:
        return {"sent": False, "reason": "no articles match criteria", "count": 0}

    sent = await send_research_digest(
        articles=articles,
        topic=f"All Research (last {days} days)",
        new_count=len(articles),
        total_found=len(articles),
    )
    return {"sent": sent, "count": len(articles)}


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    """Conversational endpoint with LLM-based intent classification, role inference, and skill routing."""
    start = time.perf_counter()
    http_client = request.app.state.http_client

    # ── Step 1: LLM-based intent + role classification (single fast call) ──
    if req.role:
        # Explicit role provided — skip LLM classification for role, still classify intent
        from app.services.llm_intent_service import classify_chat_intent
        classification = await classify_chat_intent(req.message, http_client)
        roles = await resolve_roles(req.role, req.secondary_role)
    else:
        from app.services.llm_intent_service import classify_chat_intent
        classification = await classify_chat_intent(req.message, http_client)
        roles = await resolve_roles(classification["role"], None)

    intent = classification["intent"]
    skill_context_types = classification["skill_context"]

    # ── Step 2: Route based on classified intent ──

    # Briefing intent
    if intent == "briefing":
        content = await _handle_briefing_chat(request, req.message)
        return _build_chat_response(req, roles, content, "briefing", start)

    # Skill mutation intents — route to actual skill services
    if intent == "medical_record":
        content = await _handle_skill_mutation("medical", req.message, http_client)
        return _build_chat_response(req, roles, content, "medical_record", start)

    if intent == "home_record":
        content = await _handle_skill_mutation("home", req.message, http_client)
        return _build_chat_response(req, roles, content, "home_record", start)

    if intent == "calendar_event":
        content = await _handle_skill_mutation("calendar", req.message, http_client)
        return _build_chat_response(req, roles, content, "calendar_event", start)

    # ── Step 3: Conversation — build context and generate response ──
    from app.services.prompt_service import build_chat_prompt
    system_prompt = build_chat_prompt(roles)

    # Build conversation context from history
    if req.history:
        history_block = "\n".join(
            f"{'User' if m.role_name == 'user' else 'Assistant'}: {m.content}"
            for m in req.history[-10:]
        )
        user_prompt = f"Conversation so far:\n{history_block}\n\nUser: {req.message}"
    else:
        user_prompt = req.message

    # ── RAG: retrieve from semantic memory ──
    from app.memory.semantic import search_semantic
    rag_results = await search_semantic(req.message, limit=3, http_client=http_client)
    rag_context = [r["content"] for r in rag_results if r.get("similarity", 0) > 0.6]

    # ── Skill context: LLM-directed (only query what the classifier says is relevant) ──
    skill_context = await _gather_skill_context_by_type(skill_context_types)

    # Assemble augmented prompt
    context_parts = []
    if rag_context:
        context_parts.append("Relevant knowledge:\n" + "\n---\n".join(rag_context))
    if skill_context:
        context_parts.append("Live data:\n" + "\n".join(skill_context))

    if context_parts:
        user_prompt += "\n\n[Context — use ONLY if directly relevant to the question. Ignore otherwise.]\n" + "\n\n".join(context_parts)

    # Select model based on message complexity
    from app.services.ollama_service import select_model
    model = select_model(req.message)

    # Generate response
    content = await generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        model=model,
        http_client=http_client,
    )

    return _build_chat_response(req, roles, content, intent, start)


def _build_chat_response(req, roles, content: str, intent: str, start: float) -> ChatResponse:
    """Build a ChatResponse and log the turn to episodic memory."""
    import asyncio
    duration_ms = (time.perf_counter() - start) * 1000

    # Fire-and-forget log (don't block response)
    async def _log():
        from app.memory.episodic import log_chat_turn
        await log_chat_turn(
            conversation_id=str(req.conversation_id),
            role=roles.primary.role.value,
            user_message=req.message,
            assistant_message=content,
            domain=roles.primary.domain.value,
            duration_ms=round(duration_ms, 2),
        )
    asyncio.create_task(_log())

    return ChatResponse(
        request_id=req.request_id,
        conversation_id=req.conversation_id,
        role=roles.primary.role.value,
        secondary_role=roles.secondary.role.value if roles.secondary else None,
        domain=roles.primary.domain.value,
        content=content,
        intent=intent,
        duration_ms=round(duration_ms, 2),
    )


async def _handle_skill_mutation(skill: str, message: str, http_client) -> str:
    """Route a mutation intent to the appropriate skill service and return a human-readable response."""
    try:
        if skill == "medical":
            from app.services.medical_service import process_medical_input
            result = await process_medical_input(message, http_client=http_client)
            if result.get("error"):
                return f"I tried to save that medical record but ran into an issue: {result['error']}"
            actions = result.get("actions", [])
            return " | ".join(actions) if actions else f"Recorded medical entry for {result.get('family_member', 'unknown')}."

        elif skill == "home":
            from app.services.home_knowledge_service import process_natural_input
            result = await process_natural_input(user_text=message, http_client=http_client)
            if result.get("error"):
                return f"I tried to save that home record but ran into an issue: {result['error']}"
            actions = result.get("actions", [])
            return " | ".join(actions) if actions else "Saved to the home database."

        elif skill == "calendar":
            from app.services.calendar_service import process_calendar_input
            result = await process_calendar_input(message, http_client=http_client)
            if result.get("error"):
                return f"I tried to add that to the calendar but ran into an issue: {result['error']}"
            actions = result.get("actions", [])
            return " | ".join(actions) if actions else "Added to calendar."

    except Exception as e:
        return f"Sorry, I couldn't process that {skill} request: {e}"
    return "I wasn't sure how to handle that request."


@router.get("/chat/history")
async def chat_history(conversation_id: str, limit: int = 50):
    """Retrieve persisted chat history for a conversation."""
    from app.memory.episodic import get_chat_history
    turns = await get_chat_history(conversation_id, limit=limit)
    return {"conversation_id": conversation_id, "turns": turns}


@router.get("/chat/conversations")
async def chat_conversations(limit: int = 20):
    """List recent conversations with previews."""
    from app.memory.episodic import list_conversations
    convos = await list_conversations(limit=limit)
    return {"conversations": convos}


async def _handle_briefing_chat(request, user_message: str) -> str:
    """Build daily briefing data and have the LLM format it conversationally."""
    from app.services.briefing_service import build_daily_briefing, build_briefing_text

    briefing = await build_daily_briefing(http_client=request.app.state.http_client)
    briefing_text = build_briefing_text(briefing)

    system_prompt = (
        "You are PAI — a Personal AI assistant delivering a daily briefing.\n"
        "Present the briefing data below in a clear, conversational format.\n"
        "Use sections with headers. Be concise but complete.\n"
        "Do NOT invent data — only report what's provided.\n"
        "Respond in natural language, NOT JSON."
    )
    user_prompt = (
        f"The user asked: \"{user_message}\"\n\n"
        f"Here is today's briefing data:\n\n{briefing_text}\n\n"
        "Present this as my daily briefing."
    )

    content = await generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        http_client=request.app.state.http_client,
    )
    return content


async def _gather_skill_context(message: str) -> list[str]:
    """Query home KB and meal data when the chat message references those domains."""
    lower = message.lower()
    context = []

    # Home maintenance keywords
    home_keywords = [
        "filter", "maintenance", "replace", "repair", "appliance", "hvac",
        "plumbing", "furnace", "water heater", "air filter", "home", "house",
        "when do i", "when should i", "is it time to", "overdue", "due",
        "manual", "instructions", "warranty",
    ]
    if any(kw in lower for kw in home_keywords):
        try:
            from app.services.home_knowledge_service import get_alerts, get_home_tasks, get_home_documents
            # Get upcoming/overdue tasks
            alerts = await get_alerts()
            if alerts["overdue"]:
                overdue_strs = [f"OVERDUE: {t['item_name']} — {t['description']} (due {t.get('next_due_at', 'N/A')})"
                                for t in alerts["overdue"]]
                context.append("Home maintenance overdue:\n" + "\n".join(overdue_strs))
            if alerts["upcoming"]:
                upcoming_strs = [f"Upcoming: {t['item_name']} — {t['description']} (due {t.get('next_due_at', 'N/A')})"
                                 for t in alerts["upcoming"]]
                context.append("Home maintenance upcoming:\n" + "\n".join(upcoming_strs))

            # Search documents if they seem to be asking about manuals/instructions
            doc_keywords = ["manual", "instructions", "how to", "warranty", "guide"]
            if any(kw in lower for kw in doc_keywords):
                # Extract a search term
                docs = await get_home_documents(search=message[:100])
                if docs:
                    doc_strs = [f"[{d['title']}] {d.get('preview', '')}" for d in docs[:3]]
                    context.append("Relevant home documents:\n" + "\n".join(doc_strs))

            # Always show tasks if asking about specific items
            all_tasks = await get_home_tasks()
            if all_tasks:
                task_strs = [f"{t['item_name']}: {t['description']} — status: {t.get('status','ok')}, "
                             f"next due: {t.get('next_due_at', 'N/A')}"
                             for t in all_tasks[:5]]
                context.append("Home maintenance tasks:\n" + "\n".join(task_strs))
        except Exception:
            pass

    # Meal/food keywords
    meal_keywords = [
        "meal", "dinner", "lunch", "breakfast", "recipe", "cook", "food",
        "eat", "menu", "what did we", "what should we", "what does",
        "like", "dislike", "family preference",
    ]
    if any(kw in lower for kw in meal_keywords):
        try:
            from app.services.family_preference_service import build_preference_context
            from app.services.meal_planner import get_meal_plans
            prefs = await build_preference_context()
            if prefs and "No family members" not in prefs:
                context.append(prefs)
            recent_plans = await get_meal_plans(limit=1)
            if recent_plans:
                plan = recent_plans[0]
                week = plan.get("plan", {}).get("week", [])
                if week:
                    dinners = [f"{d.get('day','')}: {d.get('dinner','?')}" for d in week if isinstance(d, dict)]
                    context.append(f"Latest meal plan ({plan.get('week_label','')}):\n" + "\n".join(dinners))
        except Exception:
            pass

    # Recipe keywords
    recipe_keywords = ["recipe", "recipes", "saved recipe", "what recipes", "how to make"]
    if any(kw in lower for kw in recipe_keywords):
        try:
            from app.services.recipe_service import build_recipe_context
            # Extract a search term from the message
            recipe_ctx = await build_recipe_context(search=message[:80])
            if recipe_ctx:
                context.append(recipe_ctx)
        except Exception:
            pass

    # Medical keywords
    medical_keywords = [
        "doctor", "medical", "health", "prescription", "medication", "dentist",
        "appointment", "checkup", "vaccine", "vaccination", "surgery", "lab",
        "blood pressure", "diagnosis", "specialist", "pediatrician", "allergy",
        "allergies", "sick", "symptoms", "vision", "eye", "dental",
    ]
    if any(kw in lower for kw in medical_keywords):
        try:
            from app.services.medical_service import build_medical_context
            med_ctx = await build_medical_context()
            if med_ctx:
                context.append(med_ctx)
        except Exception:
            pass

    # Calendar/event keywords
    calendar_keywords = [
        "calendar", "schedule", "event", "appointment", "birthday", "upcoming",
        "what's coming up", "next week", "this week", "agenda", "when is",
        "what do we have", "plans for", "busy",
    ]
    if any(kw in lower for kw in calendar_keywords):
        try:
            from app.services.calendar_service import build_calendar_context
            cal_ctx = await build_calendar_context(days=14)
            if cal_ctx:
                context.append(cal_ctx)
        except Exception:
            pass

    return context


async def _gather_skill_context_by_type(context_types: list[str]) -> list[str]:
    """Query skill data sources based on LLM-classified context types."""
    context = []

    if "home" in context_types:
        try:
            from app.services.home_knowledge_service import get_alerts, get_home_tasks
            alerts = await get_alerts()
            if alerts["overdue"]:
                overdue_strs = [f"OVERDUE: {t['item_name']} — {t['description']} (due {t.get('next_due_at', 'N/A')})"
                                for t in alerts["overdue"]]
                context.append("Home maintenance overdue:\n" + "\n".join(overdue_strs))
            if alerts["upcoming"]:
                upcoming_strs = [f"Upcoming: {t['item_name']} — {t['description']} (due {t.get('next_due_at', 'N/A')})"
                                 for t in alerts["upcoming"]]
                context.append("Home maintenance upcoming:\n" + "\n".join(upcoming_strs))
            all_tasks = await get_home_tasks()
            if all_tasks:
                task_strs = [f"{t['item_name']}: {t['description']} — status: {t.get('status','ok')}, "
                             f"next due: {t.get('next_due_at', 'N/A')}"
                             for t in all_tasks[:5]]
                context.append("Home maintenance tasks:\n" + "\n".join(task_strs))
        except Exception:
            pass

    if "meals" in context_types:
        try:
            from app.services.family_preference_service import build_preference_context
            from app.services.meal_planner import get_meal_plans
            prefs = await build_preference_context()
            if prefs and "No family members" not in prefs:
                context.append(prefs)
            recent_plans = await get_meal_plans(limit=1)
            if recent_plans:
                plan = recent_plans[0]
                week = plan.get("plan", {}).get("week", [])
                if week:
                    dinners = [f"{d.get('day','')}: {d.get('dinner','?')}" for d in week if isinstance(d, dict)]
                    context.append(f"Latest meal plan ({plan.get('week_label','')}):\n" + "\n".join(dinners))
        except Exception:
            pass

    if "recipes" in context_types:
        try:
            from app.services.recipe_service import build_recipe_context
            recipe_ctx = await build_recipe_context(search="")
            if recipe_ctx:
                context.append(recipe_ctx)
        except Exception:
            pass

    if "medical" in context_types:
        try:
            from app.services.medical_service import build_medical_context
            med_ctx = await build_medical_context()
            if med_ctx:
                context.append(med_ctx)
        except Exception:
            pass

    if "calendar" in context_types:
        try:
            from app.services.calendar_service import build_calendar_context
            cal_ctx = await build_calendar_context(days=14)
            if cal_ctx:
                context.append(cal_ctx)
        except Exception:
            pass

    return context


async def _detect_and_route_skill_action(
    lower_msg: str, original_msg: str, request
) -> tuple[str, str] | None:
    """Detect if a chat message is trying to mutate skill data (add/update/log).

    Returns (intent_type, response_text) if a skill action was executed, else None.
    """
    # Action verbs that signal mutation intent
    action_verbs = [
        "add ", "record ", "log ", "save ", "store ", "update ", "track ",
        "create ", "set ", "note ", "put ", "enter ", "register ",
        "add to ", "added to ", "mark ", "schedule ",
    ]
    has_action = any(lower_msg.startswith(v) or f" {v}" in f" {lower_msg}" for v in action_verbs)
    if not has_action:
        return None

    # Medical signals
    medical_signals = [
        "medical record", "medical", "doctor", "prescription", "medication",
        "colonoscopy", "surgery", "diagnosis", "lab result", "blood work",
        "checkup", "dentist", "physical", "vaccine", "vaccination",
        "health record", "specialist", "procedure", "appointment",
        "therapy", "screening", "x-ray", "mri", "ct scan", "ultrasound",
    ]
    if any(s in lower_msg for s in medical_signals):
        try:
            from app.services.medical_service import process_medical_input
            result = await process_medical_input(
                original_msg, http_client=request.app.state.http_client,
            )
            if result.get("error"):
                return ("medical_record", f"I tried to save that medical record but ran into an issue: {result['error']}")
            actions = result.get("actions", [])
            content = " | ".join(actions) if actions else f"Recorded medical entry for {result.get('family_member', 'unknown')}."
            return ("medical_record", content)
        except Exception as e:
            return ("medical_record", f"Sorry, I couldn't save that medical record: {e}")

    # Home signals
    home_signals = [
        "home database", "home", "house", "hvac", "appliance", "maintenance",
        "plumbing", "furnace", "water heater", "air filter", "roof",
        "garage", "washer", "dryer", "dishwasher", "refrigerator", "oven",
        "lake anna", "townhouse", "condo",
        "serviced", "repaired", "replaced", "installed", "fixed",
    ]
    if any(s in lower_msg for s in home_signals):
        try:
            from app.services.home_knowledge_service import process_natural_input
            result = await process_natural_input(
                user_text=original_msg,
                http_client=request.app.state.http_client,
            )
            if result.get("error"):
                return ("home_record", f"I tried to save that home record but ran into an issue: {result['error']}")
            actions = result.get("actions", [])
            content = " | ".join(actions) if actions else "Saved to the home database."
            return ("home_record", content)
        except Exception as e:
            return ("home_record", f"Sorry, I couldn't save that to the home database: {e}")

    # Calendar signals
    calendar_signals = [
        "calendar", "event", "appointment", "birthday", "anniversary",
        "reminder", "meeting",
    ]
    if any(s in lower_msg for s in calendar_signals):
        try:
            from app.services.calendar_service import process_calendar_input
            result = await process_calendar_input(
                original_msg, http_client=request.app.state.http_client,
            )
            if result.get("error"):
                return ("calendar_event", f"I tried to add that to the calendar but ran into an issue: {result['error']}")
            actions = result.get("actions", [])
            content = " | ".join(actions) if actions else "Added to calendar."
            return ("calendar_event", content)
        except Exception as e:
            return ("calendar_event", f"Sorry, I couldn't add that to the calendar: {e}")

    return None


# ── Meal Planning Endpoints ────────────────────────────────────


@router.get("/skills/family")
async def list_family():
    """List all family members and their preferences."""
    from app.services.family_preference_service import get_family_members, get_preferences
    members = await get_family_members()
    prefs = await get_preferences()
    return {"members": members, "preferences": prefs}


@router.post("/skills/family/member")
async def add_member(req: FamilyMemberRequest):
    """Add or update a family member."""
    from app.services.family_preference_service import add_family_member
    member = await add_family_member(
        name=req.name,
        age_group=req.age_group,
        dietary_restrictions=req.dietary_restrictions,
        notes=req.notes,
    )
    return member


@router.delete("/skills/family/member/{member_id}")
async def remove_member(member_id: int):
    """Delete a family member (cascades to their preferences)."""
    from app.services.family_preference_service import delete_family_member
    deleted = await delete_family_member(member_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Family member not found")
    return {"deleted": True}


@router.post("/skills/family/preference")
async def add_preference(req: PreferenceRequest):
    """Set a meal preference for a family member."""
    from app.services.family_preference_service import set_preference
    pref = await set_preference(
        family_member_id=req.family_member_id,
        item=req.item,
        sentiment=req.sentiment,
        item_type=req.item_type,
        notes=req.notes,
    )
    return pref


@router.post("/skills/meal-plan")
async def create_meal_plan(req: MealPlanRequest, request: Request):
    """Generate a weekly meal plan based on family preferences."""
    from app.services.meal_planner import generate_meal_plan
    plan = await generate_meal_plan(
        week_label=req.week_label,
        extra_instructions=req.extra_instructions,
        http_client=request.app.state.http_client,
    )
    return plan


@router.get("/skills/meal-plan")
async def list_meal_plans(limit: int = 5):
    """Retrieve recent meal plans."""
    from app.services.meal_planner import get_meal_plans
    plans = await get_meal_plans(limit=limit)
    return {"plans": plans}


@router.get("/skills/meal-plan/{plan_id}")
async def get_plan(plan_id: int):
    """Retrieve a specific meal plan."""
    from app.services.meal_planner import get_meal_plan
    plan = await get_meal_plan(plan_id)
    if not plan:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Meal plan not found")
    return plan


@router.post("/skills/meal-feedback")
async def submit_meal_feedback(req: MealRatingRequest):
    """Rate a meal — auto-updates family preferences based on the rating."""
    from app.services.meal_planner import rate_meal
    result = await rate_meal(
        meal_name=req.meal_name,
        family_member_id=req.family_member_id,
        rating=req.rating,
        would_repeat=req.would_repeat,
        meal_plan_id=req.meal_plan_id,
        day_of_week=req.day_of_week,
        notes=req.notes,
    )
    return result


@router.get("/skills/meal-feedback")
async def list_meal_feedback(
    meal_plan_id: int | None = None,
    family_member_id: int | None = None,
):
    """Get meal ratings, optionally filtered by plan or family member."""
    from app.services.meal_planner import get_meal_ratings
    ratings = await get_meal_ratings(
        meal_plan_id=meal_plan_id,
        family_member_id=family_member_id,
    )
    return {"ratings": ratings}


@router.post("/skills/daily-recipe")
async def daily_recipe_now():
    """Manually trigger a daily recipe generation + email."""
    from app.services.meal_scheduler import run_daily_meal
    summary = await run_daily_meal(send_email=bool(settings.gmail_address))
    return summary


# ── Home Knowledge Base Endpoints ──────────────────────────────


@router.post("/skills/home/tell")
async def home_tell(req: HomeTellRequest, request: Request):
    """Tell PAI something about your home in natural language. It will extract and store structured data."""
    from app.services.home_knowledge_service import process_natural_input
    result = await process_natural_input(
        user_text=req.text,
        http_client=request.app.state.http_client,
    )
    return result


@router.get("/skills/home/items")
async def list_home_items(category: str | None = None):
    """List all tracked home items."""
    from app.services.home_knowledge_service import get_home_items
    items = await get_home_items(category=category)
    return {"items": items}


@router.post("/skills/home/items")
async def add_home_item(req: HomeItemRequest):
    """Add or update a home item."""
    from app.services.home_knowledge_service import upsert_home_item
    item = await upsert_home_item(
        name=req.name,
        category=req.category,
        location=req.location,
        brand=req.brand,
        model_info=req.model_info,
        notes=req.notes,
    )
    return item


@router.delete("/skills/home/items/{item_id}")
async def remove_home_item(item_id: int):
    """Delete a home item (cascades to its tasks)."""
    from app.services.home_knowledge_service import delete_home_item
    deleted = await delete_home_item(item_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Home item not found")
    return {"deleted": True}


@router.get("/skills/home/tasks")
async def list_home_tasks(
    overdue: bool = False,
    upcoming_days: int | None = None,
    home_item_id: int | None = None,
):
    """List home maintenance tasks. Filter by overdue, upcoming, or item."""
    from app.services.home_knowledge_service import get_home_tasks
    tasks = await get_home_tasks(
        overdue_only=overdue,
        upcoming_days=upcoming_days,
        home_item_id=home_item_id,
    )
    return {"tasks": tasks}


@router.post("/skills/home/tasks/complete")
async def complete_home_task(req: HomeTaskCompleteRequest):
    """Mark a maintenance task as completed. Recurring tasks auto-reschedule."""
    from app.services.home_knowledge_service import complete_task
    result = await complete_task(
        task_id=req.task_id,
        notes=req.notes,
        cost=req.cost,
    )
    if "error" in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/skills/home/tasks/{task_id}/history")
async def task_history(task_id: int):
    """Get completion history for a specific task."""
    from app.services.home_knowledge_service import get_task_history
    history = await get_task_history(task_id)
    return {"history": history}


@router.get("/skills/home/alerts")
async def home_alerts():
    """Get current overdue and upcoming maintenance alerts."""
    from app.services.home_knowledge_service import get_alerts
    return await get_alerts()


@router.post("/skills/home/alerts/check")
async def check_home_alerts():
    """Manually trigger a home maintenance alert check + email."""
    from app.services.home_alert_scheduler import check_and_send_alerts
    return await check_and_send_alerts()


@router.post("/skills/home/documents")
async def add_document(req: HomeDocumentRequest):
    """Store a home document (manual, warranty, notes, etc.)."""
    from app.services.home_knowledge_service import add_home_document
    doc = await add_home_document(
        title=req.title,
        content=req.content,
        doc_type=req.doc_type,
        home_item_id=req.home_item_id,
        source=req.source,
    )
    return doc


@router.get("/skills/home/documents")
async def list_documents(
    home_item_id: int | None = None,
    doc_type: str | None = None,
    search: str | None = None,
):
    """List/search home documents."""
    from app.services.home_knowledge_service import get_home_documents
    docs = await get_home_documents(
        home_item_id=home_item_id,
        doc_type=doc_type,
        search=search,
    )
    return {"documents": docs}


@router.get("/skills/home/documents/{doc_id}")
async def get_document(doc_id: int):
    """Get a full home document by ID."""
    from app.services.home_knowledge_service import get_home_document
    doc = await get_home_document(doc_id)
    if not doc:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.delete("/skills/home/documents/{doc_id}")
async def remove_document(doc_id: int):
    """Delete a home document."""
    from app.services.home_knowledge_service import delete_home_document
    deleted = await delete_home_document(doc_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": True}


# ── Quality Metrics ──

@router.get("/quality/stats")
async def quality_stats(agent: str | None = None):
    """Get aggregate quality metrics per agent."""
    from app.services.quality_service import get_agent_stats
    return await get_agent_stats(agent)


# ── Procedural Memory ──

@router.get("/memory/procedural")
async def procedural_memory_list(intent: str | None = None):
    """List proven workflow patterns from procedural memory."""
    from app.services.procedural_memory import get_patterns
    return await get_patterns(intent)


# ── Document Ingestion ──────────────────────────────────────────


@router.post("/skills/ingest/url")
async def ingest_url(req: IngestURLRequest, request: Request):
    """Ingest a web page into semantic memory (extract, chunk, embed)."""
    from app.services.document_ingestion import ingest_url as do_ingest
    result = await do_ingest(req.url, http_client=request.app.state.http_client)
    return result


@router.post("/skills/ingest/text")
async def ingest_text(req: IngestTextRequest, request: Request):
    """Ingest raw text into semantic memory (chunk, embed)."""
    from app.services.document_ingestion import ingest_text as do_ingest
    result = await do_ingest(
        req.text, title=req.title, source=req.source,
        http_client=request.app.state.http_client,
    )
    return result


@router.post("/skills/ingest/file")
async def ingest_file(request: Request):
    """Upload and ingest a file (PDF, TXT, MD, HTML) into semantic memory."""
    from fastapi import UploadFile
    from app.services.document_ingestion import ingest_file as do_ingest

    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No file uploaded. Use multipart form with field 'file'.")

    file_bytes = await file.read()
    filename = getattr(file, "filename", "upload.txt")

    result = await do_ingest(
        file_bytes, filename=filename, http_client=request.app.state.http_client,
    )
    return result


# ── Medical History ─────────────────────────────────────────────


@router.post("/skills/medical/tell")
async def medical_tell(req: MedicalTellRequest, request: Request):
    """Tell PAI about a medical event in natural language."""
    from app.services.medical_service import process_medical_input
    return await process_medical_input(req.text, http_client=request.app.state.http_client)


@router.post("/skills/medical/record")
async def add_medical_record(req: MedicalRecordRequest):
    """Add a structured medical record directly."""
    from app.services.medical_service import add_medical_record as add_record
    record = await add_record(
        family_member_id=req.family_member_id,
        date=req.date,
        category=req.category,
        provider=req.provider,
        summary=req.summary,
        details=req.details,
        follow_up=req.follow_up,
        medications=req.medications,
    )
    return record


@router.get("/skills/medical/records")
async def list_medical_records(
    family_member_id: int | None = None,
    category: str | None = None,
    limit: int = 50,
):
    """List medical records, optionally filtered."""
    from app.services.medical_service import get_medical_records
    records = await get_medical_records(
        family_member_id=family_member_id, category=category, limit=limit,
    )
    return {"records": records}


@router.get("/skills/medical/records/{record_id}")
async def get_medical_record_detail(record_id: int):
    """Get a specific medical record."""
    from app.services.medical_service import get_medical_record
    record = await get_medical_record(record_id)
    if not record:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Medical record not found")
    return record


@router.delete("/skills/medical/records/{record_id}")
async def remove_medical_record(record_id: int):
    """Delete a medical record."""
    from app.services.medical_service import delete_medical_record
    deleted = await delete_medical_record(record_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Medical record not found")
    return {"deleted": True}


@router.post("/skills/medical/upload/{record_id}")
async def upload_medical_file(record_id: int, request: Request):
    """Upload a file (PDF, image) and attach it to a medical record. Also ingests into semantic memory."""
    from app.services.document_ingestion import ingest_file as do_ingest
    from app.services.medical_service import attach_file_to_record, get_medical_record

    rec = await get_medical_record(record_id)
    if not rec:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Medical record not found")

    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No file uploaded")

    file_bytes = await file.read()
    filename = getattr(file, "filename", "upload.pdf")

    # Ingest into semantic memory
    ingest_result = await do_ingest(
        file_bytes, filename=filename, http_client=request.app.state.http_client,
    )

    # Attach reference to medical record
    attach = await attach_file_to_record(
        record_id, filename, ingest_result.get("file_path", ""),
    )

    return {**ingest_result, **attach}


# ── Recipes ─────────────────────────────────────────────────────


@router.post("/skills/recipes")
async def save_recipe(req: RecipeRequest):
    """Save or update a recipe."""
    from app.services.recipe_service import save_recipe as do_save
    return await do_save(
        title=req.title,
        ingredients=req.ingredients,
        instructions=req.instructions,
        source=req.source,
        source_url=req.source_url,
        cuisine=req.cuisine,
        prep_time_min=req.prep_time_min,
        cook_time_min=req.cook_time_min,
        servings=req.servings,
        tags=req.tags,
        notes=req.notes,
    )


@router.get("/skills/recipes")
async def list_recipes(
    search: str | None = None,
    cuisine: str | None = None,
    tag: str | None = None,
    limit: int = 50,
):
    """Search/list saved recipes."""
    from app.services.recipe_service import get_recipes
    recipes = await get_recipes(search=search, cuisine=cuisine, tag=tag, limit=limit)
    return {"recipes": recipes}


@router.get("/skills/recipes/{recipe_id}")
async def get_recipe_detail(recipe_id: int):
    """Get a specific recipe."""
    from app.services.recipe_service import get_recipe
    recipe = await get_recipe(recipe_id)
    if not recipe:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@router.post("/skills/recipes/{recipe_id}/rate")
async def rate_recipe(recipe_id: int, req: RecipeRateRequest):
    """Rate a recipe (1-5)."""
    from app.services.recipe_service import rate_recipe as do_rate
    result = await do_rate(recipe_id, req.rating)
    if "error" in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.delete("/skills/recipes/{recipe_id}")
async def remove_recipe(recipe_id: int):
    """Delete a recipe."""
    from app.services.recipe_service import delete_recipe
    deleted = await delete_recipe(recipe_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Recipe not found")
    return {"deleted": True}


# ── Calendar / Events ───────────────────────────────────────────


@router.post("/skills/calendar/tell")
async def calendar_tell(req: CalendarTellRequest, request: Request):
    """Tell PAI about an event in natural language."""
    from app.services.calendar_service import process_calendar_input
    return await process_calendar_input(req.text, http_client=request.app.state.http_client)


@router.post("/skills/calendar/event")
async def add_calendar_event(req: CalendarEventRequest):
    """Add a structured calendar event."""
    from app.services.calendar_service import add_event
    return await add_event(
        title=req.title,
        event_date=req.event_date,
        event_time=req.event_time,
        end_time=req.end_time,
        category=req.category,
        family_member_name=req.family_member_name,
        location=req.location,
        recurrence=req.recurrence,
        notes=req.notes,
    )


@router.get("/skills/calendar/events")
async def list_calendar_events(
    upcoming_days: int | None = None,
    family_member_id: int | None = None,
    category: str | None = None,
    limit: int = 50,
):
    """List calendar events."""
    from app.services.calendar_service import get_events
    events = await get_events(
        upcoming_days=upcoming_days, family_member_id=family_member_id,
        category=category, limit=limit,
    )
    return {"events": events}


@router.get("/skills/calendar/agenda")
async def calendar_agenda(days: int = 7):
    """Get upcoming agenda for the next N days."""
    from app.services.calendar_service import get_agenda
    return await get_agenda(days=days)


@router.get("/skills/calendar/events/{event_id}")
async def get_calendar_event(event_id: int):
    """Get a specific event."""
    from app.services.calendar_service import get_event
    event = await get_event(event_id)
    if not event:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.delete("/skills/calendar/events/{event_id}")
async def remove_calendar_event(event_id: int):
    """Delete a calendar event."""
    from app.services.calendar_service import delete_event
    deleted = await delete_event(event_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Event not found")
    return {"deleted": True}


# ── Learning Loop ───────────────────────────────────────────────


@router.post("/learning/generate")
async def learning_generate(request: Request):
    """Generate a candidate improvement based on quality data."""
    from app.services.learning_service import generate_improvement
    return await generate_improvement(http_client=request.app.state.http_client)


@router.get("/learning/experiments")
async def learning_list(status: str | None = None, limit: int = 20):
    """List learning experiments."""
    from app.services.learning_service import get_experiments
    experiments = await get_experiments(status=status, limit=limit)
    return {"experiments": experiments}


@router.post("/learning/evaluate/{experiment_id}")
async def learning_evaluate(experiment_id: str, request: Request):
    """Evaluate a pending experiment against current quality stats."""
    from app.services.learning_service import evaluate_experiment
    return await evaluate_experiment(experiment_id, http_client=request.app.state.http_client)


@router.post("/learning/promote/{experiment_id}")
async def learning_promote(experiment_id: str):
    """Promote an experiment — apply its improvement as an active prompt override."""
    from app.services.learning_service import promote_experiment
    return await promote_experiment(experiment_id)


@router.post("/learning/rollback/{experiment_id}")
async def learning_rollback(experiment_id: str):
    """Rollback a promoted experiment — deactivate its override."""
    from app.services.learning_service import rollback_experiment
    return await rollback_experiment(experiment_id)


@router.get("/learning/overrides")
async def learning_overrides():
    """List all active prompt overrides."""
    from app.services.learning_service import get_active_overrides
    overrides = await get_active_overrides()
    return {"overrides": overrides}


# ── Daily Briefing ──────────────────────────────────────────────


@router.post("/skills/briefing")
async def send_briefing(request: Request):
    """Build and send the daily briefing email now."""
    from app.services.briefing_service import send_daily_briefing
    sent = await send_daily_briefing(http_client=request.app.state.http_client)
    return {"sent": sent}


@router.get("/skills/briefing/preview")
async def preview_briefing(request: Request):
    """Build the daily briefing data without sending email."""
    from app.services.briefing_service import build_daily_briefing
    briefing = await build_daily_briefing(http_client=request.app.state.http_client)
    return briefing


# ── Process Engine ──────────────────────────────────────────────


@router.get("/processes")
async def list_processes(role: str | None = None, include_inactive: bool = False):
    """List process definitions."""
    from app.services.process_engine import list_process_definitions
    defs = await list_process_definitions(active_only=not include_inactive, role=role)
    return {"processes": defs}


@router.get("/processes/executions")
async def list_process_executions(
    process_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
):
    """List process executions with optional filters."""
    from app.services.process_engine import list_executions
    execs = await list_executions(process_id=process_id, status=status, limit=limit)
    return {"executions": execs}


@router.get("/processes/executions/{execution_id}")
async def get_process_execution(execution_id: str):
    """Get full state of a process execution."""
    from app.services.process_engine import get_execution
    exc = await get_execution(execution_id)
    if not exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return exc


@router.post("/processes/start")
async def start_process(req: ProcessStartRequest, request: Request):
    """Start a new process execution."""
    from app.services.process_engine import start_process as do_start
    result = await do_start(
        process_id=req.process_id,
        params=req.params,
        role=req.role,
        http_client=request.app.state.http_client,
    )
    # Only raise 400 for startup errors (no execution created), not execution failures
    if result.get("error") and "execution_id" not in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/processes/{process_id}")
async def get_process(process_id: str):
    """Get a process definition by ID."""
    from app.services.process_engine import get_process_definition
    defn = await get_process_definition(process_id)
    if not defn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")
    return defn


@router.post("/processes")
async def create_process(req: ProcessDefinitionCreate):
    """Create a new process definition."""
    from app.services.process_engine import create_process_definition
    defn = await create_process_definition(req.model_dump())
    return defn


@router.patch("/processes/{process_id}")
async def update_process(process_id: str, req: ProcessDefinitionUpdate):
    """Update a process definition."""
    from app.services.process_engine import update_process_definition
    updates = req.model_dump(exclude_unset=True)
    defn = await update_process_definition(process_id, updates)
    if not defn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")
    return defn


@router.delete("/processes/{process_id}")
async def delete_process(process_id: str):
    """Soft-delete a process definition (set inactive)."""
    from app.services.process_engine import soft_delete_process_definition
    defn = await soft_delete_process_definition(process_id)
    if not defn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")
    return {"status": "deleted", "process_id": process_id}


@router.post("/processes/executions/{execution_id}/gate")
async def respond_to_gate(execution_id: str, req: GateResponse, request: Request):
    """Respond to a gate step (approve, reject, or modify)."""
    from app.services.process_engine import resume_process
    result = await resume_process(
        execution_id=execution_id,
        decision=req.decision,
        message=req.message,
        modifications=req.modifications,
        http_client=request.app.state.http_client,
    )
    # Only raise 400 for lookup errors, not for execution-level state
    if result.get("error") and "execution_id" not in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/processes/executions/{execution_id}/cancel")
async def cancel_process_execution(execution_id: str):
    """Cancel a running or paused execution."""
    from app.services.process_engine import cancel_execution
    result = await cancel_execution(execution_id)
    if result.get("error") and "execution_id" not in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=result["error"])
    return result
