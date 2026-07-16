"""Scheduled research job — runs web research on cron, deduplicates, and sends Gmail digests."""

import asyncio
import json
import logging
from datetime import datetime

import httpx

from app.core.config import settings
from app.services.web_search_service import search_and_extract
from app.services.content_ranker import rank_articles
from app.services.article_dedup import mark_article_seen, filter_new_articles
from app.services.gmail_service import send_research_digest
from app.memory.semantic import store_semantic

logger = logging.getLogger("pai.scheduler")

# Default research topics for AI + cybersecurity convergence
DEFAULT_TOPICS = [
    "AI cybersecurity convergence latest news",
    "generative AI security threats and defenses",
    "NIST AI risk management framework updates",
    "AI governance policy regulation 2025 2026",
    "adversarial machine learning attacks defenses",
    "AI-powered SOC security operations automation",
    "LLM security prompt injection model attacks",
]


async def run_scheduled_research(
    topics: list[str] | None = None,
    max_results_per_topic: int = 10,
    time_filter: str = "w",
    min_score: float = 0.2,
    send_email: bool = True,
) -> dict:
    """
    Execute a full scheduled research cycle:
    1. Search multiple topics
    2. Deduplicate against the article ledger
    3. Score and rank new articles
    4. Ingest top new articles into semantic memory
    5. Send Gmail digest with findings

    Returns a summary dict.
    """
    topics = topics or DEFAULT_TOPICS
    start = datetime.utcnow()

    logger.info("scheduled_research_started", extra={"topics": len(topics)})

    all_new_articles = []
    total_found = 0
    total_duplicates = 0

    async with httpx.AsyncClient(timeout=20.0) as http_client:
        for topic in topics:
            try:
                # 1. Search and extract
                results = await search_and_extract(
                    query=topic,
                    max_results=max_results_per_topic,
                    time_filter=time_filter,
                    http_client=http_client,
                    extract_bodies=True,
                    max_extract=5,
                )
                total_found += len(results)

                if not results:
                    continue

                # 2. Deduplicate — check which URLs are new
                urls = [r.url for r in results if r.url]
                new_urls = await filter_new_articles(urls)

                new_results = [r for r in results if r.url in new_urls]
                total_duplicates += len(results) - len(new_results)

                if not new_results:
                    logger.info("no_new_articles", extra={"topic": topic})
                    continue

                # 3. Score and rank
                ranked = rank_articles(new_results, query=topic, min_score=min_score)

                for result, score in ranked:
                    # 4. Record in ledger
                    await mark_article_seen(
                        url=result.url,
                        title=result.title,
                        source=result.source,
                        topic=topic,
                        score=score.total,
                    )

                    all_new_articles.append({
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "body_preview": result.body[:500] if result.body else "",
                        "source": result.source,
                        "topic": topic,
                        "score": score.to_dict(),
                    })

            except Exception as e:
                logger.error("topic_research_failed", extra={"topic": topic, "error": str(e)})
                continue

    # 5. Ingest top new articles into semantic memory
    ingested_count = 0
    # Take top 10 across all topics by score
    all_new_articles.sort(key=lambda a: a.get("score", {}).get("total", 0), reverse=True)

    async with httpx.AsyncClient(timeout=20.0) as http_client:
        for article in all_new_articles[:10]:
            try:
                content_to_store = f"[{article['title']}] ({article['url']})\n{article['snippet']}"
                if article.get("body_preview"):
                    content_to_store += f"\n\n{article['body_preview']}"
                row_id = await store_semantic(
                    content=content_to_store,
                    source=article["url"],
                    metadata={
                        "type": "scheduled_research",
                        "topic": article["topic"],
                        "title": article["title"],
                        "score": article["score"].get("total", 0),
                        "discovered_at": start.isoformat(),
                    },
                    http_client=http_client,
                )
                if row_id > 0:
                    ingested_count += 1
            except Exception as e:
                logger.warning("ingest_failed", extra={"url": article["url"], "error": str(e)})

    # 6. Send email digest
    email_sent = False
    if send_email and all_new_articles:
        email_sent = await send_research_digest(
            articles=all_new_articles,
            topic="AI + Cybersecurity Convergence",
            new_count=len(all_new_articles),
            total_found=total_found,
            ingested_count=ingested_count,
        )

    summary = {
        "started_at": start.isoformat(),
        "topics_searched": len(topics),
        "total_found": total_found,
        "duplicates_filtered": total_duplicates,
        "new_articles": len(all_new_articles),
        "ingested_to_memory": ingested_count,
        "email_sent": email_sent,
    }

    logger.info("scheduled_research_completed", extra=summary)
    return summary


async def scheduler_loop():
    """
    Background scheduler loop. Runs research at configured intervals.
    Designed to run as a long-lived asyncio task inside the orchestrator.
    """
    interval_hours = settings.research_schedule_hours
    if interval_hours <= 0:
        logger.info("scheduler_disabled", extra={"interval_hours": interval_hours})
        return

    logger.info("scheduler_started", extra={"interval_hours": interval_hours})

    # Delay first run so the server is responsive on startup
    await asyncio.sleep(60)

    while True:
        try:
            await run_scheduled_research(
                topics=_parse_topics(),
                send_email=bool(settings.gmail_address),
            )
        except Exception as e:
            logger.error("scheduler_run_failed", extra={"error": str(e)})

        # Sleep until next run
        await asyncio.sleep(interval_hours * 3600)


def _parse_topics() -> list[str]:
    """Parse configured topics or fall back to defaults."""
    raw = settings.research_topics
    if raw:
        return [t.strip() for t in raw.split("|") if t.strip()]
    return DEFAULT_TOPICS
