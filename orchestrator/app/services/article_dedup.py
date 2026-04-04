"""Article deduplication service — tracks seen articles to prevent repeats."""

import hashlib
import logging

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.services.article_dedup")


def _url_hash(url: str) -> str:
    """Create a stable hash of a URL for dedup lookups."""
    normalized = url.strip().lower().rstrip("/")
    return hashlib.sha256(normalized.encode()).hexdigest()


async def is_article_seen(url: str) -> bool:
    """Check if an article URL has already been processed."""
    url_h = _url_hash(url)
    async with async_session() as session:
        result = await session.execute(
            text("SELECT 1 FROM article_ledger WHERE url_hash = :h LIMIT 1"),
            {"h": url_h},
        )
        return result.scalar() is not None


async def mark_article_seen(
    url: str,
    title: str = "",
    source: str = "",
    topic: str = "",
    score: float = 0.0,
) -> bool:
    """Record an article in the ledger. Returns True if newly inserted, False if duplicate."""
    url_h = _url_hash(url)
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO article_ledger (url_hash, url, title, source, topic, score) "
                "VALUES (:h, :url, :title, :source, :topic, :score) "
                "ON CONFLICT (url_hash) DO NOTHING "
                "RETURNING id"
            ),
            {
                "h": url_h,
                "url": url,
                "title": title,
                "source": source,
                "topic": topic,
                "score": score,
            },
        )
        row = result.scalar()
        await session.commit()
        is_new = row is not None
        if is_new:
            logger.info("article_new", extra={"url": url, "title": title})
        else:
            logger.debug("article_duplicate", extra={"url": url})
        return is_new


async def filter_new_articles(urls: list[str]) -> set[str]:
    """Given a list of URLs, return the set that have NOT been seen before."""
    if not urls:
        return set()

    hashes = {_url_hash(u): u for u in urls}
    hash_list = list(hashes.keys())

    async with async_session() as session:
        # Check which hashes already exist
        placeholders = ", ".join(f":h{i}" for i in range(len(hash_list)))
        params = {f"h{i}": h for i, h in enumerate(hash_list)}

        result = await session.execute(
            text(f"SELECT url_hash FROM article_ledger WHERE url_hash IN ({placeholders})"),
            params,
        )
        seen_hashes = {row[0] for row in result}

    # Return URLs whose hashes are NOT in the DB
    return {url for h, url in hashes.items() if h not in seen_hashes}


async def get_ledger_stats() -> dict:
    """Get summary stats from the article ledger."""
    async with async_session() as session:
        total = await session.execute(text("SELECT COUNT(*) FROM article_ledger"))
        recent = await session.execute(
            text("SELECT COUNT(*) FROM article_ledger WHERE discovered_at > NOW() - INTERVAL '7 days'")
        )
        return {
            "total_articles": total.scalar(),
            "articles_last_7_days": recent.scalar(),
        }
