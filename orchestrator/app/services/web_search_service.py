"""Web search service using DuckDuckGo for article discovery."""

import logging
from datetime import datetime

import httpx
import trafilatura
from duckduckgo_search import DDGS

logger = logging.getLogger("pai.services.web_search")


class SearchResult:
    __slots__ = ("title", "url", "snippet", "body", "published", "source")

    def __init__(self, title: str, url: str, snippet: str, body: str = "",
                 published: str = "", source: str = ""):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.body = body
        self.published = published
        self.source = source

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "body": self.body[:2000] if self.body else "",
            "published": self.published,
            "source": self.source,
        }


async def search_web(
    query: str,
    max_results: int = 10,
    time_filter: str = "m",  # d=day, w=week, m=month, y=year
) -> list[SearchResult]:
    """
    Search DuckDuckGo for articles matching the query.
    Returns SearchResult objects with title, url, snippet.
    """
    results = []
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results, timelimit=time_filter))
            for hit in hits:
                results.append(SearchResult(
                    title=hit.get("title", ""),
                    url=hit.get("href", ""),
                    snippet=hit.get("body", ""),
                    source=_extract_domain(hit.get("href", "")),
                ))
    except Exception as e:
        logger.error("ddg_search_failed", extra={"error": str(e), "query": query})

    return results


async def fetch_article_content(
    url: str,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch and extract clean article text from a URL using trafilatura."""
    try:
        if http_client:
            resp = await http_client.get(url, follow_redirects=True, timeout=15.0)
            html = resp.text
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, follow_redirects=True)
                html = resp.text

        extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
        return extracted or ""
    except Exception as e:
        logger.warning("article_fetch_failed", extra={"url": url, "error": str(e)})
        return ""


async def search_and_extract(
    query: str,
    max_results: int = 10,
    time_filter: str = "m",
    http_client: httpx.AsyncClient | None = None,
    extract_bodies: bool = True,
    max_extract: int = 5,
) -> list[SearchResult]:
    """
    Search for articles and optionally extract full body text from top results.
    """
    results = await search_web(query, max_results=max_results, time_filter=time_filter)

    if extract_bodies:
        for result in results[:max_extract]:
            body = await fetch_article_content(result.url, http_client=http_client)
            result.body = body

    return results


def _extract_domain(url: str) -> str:
    """Extract domain name from URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except Exception:
        return ""
