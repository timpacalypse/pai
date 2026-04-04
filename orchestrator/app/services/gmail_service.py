"""Gmail notification service — sends research digest emails via SMTP."""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger("pai.services.gmail")


async def send_research_digest(
    articles: list[dict],
    topic: str,
    new_count: int,
    total_found: int,
    ingested_count: int = 0,
) -> bool:
    """
    Send a formatted research digest email via Gmail SMTP.

    articles: list of dicts with title, url, snippet, source, score
    Returns True on success, False on failure.
    """
    if not settings.gmail_address or not settings.gmail_app_password:
        logger.warning("gmail_not_configured", extra={"topic": topic})
        return False

    if not articles:
        logger.info("no_articles_to_send", extra={"topic": topic})
        return False

    subject = f"PAI Research Digest: {topic} ({new_count} new articles)"
    html_body = _build_digest_html(articles, topic, new_count, total_found, ingested_count)
    text_body = _build_digest_text(articles, topic, new_count, total_found, ingested_count)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"PAI Orchestrator <{settings.gmail_address}>"
        msg["To"] = settings.gmail_recipient or settings.gmail_address

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)

        logger.info(
            "digest_sent",
            extra={"topic": topic, "articles": len(articles), "to": msg["To"]},
        )
        return True

    except Exception as e:
        logger.error("digest_send_failed", extra={"error": str(e), "topic": topic})
        return False


def _build_digest_html(
    articles: list[dict], topic: str, new_count: int,
    total_found: int, ingested_count: int,
) -> str:
    """Build an HTML email body for the research digest."""
    now = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

    rows = ""
    for i, a in enumerate(articles, 1):
        score = a.get("score", {})
        total = score.get("total", 0) if isinstance(score, dict) else 0
        source = a.get("source", "")
        snippet = a.get("snippet", "")[:200]

        # Score color
        if total >= 0.7:
            badge_color = "#4caf50"
        elif total >= 0.4:
            badge_color = "#ff9800"
        else:
            badge_color = "#9e9e9e"

        rows += f"""
        <tr style="border-bottom: 1px solid #2a2d42;">
            <td style="padding: 12px; vertical-align: top; width: 40px; color: #8b8fa8;">
                {i}.
            </td>
            <td style="padding: 12px;">
                <div style="margin-bottom: 4px;">
                    <span style="background: {badge_color}; color: #fff; padding: 2px 8px;
                                 border-radius: 4px; font-size: 12px; font-weight: 600;">
                        {total:.2f}
                    </span>
                    <span style="color: #8b8fa8; font-size: 12px; margin-left: 8px;">
                        {source}
                    </span>
                </div>
                <a href="{a.get('url', '#')}" style="color: #4f8ef7; text-decoration: none;
                          font-weight: 600; font-size: 15px;">
                    {a.get('title', 'Untitled')}
                </a>
                <p style="color: #b0b3c8; font-size: 13px; margin: 6px 0 0 0; line-height: 1.5;">
                    {snippet}
                </p>
            </td>
        </tr>"""

    return f"""
    <html>
    <body style="background: #0f1117; font-family: 'Segoe UI', system-ui, sans-serif;
                 margin: 0; padding: 20px;">
        <div style="max-width: 700px; margin: 0 auto; background: #161822;
                    border-radius: 12px; overflow: hidden; border: 1px solid #2a2d42;">
            <div style="background: #1a1d2e; padding: 24px 30px; border-bottom: 1px solid #2a2d42;">
                <h1 style="color: #4f8ef7; margin: 0; font-size: 22px;">
                    PAI Research Digest
                </h1>
                <p style="color: #8b8fa8; margin: 6px 0 0 0; font-size: 13px;">
                    {now}
                </p>
            </div>

            <div style="padding: 20px 30px;">
                <div style="background: #222539; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
                    <p style="color: #e4e6f0; margin: 0; font-size: 14px;">
                        <strong>Topic:</strong> {topic}<br>
                        <strong>New articles:</strong> {new_count} of {total_found} found<br>
                        <strong>Ingested to memory:</strong> {ingested_count}
                    </p>
                </div>

                <table style="width: 100%; border-collapse: collapse;">
                    {rows}
                </table>
            </div>

            <div style="padding: 16px 30px; background: #1a1d2e; border-top: 1px solid #2a2d42;">
                <p style="color: #5c6078; font-size: 11px; margin: 0; text-align: center;">
                    Sent by PAI Orchestrator &middot; Automated AI + Cybersecurity Research
                </p>
            </div>
        </div>
    </body>
    </html>
    """


def _build_digest_text(
    articles: list[dict], topic: str, new_count: int,
    total_found: int, ingested_count: int,
) -> str:
    """Build a plain text fallback for the research digest."""
    now = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    lines = [
        f"PAI Research Digest — {now}",
        f"Topic: {topic}",
        f"New articles: {new_count} of {total_found} found",
        f"Ingested to memory: {ingested_count}",
        "",
        "=" * 60,
    ]

    for i, a in enumerate(articles, 1):
        score = a.get("score", {})
        total = score.get("total", 0) if isinstance(score, dict) else 0
        lines.append(f"\n{i}. [{total:.2f}] {a.get('title', 'Untitled')}")
        lines.append(f"   {a.get('url', '')}")
        lines.append(f"   Source: {a.get('source', '')}")
        snippet = a.get("snippet", "")[:150]
        if snippet:
            lines.append(f"   {snippet}")

    lines.append("\n" + "=" * 60)
    lines.append("Sent by PAI Orchestrator")
    return "\n".join(lines)
