"""Content ranking service for scoring and ordering discovered articles."""

import logging
import re
from datetime import datetime

from app.services.web_search_service import SearchResult

logger = logging.getLogger("pai.services.content_ranker")

# Trusted sources for AI + cybersecurity convergence
HIGH_CREDIBILITY_SOURCES = {
    "arxiv.org", "nist.gov", "cisa.gov", "ieee.org", "acm.org",
    "darkreading.com", "krebsonsecurity.com", "schneier.com",
    "therecord.media", "bleepingcomputer.com", "securityweek.com",
    "csoonline.com", "wired.com", "arstechnica.com", "technologyreview.com",
    "thehackernews.com", "threatpost.com", "infosecurity-magazine.com",
    "nature.com", "sciencedirect.com", "springer.com",
    "openai.com", "anthropic.com", "deepmind.com",
    "whitehouse.gov", "congress.gov", "gao.gov",
}

MEDIUM_CREDIBILITY_SOURCES = {
    "medium.com", "substack.com", "forbes.com", "zdnet.com",
    "techcrunch.com", "venturebeat.com", "theverge.com",
    "helpnetsecurity.com", "cybersecuritydive.com",
}

# Keyword relevance signals for AI + cybersecurity convergence
CONVERGENCE_KEYWORDS = [
    "ai cybersecurity", "artificial intelligence security", "machine learning threat",
    "llm security", "ai governance", "ai risk", "adversarial ai", "ai compliance",
    "ai regulation", "automated threat", "ai-powered", "generative ai security",
    "deepfake", "ai vulnerability", "ai red team", "ai audit",
    "nist ai rmf", "ai supply chain", "ai incident", "ai ethics",
    "autonomous cyber", "ai soc", "ai detection", "ai defense",
    "prompt injection", "model security", "ai policy", "ai executive order",
]


class ArticleScore:
    __slots__ = (
        "relevance", "depth", "source_credibility", "recency",
        "total", "breakdown",
    )

    def __init__(self):
        self.relevance = 0.0
        self.depth = 0.0
        self.source_credibility = 0.0
        self.recency = 0.0
        self.total = 0.0
        self.breakdown: dict = {}

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 3),
            "relevance": round(self.relevance, 3),
            "depth": round(self.depth, 3),
            "source_credibility": round(self.source_credibility, 3),
            "recency": round(self.recency, 3),
        }


# Weights for each scoring dimension
WEIGHTS = {
    "relevance": 0.40,
    "depth": 0.20,
    "source_credibility": 0.25,
    "recency": 0.15,
}


def score_article(result: SearchResult, query: str = "") -> ArticleScore:
    """Score a single article across multiple dimensions."""
    score = ArticleScore()

    text = (result.title + " " + result.snippet + " " + result.body).lower()

    # ── Relevance (0-1) ──
    keyword_hits = sum(1 for kw in CONVERGENCE_KEYWORDS if kw in text)
    keyword_ratio = min(keyword_hits / 8.0, 1.0)  # 8+ keywords = max score

    query_terms = query.lower().split() if query else []
    query_hits = sum(1 for term in query_terms if term in text) if query_terms else 0
    query_ratio = min(query_hits / max(len(query_terms), 1), 1.0)

    score.relevance = (keyword_ratio * 0.6) + (query_ratio * 0.4)

    # ── Depth (0-1) ──
    body_len = len(result.body)
    if body_len > 3000:
        score.depth = 1.0
    elif body_len > 1500:
        score.depth = 0.75
    elif body_len > 500:
        score.depth = 0.5
    elif result.snippet:
        score.depth = 0.25
    else:
        score.depth = 0.1

    # ── Source Credibility (0-1) ──
    domain = result.source.lower()
    if domain in HIGH_CREDIBILITY_SOURCES:
        score.source_credibility = 1.0
    elif domain in MEDIUM_CREDIBILITY_SOURCES:
        score.source_credibility = 0.6
    elif any(tld in domain for tld in [".gov", ".edu", ".mil"]):
        score.source_credibility = 0.9
    else:
        score.source_credibility = 0.3

    # ── Recency (0-1) ──
    # Try to detect date signals; default to neutral if unavailable
    if result.published:
        score.recency = _score_date(result.published)
    else:
        # Check for date patterns in snippet
        date_match = re.search(r'(\d{4})', result.snippet)
        if date_match:
            year = int(date_match.group(1))
            current_year = datetime.now().year
            if year == current_year:
                score.recency = 0.9
            elif year == current_year - 1:
                score.recency = 0.6
            else:
                score.recency = 0.3
        else:
            score.recency = 0.5  # neutral default

    # ── Weighted total ──
    score.total = (
        score.relevance * WEIGHTS["relevance"]
        + score.depth * WEIGHTS["depth"]
        + score.source_credibility * WEIGHTS["source_credibility"]
        + score.recency * WEIGHTS["recency"]
    )

    return score


def rank_articles(
    results: list[SearchResult],
    query: str = "",
    min_score: float = 0.0,
) -> list[tuple[SearchResult, ArticleScore]]:
    """Score and rank articles by total score descending. Optionally filter by min_score."""
    scored = [(r, score_article(r, query)) for r in results]
    scored.sort(key=lambda x: x[1].total, reverse=True)

    if min_score > 0:
        scored = [(r, s) for r, s in scored if s.total >= min_score]

    return scored


def _score_date(date_str: str) -> float:
    """Score a date string based on recency."""
    try:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y", "%Y"):
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                days_ago = (datetime.now() - dt).days
                if days_ago <= 7:
                    return 1.0
                elif days_ago <= 30:
                    return 0.85
                elif days_ago <= 90:
                    return 0.7
                elif days_ago <= 365:
                    return 0.5
                else:
                    return 0.2
            except ValueError:
                continue
    except Exception:
        pass
    return 0.5
