import io
import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

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


@router.get("/skills")
async def list_all_skills():
    """List all registered skills with metadata."""
    from app.services.skill_registry import list_skills
    skills = list_skills()
    return {
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "examples": s.examples,
                "category": s.category,
                "can_read": s.read_handler is not None,
                "can_write": s.write_handler is not None,
            }
            for s in skills
        ],
        "total": len(skills),
    }


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
    """Conversational endpoint with dynamic skill routing via LLM classification."""
    start = time.perf_counter()
    http_client = request.app.state.http_client

    # ── Step 1: LLM-based classification (single fast call) ──
    from app.services.llm_intent_service import classify_chat_intent
    classification = await classify_chat_intent(req.message, http_client)

    if req.role:
        roles = await resolve_roles(req.role, req.secondary_role)
    else:
        roles = await resolve_roles(classification["role"], None)

    action = classification["action"]     # query | execute | conversation
    skill_id = classification["skill"]    # registered skill id or "none"

    # ── Step 2: Skill dispatch ──
    from app.services.skill_registry import get_skill

    if skill_id and skill_id != "none":
        skill = get_skill(skill_id)
        if skill:
            try:
                if action == "execute" and skill.write_handler:
                    content = await skill.write_handler(req.message, http_client)
                    return _build_chat_response(req, roles, content, f"skill:{skill_id}", start, http_client)
                elif action == "query" and skill.read_handler:
                    # Inject skill data as context for the LLM to interpret
                    skill_data = await skill.read_handler(req.message, http_client)
                    content = await _generate_with_context(
                        req, roles, http_client,
                        skill_context=[skill_data] if skill_data else [],
                    )
                    return _build_chat_response(req, roles, content, f"skill:{skill_id}", start, http_client)
                elif skill.read_handler:
                    # Action ambiguous — default to reading with context
                    skill_data = await skill.read_handler(req.message, http_client)
                    content = await _generate_with_context(
                        req, roles, http_client,
                        skill_context=[skill_data] if skill_data else [],
                    )
                    return _build_chat_response(req, roles, content, f"skill:{skill_id}", start, http_client)
            except Exception as e:
                logger.warning(f"Skill {skill_id} failed: {e}")
                # Fall through to conversation

    # ── Step 3: Conversation — build context and generate response ──
    content = await _generate_with_context(req, roles, http_client)
    return _build_chat_response(req, roles, content, "conversation", start, http_client)


async def _generate_with_context(
    req: ChatRequest,
    roles,
    http_client,
    skill_context: list[str] | None = None,
) -> str:
    """Generate an LLM response with RAG + optional skill context."""
    from app.services.prompt_service import build_chat_prompt
    from app.memory.semantic import search_semantic
    from app.services.ollama_service import select_model

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

    # RAG: retrieve from semantic memory
    rag_results = await search_semantic(req.message, limit=3, http_client=http_client)
    rag_context = [r["content"] for r in rag_results if r.get("similarity", 0) > 0.6]

    # Assemble augmented prompt
    context_parts = []
    if rag_context:
        context_parts.append("Relevant knowledge:\n" + "\n---\n".join(rag_context))
    if skill_context:
        context_parts.append("Live data:\n" + "\n".join(skill_context))

    if context_parts:
        user_prompt += "\n\n[Context — use ONLY this data to answer. Do not make up information not present in the context.]\n" + "\n\n".join(context_parts)

    model = select_model(req.message)

    return await generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        model=model,
        http_client=http_client,
    )


def _build_chat_response(req, roles, content: str, intent: str, start: float, http_client=None) -> ChatResponse:
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
        # Persist conversation record if user_id provided
        if getattr(req, "user_id", None):
            from app.services.conversation_service import ensure_conversation
            await ensure_conversation(req.conversation_id, req.user_id, title=req.message[:200], http_client=http_client)
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


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Streaming SSE version of the conversational endpoint.

    Returns Server-Sent Events with token-by-token streaming for the final response.
    Non-streaming pre-work (classification, skill data) runs before the stream begins.
    """
    import asyncio
    import json as _json

    start = time.perf_counter()
    http_client = request.app.state.http_client

    # ── Pre-work (not streamed) ──
    from app.services.llm_intent_service import classify_chat_intent
    classification = await classify_chat_intent(req.message, http_client)

    if req.role:
        roles = await resolve_roles(req.role, req.secondary_role)
    else:
        roles = await resolve_roles(classification["role"], None)

    action = classification["action"]
    skill_id = classification["skill"]

    # ── Skill dispatch (non-streaming — skills return complete text) ──
    from app.services.skill_registry import get_skill

    skill_content = None
    if skill_id and skill_id != "none":
        skill = get_skill(skill_id)
        if skill:
            try:
                if action == "execute" and skill.write_handler:
                    skill_content = await skill.write_handler(req.message, http_client)
                elif skill.read_handler:
                    skill_content = await skill.read_handler(req.message, http_client)
            except Exception as e:
                logger.warning(f"Skill {skill_id} failed in stream: {e}")

    # If skill returned complete content directly, send it as one SSE event
    if skill_content and action == "execute":
        async def _skill_stream():
            meta = _json.dumps({
                "role": roles.primary.role.value,
                "domain": roles.primary.domain.value,
                "intent": f"skill:{skill_id}",
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            })
            yield f"data: {_json.dumps({'type': 'meta', 'data': meta})}\n\n"
            yield f"data: {_json.dumps({'type': 'content', 'text': skill_content})}\n\n"
            yield f"data: {_json.dumps({'type': 'done'})}\n\n"

            # Fire-and-forget log
            asyncio.create_task(_log_stream_turn(req, roles, skill_content, f"skill:{skill_id}", start, http_client))

        return StreamingResponse(_skill_stream(), media_type="text/event-stream")

    # ── Build context for streaming generation ──
    from app.services.prompt_service import build_chat_prompt
    from app.memory.semantic import search_semantic
    from app.services.ollama_service import generate_stream, select_model

    system_prompt = build_chat_prompt(roles)

    if req.history:
        history_block = "\n".join(
            f"{'User' if m.role_name == 'user' else 'Assistant'}: {m.content}"
            for m in req.history[-10:]
        )
        user_prompt = f"Conversation so far:\n{history_block}\n\nUser: {req.message}"
    else:
        user_prompt = req.message

    rag_results = await search_semantic(req.message, limit=3, http_client=http_client)
    rag_context = [r["content"] for r in rag_results if r.get("similarity", 0) > 0.6]

    context_parts = []
    if rag_context:
        context_parts.append("Relevant knowledge:\n" + "\n---\n".join(rag_context))
    if skill_content:
        context_parts.append("Live data:\n" + skill_content)

    if context_parts:
        user_prompt += "\n\n[Context — use ONLY this data to answer. Do not make up information not present in the context.]\n" + "\n\n".join(context_parts)

    model = select_model(req.message)
    intent = f"skill:{skill_id}" if skill_id and skill_id != "none" else "conversation"

    async def _event_stream():
        meta = _json.dumps({
            "role": roles.primary.role.value,
            "domain": roles.primary.domain.value,
            "intent": intent,
            "model": model,
        })
        yield f"data: {_json.dumps({'type': 'meta', 'data': meta})}\n\n"

        full_content = []
        async for token in generate_stream(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model,
            http_client=http_client,
        ):
            full_content.append(token)
            yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        yield f"data: {_json.dumps({'type': 'done', 'duration_ms': duration_ms})}\n\n"

        # Fire-and-forget log
        content = "".join(full_content)
        asyncio.create_task(_log_stream_turn(req, roles, content, intent, start, http_client))

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


async def _log_stream_turn(req, roles, content: str, intent: str, start: float, http_client=None):
    """Log a streamed chat turn to episodic memory."""
    try:
        from app.memory.episodic import log_chat_turn
        duration_ms = (time.perf_counter() - start) * 1000
        await log_chat_turn(
            conversation_id=str(req.conversation_id),
            role=roles.primary.role.value,
            user_message=req.message,
            assistant_message=content,
            domain=roles.primary.domain.value,
            duration_ms=round(duration_ms, 2),
        )
        if getattr(req, "user_id", None):
            from app.services.conversation_service import ensure_conversation
            await ensure_conversation(req.conversation_id, req.user_id, title=req.message[:200], http_client=http_client)
    except Exception as e:
        logger.warning(f"stream_log_failed: {e}")


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


@router.post("/skills/receipts/upload")
async def upload_receipt(request: Request):
    """Upload a receipt (image or PDF) for tax tracking."""
    from app.services.receipt_service import ingest_receipt

    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No file uploaded. Use multipart form with field 'file'.")

    file_bytes = await file.read()
    filename = getattr(file, "filename", "receipt.jpg")

    result = await ingest_receipt(file_bytes, filename)
    return result


@router.get("/skills/receipts")
async def list_receipts(request: Request):
    """Query receipts with optional filters."""
    from app.services.receipt_service import get_receipts
    params = request.query_params
    year = int(params["year"]) if "year" in params else None
    category = params.get("category")
    vendor = params.get("vendor")
    limit = int(params.get("limit", 50))
    return await get_receipts(tax_year=year, category=category, vendor=vendor, limit=limit)


@router.get("/skills/receipts/summary")
async def receipt_summary(request: Request):
    """Get tax receipt summary by category."""
    from app.services.receipt_service import get_tax_summary
    params = request.query_params
    year = int(params["year"]) if "year" in params else None
    return await get_tax_summary(tax_year=year)


# ── Medical History ─────────────────────────────────────────────


@router.post("/skills/medical/tell")
async def medical_tell(req: MedicalTellRequest, request: Request):
    """Tell PAI about a medical event, or ask about medical history/documents."""
    from app.services.ollama_service import generate
    # Use LLM to determine if this is a read (query) or write (record)
    raw = await generate(
        prompt=f"Is this a question asking about medical history/records, or a statement recording new medical data? Return ONLY 'read' or 'write'.\n\nMessage: {req.text}",
        system_prompt="Classify medical messages. 'read' = asking about history, medications, records, lab results, appointments, vaccinations. 'write' = logging a new visit, recording medication, adding health data. Return only one word.",
        model="qwen3:4b",
        http_client=request.app.state.http_client,
    )
    is_read = "read" in raw.strip().lower()
    if is_read:
        from app.services.skill_registry import get_skill
        skill = get_skill("medical")
        if skill and skill.read_handler:
            context = await skill.read_handler(req.text, http_client=request.app.state.http_client)
            from app.services.ollama_service import generate
            response = await generate(
                prompt=f"Based on the following medical data, answer the user's question.\n\nMedical data:\n{context}\n\nUser question: {req.text}",
                system_prompt="You are a knowledgeable family health assistant. Provide clear, helpful answers based on the medical data provided. Always recommend consulting a healthcare provider for medical decisions.",
                http_client=request.app.state.http_client,
            )
            return {"actions": [f"✓ {response}"], "family_member": "Tim"}
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


@router.post("/skills/recipes/paste")
async def paste_recipe(request: Request):
    """Parse and save a pasted recipe — no LLM, no token limit."""
    from app.services.recipe_service import ingest_recipe_text
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No recipe text provided")
    result = await ingest_recipe_text(text)
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return result


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


@router.get("/skills/calendar/google/status")
async def google_calendar_status():
    """Check if Google Calendar integration is configured and working."""
    try:
        from app.services.google_calendar_service import is_configured, is_authorized
        configured = await is_configured()
        if not configured:
            return {"status": "not_configured", "message": "Place google_credentials.json in /credentials/ directory and restart"}
        authorized = await is_authorized()
        if not authorized:
            return {"status": "needs_auth", "message": "Visit /skills/calendar/google/auth to authorize"}
        return {"status": "connected"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/skills/calendar/google/auth")
async def google_calendar_auth():
    """Get the Google OAuth authorization URL. User visits this URL, grants access, and posts the code back."""
    from app.services.google_calendar_service import get_auth_url, is_authorized
    if await is_authorized():
        return {"status": "already_authorized"}
    url = get_auth_url()
    if not url:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No credentials file found")
    return {"auth_url": url, "instructions": "Visit the URL, grant access, copy the authorization code, then POST it to /skills/calendar/google/callback"}


@router.post("/skills/calendar/google/callback")
async def google_calendar_callback(request: Request):
    """Exchange the OAuth authorization code for tokens."""
    from app.services.google_calendar_service import exchange_auth_code
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Missing 'code' field")
    result = await exchange_auth_code(code)
    return result


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


# ── Content Services (grocery, linkedin, digest, dinner) ────────


@router.get("/skills/grocery")
async def get_grocery_list(request: Request):
    """Generate a consolidated grocery list from meal plans."""
    from app.services.content_service import generate_grocery_list
    result = await generate_grocery_list(http_client=request.app.state.http_client)
    return result


@router.get("/skills/linkedin/draft")
async def get_linkedin_draft(request: Request, topic: str = ""):
    """Draft a LinkedIn post from top articles."""
    from app.services.content_service import draft_linkedin_post
    result = await draft_linkedin_post(topic=topic, http_client=request.app.state.http_client)
    return result


@router.get("/skills/research/weekly-digest")
async def get_weekly_digest(request: Request):
    """Generate weekly AI + cybersecurity digest."""
    from app.services.content_service import generate_weekly_digest
    result = await generate_weekly_digest(http_client=request.app.state.http_client)
    return result


@router.get("/skills/meals/tonight")
async def get_tonights_dinner(request: Request):
    """Get tonight's dinner recipe."""
    from app.services.content_service import get_tonights_dinner as _get_dinner
    result = await _get_dinner(http_client=request.app.state.http_client)
    return result


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


# ── Workout Tracking ──────────────────────────────────────────────


@router.post("/skills/workout/tell")
async def workout_tell(request: Request):
    """Tell PAI about a workout program or log an activity in natural language."""
    body = await request.json()
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    from app.services.workout_service import process_workout_input
    return await process_workout_input(text, http_client=request.app.state.http_client)


@router.post("/skills/workout/log")
async def workout_log(request: Request):
    """Log a completed workout activity directly."""
    body = await request.json()
    from app.services.workout_service import log_activity
    record = await log_activity(
        activity=body.get("activity", ""),
        duration_minutes=int(body.get("duration_minutes", 0)),
        notes=body.get("notes", ""),
        metrics=body.get("metrics", {}),
    )
    return record


@router.get("/skills/workout/today")
async def workout_today():
    """Get today's scheduled workout and completed activities."""
    from app.services.workout_service import get_todays_workout
    return await get_todays_workout()


@router.get("/skills/workout/programs")
async def workout_programs(active_only: bool = True):
    """List workout programs."""
    from app.services.workout_service import get_programs
    programs = await get_programs(active_only=active_only)
    return {"programs": programs}


@router.delete("/skills/workout/programs/{program_id}")
async def workout_deactivate(program_id: int):
    """Deactivate a workout program."""
    from app.services.workout_service import deactivate_program
    ok = await deactivate_program(program_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Program not found")
    return {"deactivated": True}


@router.get("/skills/workout/logs")
async def workout_logs(limit: int = 30, days_back: int | None = None):
    """Get recent workout logs."""
    from app.services.workout_service import get_logs
    logs = await get_logs(limit=limit, days_back=days_back)
    return {"logs": logs}


# ── User Authentication ─────────────────────────────────────────


@router.post("/auth/login")
async def user_login(request: Request):
    """Login or create a user by first name. Returns user info."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    from app.services.user_service import login_or_create
    result = await login_or_create(name)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/auth/users")
async def list_all_users():
    """List all users for the login screen."""
    from app.services.user_service import list_users
    users = await list_users()
    return {"users": users}


# ── Conversations (per user) ────────────────────────────────────


@router.get("/conversations")
async def get_conversations(user_id: int, limit: int = 30):
    """Get a user's conversation history."""
    from app.services.conversation_service import get_user_conversations
    convos = await get_user_conversations(user_id, limit=limit)
    return {"conversations": convos}


@router.delete("/conversations/{conversation_id}")
async def remove_conversation(conversation_id: str):
    """Delete a conversation and its chat history."""
    from uuid import UUID
    from app.services.conversation_service import delete_conversation
    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation ID")
    deleted = await delete_conversation(cid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


# ── Memory Consolidation ────────────────────────────────────────


@router.post("/memory/consolidate")
async def consolidate_mem(request: Request):
    """Merge duplicate semantic memory entries."""
    from app.services.memory_consolidation import consolidate_memory
    result = await consolidate_memory(http_client=request.app.state.http_client)
    return result


@router.post("/memory/prune")
async def prune_mem():
    """Remove low-quality semantic memory entries."""
    from app.services.memory_consolidation import prune_low_quality
    result = await prune_low_quality()
    return result


@router.get("/memory/stats")
async def memory_stats():
    """Get semantic memory statistics."""
    from app.services.memory_consolidation import get_memory_stats
    return await get_memory_stats()


# ── Fitness Platform Integration ──


@router.get("/fitness/summary")
async def fitness_summary(days: int = 7):
    """Get cross-platform fitness summary."""
    from app.services.fitness.fitness_query import get_fitness_summary
    return {"summary": await get_fitness_summary(days=days)}


@router.get("/fitness/workouts")
async def fitness_workouts(days: int = 7, platform: str = ""):
    """Get workout history across platforms."""
    from app.services.fitness.fitness_query import get_workout_details
    return {"workouts": await get_workout_details(days=days, platform=platform)}


@router.get("/fitness/recovery")
async def fitness_recovery(days: int = 14):
    """Get recovery and HRV trends."""
    from app.services.fitness.fitness_query import get_recovery_trends
    return {"recovery": await get_recovery_trends(days=days)}


@router.get("/fitness/sleep")
async def fitness_sleep(days: int = 14):
    """Get sleep analysis."""
    from app.services.fitness.fitness_query import get_sleep_analysis
    return {"sleep": await get_sleep_analysis(days=days)}


@router.get("/fitness/strength")
async def fitness_strength(days: int = 30):
    """Get Tonal strength progress."""
    from app.services.fitness.fitness_query import get_strength_progress
    return {"strength": await get_strength_progress(days=days)}


@router.post("/fitness/sync")
async def fitness_sync():
    """Manually trigger a sync of all configured fitness platforms."""
    from app.services.fitness.fitness_query import trigger_sync
    return {"result": await trigger_sync()}


@router.get("/fitness/sync/status")
async def fitness_sync_status():
    """Get sync status for all fitness platforms."""
    from app.services.fitness.fitness_query import _get_sync_status
    return {"platforms": await _get_sync_status()}


# ── Whoop OAuth2 ─────────────────────────────────────────────

WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_SCOPES = "offline read:workout read:recovery read:sleep read:cycles read:profile read:body_measurement"


@router.get("/whoop/auth")
async def whoop_auth_start(request: Request):
    """Start Whoop OAuth2 flow. Visit this URL in your browser."""
    if not settings.whoop_client_id:
        raise HTTPException(400, "WHOOP_CLIENT_ID not configured in .env")

    state = secrets.token_urlsafe(8)[:8]
    redis = request.app.state.redis
    await redis.set("pai:whoop_oauth_state", state, ex=600)

    redirect_uri = str(request.base_url).rstrip("/") + "/whoop/callback"
    params = (
        f"?response_type=code"
        f"&client_id={settings.whoop_client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={WHOOP_SCOPES.replace(' ', '%20')}"
        f"&state={state}"
    )
    auth_url = WHOOP_AUTH_URL + params
    return RedirectResponse(auth_url)


@router.get("/whoop/callback")
async def whoop_auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Whoop OAuth2 callback — exchanges code for tokens."""
    if error:
        return HTMLResponse(f"<h2>Whoop authorization failed</h2><p>{error}</p>", status_code=400)

    redis = request.app.state.redis
    expected = await redis.get("pai:whoop_oauth_state")
    if not expected or state != expected:
        return HTMLResponse("<h2>Invalid state parameter</h2><p>CSRF check failed. Try again from /whoop/auth</p>", status_code=400)
    await redis.delete("pai:whoop_oauth_state")

    if not code:
        return HTMLResponse("<h2>No authorization code received</h2>", status_code=400)

    redirect_uri = str(request.base_url).rstrip("/") + "/whoop/callback"

    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(WHOOP_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.whoop_client_id,
            "client_secret": settings.whoop_client_secret,
            "redirect_uri": redirect_uri,
        })

    if resp.status_code != 200:
        logger.error("whoop_token_exchange_failed", extra={"status": resp.status_code, "body": resp.text})
        return HTMLResponse(f"<h2>Token exchange failed</h2><pre>{resp.text}</pre>", status_code=500)

    tokens = resp.json()

    # Store tokens in DB
    from app.services.fitness.whoop_sync import store_whoop_tokens
    await store_whoop_tokens(tokens)

    return HTMLResponse(
        "<h2>Whoop connected successfully!</h2>"
        "<p>Access token and refresh token stored. PAI will now sync your Whoop data.</p>"
        "<p>You can close this tab.</p>"
    )


# ── Villain Challenge System ───────────────────────────────────

@router.get("/villain/profile")
async def villain_profile():
    """Get current hero profile, HCI, domain scores, tier, archetype."""
    from app.services.villain_challenge.hero_engine import get_hero_profile
    return await get_hero_profile()


@router.get("/villain/challenge")
async def villain_challenge():
    """Get the current active challenge with objectives and battle status."""
    from app.services.villain_challenge.villain_engine import get_active_challenge
    from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
    from app.services.villain_challenge.hero_engine import get_hero_profile

    challenge = await get_active_challenge()
    if not challenge:
        return {"challenge": None, "battle_status": None}

    hero_data = await get_hero_profile()
    battle_status = await calculate_daily_battle_probability(challenge, hero_data)

    return {"challenge": challenge, "battle_status": battle_status}


@router.post("/villain/challenge/create")
async def villain_challenge_create():
    """Manually trigger weekly challenge creation (normally automatic on Monday)."""
    from app.services.villain_challenge.hero_engine import get_hero_profile
    from app.services.villain_challenge.villain_engine import (
        select_weekly_villain, generate_weekly_objectives, create_weekly_challenge,
    )

    hero_data = await get_hero_profile()
    villain_selection = await select_weekly_villain(hero_data)
    objectives = await generate_weekly_objectives(villain_selection, hero_data)
    challenge = await create_weekly_challenge(villain_selection, objectives)
    return challenge


@router.post("/villain/challenge/resolve")
async def villain_challenge_resolve():
    """Manually trigger weekly battle resolution (normally automatic on Sunday)."""
    from app.services.villain_challenge.villain_engine import get_active_challenge
    from app.services.villain_challenge.hero_engine import get_hero_profile
    from app.services.villain_challenge.battle_system import resolve_weekly_battle
    from app.services.villain_challenge.xp_engine import award_battle_xp
    from app.services.villain_challenge.narrative import generate_battle_report

    challenge = await get_active_challenge()
    if not challenge or challenge.get("status") != "active":
        raise HTTPException(status_code=400, detail="No active challenge to resolve")

    hero_data = await get_hero_profile()
    outcome = await resolve_weekly_battle(challenge, hero_data)
    xp_result = await award_battle_xp(outcome, challenge_id=challenge.get("id"))
    narrative = await generate_battle_report(outcome, challenge, hero_data, xp_result)

    return {
        "outcome": outcome,
        "xp": xp_result,
        "narrative": narrative,
    }


@router.post("/villain/checkin")
async def villain_checkin(request: Request):
    """Submit a daily fitness check-in."""
    from app.core.database import async_session as db_session
    from sqlalchemy import text as sql_text

    body = await request.json()

    async with db_session() as session:
        await session.execute(sql_text("""
            INSERT INTO daily_checkins
                (checkin_date, body_weight, body_fat_pct, soreness_level,
                 soreness_notes, injury_notes, nutrition_adherence,
                 protein_target_hit, mobility_done, notes)
            VALUES (CURRENT_DATE, :bw, :bf, :sore, :sore_notes, :injury,
                    :nutr, :protein, :mobility, :notes)
            ON CONFLICT (checkin_date) DO UPDATE SET
                body_weight = COALESCE(EXCLUDED.body_weight, daily_checkins.body_weight),
                body_fat_pct = COALESCE(EXCLUDED.body_fat_pct, daily_checkins.body_fat_pct),
                soreness_level = EXCLUDED.soreness_level,
                soreness_notes = EXCLUDED.soreness_notes,
                injury_notes = EXCLUDED.injury_notes,
                nutrition_adherence = EXCLUDED.nutrition_adherence,
                protein_target_hit = EXCLUDED.protein_target_hit,
                mobility_done = EXCLUDED.mobility_done,
                notes = EXCLUDED.notes
        """), {
            "bw": body.get("body_weight"),
            "bf": body.get("body_fat_pct"),
            "sore": body.get("soreness_level", 0),
            "sore_notes": body.get("soreness_notes", ""),
            "injury": body.get("injury_notes", ""),
            "nutr": body.get("nutrition_adherence", 0),
            "protein": body.get("protein_target_hit", False),
            "mobility": body.get("mobility_done", False),
            "notes": body.get("notes", ""),
        })
        await session.commit()

    # Award XP for check-in
    from app.services.villain_challenge.xp_engine import award_xp
    xp = await award_xp(25, "daily_checkin", category="checkin")

    return {"status": "checked_in", "xp": xp}


@router.get("/villain/battle-status")
async def villain_battle_status():
    """Get daily battle probability and status narrative."""
    from app.services.villain_challenge.villain_engine import get_active_challenge
    from app.services.villain_challenge.hero_engine import get_hero_profile
    from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
    from app.services.villain_challenge.narrative import generate_daily_update

    challenge = await get_active_challenge()
    if not challenge:
        return {"status": "No active challenge", "narrative": None}

    hero_data = await get_hero_profile()
    battle_status = await calculate_daily_battle_probability(challenge, hero_data)
    tone = challenge.get("narrative_tone", "shield_tactical")
    narrative = await generate_daily_update(battle_status, challenge, hero_data, tone=tone)

    return {"battle_status": battle_status, "narrative": narrative}


@router.get("/villain/history")
async def villain_history(limit: int = 10):
    """Get battle history."""
    from app.services.villain_challenge.battle_system import get_battle_history
    return {"battles": await get_battle_history(limit=limit)}


@router.get("/villain/nemesis")
async def villain_nemesis():
    """Get nemesis tracker list."""
    from app.services.villain_challenge.battle_system import get_nemesis_list
    return {"nemeses": await get_nemesis_list()}


@router.get("/villain/xp")
async def villain_xp():
    """Get XP summary and recent history."""
    from app.services.villain_challenge.xp_engine import (
        get_xp_summary, get_xp_history, get_active_surges,
    )
    summary = await get_xp_summary()
    history = await get_xp_history(limit=10)
    surges = await get_active_surges()
    return {"summary": summary, "history": history, "active_surges": surges}


@router.get("/villain/surges")
async def villain_surges():
    """Get active power surges."""
    from app.services.villain_challenge.xp_engine import get_active_surges
    return {"surges": await get_active_surges()}


@router.post("/villain/sync")
async def villain_sync():
    """Manually trigger objective progress sync from fitness data."""
    from app.services.villain_challenge.scheduler import _sync_objective_progress
    await _sync_objective_progress()
    from app.services.villain_challenge.villain_engine import get_active_challenge
    return await get_active_challenge()


# ── AEGIS Voice Interface ─────────────────────────────────────────────────────

@router.websocket("/voice/ws")
async def voice_ws(websocket: WebSocket):
    """WebSocket — pushes orb state changes to AEGIS display clients."""
    from app.services.voice_service import register_ws, unregister_ws, get_state_dict
    import json

    await websocket.accept()
    await register_ws(websocket)
    try:
        await websocket.send_text(json.dumps(get_state_dict()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except Exception:
        pass
    finally:
        await unregister_ws(websocket)


@router.get("/voice/state")
async def voice_state():
    """Get the current AEGIS voice session state."""
    from app.services.voice_service import get_state_dict
    return get_state_dict()


@router.post("/voice/transcribe")
async def voice_transcribe(request: Request):
    """Accept audio (multipart field 'audio'), return transcript text."""
    from app.services.voice_service import transcribe_audio, set_state, VoiceState
    form = await request.form()
    audio_file = form.get("audio")
    if not audio_file:
        raise HTTPException(400, "Missing 'audio' field")
    audio_bytes = await audio_file.read()
    await set_state(VoiceState.LISTENING)
    transcript = await transcribe_audio(audio_bytes)
    if not transcript:
        await set_state(VoiceState.SLEEPING)
        raise HTTPException(422, "Could not transcribe audio")
    return {"transcript": transcript}


@router.post("/voice/respond")
async def voice_respond(request: Request):
    """Accept transcript text, route through PAI skills, return response.

    Body: {"text": "...", "telegram": false}
    """
    from app.services.voice_service import (
        generate_voice_response, set_state, VoiceState, _forward_to_telegram,
    )
    body = await request.json()
    text = (body.get("text") or "").strip()
    telegram = bool(body.get("telegram", False))
    if not text:
        raise HTTPException(400, "Missing 'text' field")
    await set_state(VoiceState.THINKING)
    response_text = await generate_voice_response(
        text, http_client=request.app.state.http_client
    )
    if telegram:
        await _forward_to_telegram(text, response_text)
    await set_state(VoiceState.RESPONDING)
    return {"response": response_text}


@router.post("/voice/tts")
async def voice_tts(request: Request):
    """Convert text to WAV audio. Body: {"text": "..."}"""
    from app.services.voice_service import synthesize_speech
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Missing 'text' field")
    audio = await synthesize_speech(text)
    if audio is None:
        raise HTTPException(503, "TTS backend not available")
    return StreamingResponse(io.BytesIO(audio), media_type="audio/wav")


@router.post("/voice/turn")
async def voice_turn(request: Request):
    """One complete voice turn: audio/text → STT → skills → TTS.

    Multipart fields: audio (file, optional), text (str, optional), telegram ("1"/"0")
    Returns: {transcript, response_text, audio_b64}
    """
    from app.services.voice_service import process_voice_turn
    form = await request.form()
    audio_file = form.get("audio")
    text_input = form.get("text") or None
    telegram = form.get("telegram", "0") == "1"
    audio_bytes = await audio_file.read() if audio_file else None
    if not audio_bytes and not text_input:
        raise HTTPException(400, "Provide 'audio' file or 'text' field")
    return await process_voice_turn(
        audio_bytes=audio_bytes,
        text_input=text_input,
        http_client=request.app.state.http_client,
        telegram_forward=telegram,
    )


@router.post("/voice/sleep")
async def voice_sleep():
    """Force AEGIS back to sleeping state."""
    from app.services.voice_service import set_state, VoiceState
    await set_state(VoiceState.SLEEPING)
    return {"state": "sleeping"}


@router.post("/voice/wake")
async def voice_wake():
    """Force AEGIS into listening state (for hardware wake-word triggers)."""
    from app.services.voice_service import set_state, VoiceState
    await set_state(VoiceState.LISTENING)
    return {"state": "listening"}
