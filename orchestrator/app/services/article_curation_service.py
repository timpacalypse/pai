"""Article Curation — fetches articles from web/RSS, scores for relevance,
and delivers top articles with explanations. Supports LinkedIn content pipeline."""

import logging
from datetime import datetime, timezone

import httpx

from app.services.web_search_service import search_and_extract
from app.services.content_ranker import rank_articles
from app.services.article_dedup import filter_new_articles, mark_article_seen

logger = logging.getLogger("pai.services.article_curation")

# Default curation topics — AI + cybersecurity convergence + thought leadership
DEFAULT_CURATION_TOPICS = [
    "AI cybersecurity convergence latest developments",
    "generative AI enterprise security risks",
    "AI governance policy regulation updates",
    "CISO leadership AI strategy",
    "cybersecurity workforce AI transformation",
]


async def curate_articles(
    topics: list[str] | None = None,
    max_per_topic: int = 8,
    top_n: int = 10,
    min_score: float = 0.2,
    dedup: bool = True,
) -> dict:
    """
    Curate articles across topics:
    1. Search across all topics
    2. Deduplicate against ledger
    3. Score and rank
    4. Return top N with relevance explanations
    """
    topics = topics or DEFAULT_CURATION_TOPICS
    start = datetime.now(timezone.utc)

    all_results = []
    topic_stats = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for topic in topics:
            try:
                results = await search_and_extract(
                    query=topic,
                    max_results=max_per_topic,
                    time_filter="w",
                    http_client=client,
                    extract_bodies=True,
                    max_extract=3,
                )
                topic_stats[topic] = {"found": len(results)}

                if dedup and results:
                    urls = [r.url for r in results if r.url]
                    new_urls = await filter_new_articles(urls)
                    results = [r for r in results if r.url in new_urls]
                    topic_stats[topic]["new"] = len(results)

                for r in results:
                    all_results.append((r, topic))

            except Exception as e:
                logger.error("curation_topic_failed", extra={"topic": topic, "error": str(e)})
                topic_stats[topic] = {"found": 0, "error": str(e)}

    # Score and rank
    scored = []
    for result, topic in all_results:
        ranked = rank_articles([result], query=topic, min_score=min_score)
        if ranked:
            scored.append((ranked[0][0], ranked[0][1], topic))

    # Sort by score descending
    scored.sort(key=lambda x: x[1].total, reverse=True)
    top_articles = scored[:top_n]

    # Mark as seen in ledger
    for result, score, topic in top_articles:
        try:
            await mark_article_seen(
                url=result.url,
                title=result.title,
                source=result.source,
                topic=topic,
                score=score.total,
            )
        except Exception as e:
            logger.warning("mark_seen_failed", extra={"url": result.url, "error": str(e)})

    curated = []
    for result, score, topic in top_articles:
        curated.append({
            "title": result.title,
            "url": result.url,
            "source": result.source,
            "snippet": result.snippet,
            "topic": topic,
            "score": score.to_dict(),
            "relevance_explanation": _explain_relevance(result, score, topic),
        })

    duration_s = (datetime.now(timezone.utc) - start).total_seconds()
    return {
        "curated_at": start.isoformat(),
        "duration_seconds": round(duration_s, 1),
        "total_found": sum(s.get("found", 0) for s in topic_stats.values()),
        "total_curated": len(curated),
        "topic_stats": topic_stats,
        "articles": curated,
    }


async def curate_articles_text(
    topics: list[str] | None = None,
    top_n: int = 10,
) -> str:
    """Return a formatted text report of curated articles."""
    result = await curate_articles(topics=topics, top_n=top_n)

    if not result["articles"]:
        return "No new articles found matching the curation criteria."

    lines = [
        f"═══ Article Curation Report ═══",
        f"Found {result['total_found']} articles, curated top {result['total_curated']}",
        "",
    ]

    for i, article in enumerate(result["articles"], 1):
        score = article["score"]["total"]
        lines.append(f"  {i}. [{score:.0%}] {article['title']}")
        lines.append(f"     Source: {article['source']} | Topic: {article['topic']}")
        lines.append(f"     {article['url']}")
        lines.append(f"     Why: {article['relevance_explanation']}")
        if article.get("snippet"):
            lines.append(f"     {article['snippet'][:150]}...")
        lines.append("")

    lines.append(f"── LinkedIn Content Candidates ──")
    # Top 3 by score are best for thought leadership
    linkedin_picks = result["articles"][:3]
    for i, article in enumerate(linkedin_picks, 1):
        lines.append(f"  {i}. {article['title']}")
        lines.append(f"     Angle: {_suggest_linkedin_angle(article)}")
    lines.append("")

    return "\n".join(lines)


def _explain_relevance(result, score, topic: str) -> str:
    """Generate a brief explanation of why this article scored high."""
    parts = []
    s = score

    if s.relevance >= 0.7:
        parts.append("highly relevant to convergence themes")
    elif s.relevance >= 0.4:
        parts.append("moderately relevant")

    if s.source_credibility >= 0.8:
        parts.append(f"trusted source ({result.source})")
    elif s.source_credibility >= 0.5:
        parts.append("credible source")

    if s.depth >= 0.75:
        parts.append("in-depth analysis")
    elif s.depth >= 0.5:
        parts.append("good depth")

    if s.recency >= 0.8:
        parts.append("very recent")

    if not parts:
        parts.append(f"matched topic: {topic}")

    return "; ".join(parts)


def _suggest_linkedin_angle(article: dict) -> str:
    """Suggest a LinkedIn thought leadership angle for an article."""
    topic = article.get("topic", "").lower()
    title = article.get("title", "").lower()

    if "governance" in topic or "policy" in topic or "regulation" in title:
        return "Share your perspective on how this regulation affects enterprise security programs"
    elif "ciso" in topic or "leadership" in topic:
        return "Relate this to your experience leading cybersecurity transformation"
    elif "workforce" in topic or "talent" in topic:
        return "Discuss implications for building AI-ready security teams"
    elif "threat" in topic or "attack" in topic or "vulnerability" in title:
        return "Provide actionable takeaways for security practitioners"
    else:
        return "Connect this to the AI + cybersecurity convergence narrative"
