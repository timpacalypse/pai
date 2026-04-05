"""Daily briefing service — assembles weather, articles, calendar, and email recommendations."""

import imaplib
import email
import json
import logging
from datetime import datetime, timezone
from email.header import decode_header

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.briefing")


async def build_daily_briefing(http_client: httpx.AsyncClient | None = None) -> dict:
    """Assemble all sections of the daily briefing."""
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=30.0)

    weather = await _get_weather(http_client)
    articles = await _get_top_articles(limit=8)
    agenda = await _get_today_agenda()
    email_recs = await _get_email_recommendations(http_client)

    return {
        "weather": weather,
        "articles": articles,
        "agenda": agenda,
        "email_recommendations": email_recs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _get_weather(http_client: httpx.AsyncClient) -> dict:
    """Fetch weather from Open-Meteo (free, no API key)."""
    lat = settings.weather_lat
    lon = settings.weather_lon
    if not lat or not lon:
        return {"error": "Weather location not configured (WEATHER_LAT/WEATHER_LON)"}

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        f"precipitation,weather_code,wind_speed_10m"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,precipitation_probability_max"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
        f"&precipitation_unit=inch&timezone=auto&forecast_days=3"
    )

    try:
        resp = await http_client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current", {})
        daily = data.get("daily", {})

        # Map WMO weather codes to descriptions
        wmo = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Depositing rime fog",
            51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
            61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
            80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
            95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
        }

        forecast_days = []
        if daily.get("time"):
            for i in range(min(3, len(daily["time"]))):
                forecast_days.append({
                    "date": daily["time"][i],
                    "high": daily.get("temperature_2m_max", [None])[i],
                    "low": daily.get("temperature_2m_min", [None])[i],
                    "precip_chance": daily.get("precipitation_probability_max", [0])[i],
                    "precip_total": daily.get("precipitation_sum", [0])[i],
                    "condition": wmo.get(daily.get("weather_code", [0])[i], "Unknown"),
                })

        return {
            "current_temp": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "condition": wmo.get(current.get("weather_code", 0), "Unknown"),
            "precipitation": current.get("precipitation"),
            "forecast": forecast_days,
        }
    except Exception as e:
        logger.error("weather_fetch_failed", extra={"error": str(e)})
        return {"error": f"Weather unavailable: {e}"}


async def _get_top_articles(limit: int = 8) -> list[dict]:
    """Get top recent articles from article_ledger (last 24h, by score)."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT title, url, source, topic, score "
                "FROM article_ledger "
                "WHERE discovered_at > NOW() - INTERVAL '24 hours' "
                "ORDER BY score DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(r) for r in result.mappings()]


async def _get_today_agenda() -> dict:
    """Get upcoming events from calendar_service (next 7 days)."""
    from app.services.calendar_service import get_agenda
    return await get_agenda(days=7)


def _read_gmail_inbox(max_emails: int = 15) -> list[dict]:
    """Read recent Gmail inbox via IMAP. Returns list of {subject, from, date, snippet}."""
    if not settings.gmail_address or not settings.gmail_app_password:
        return []

    emails = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(settings.gmail_address, settings.gmail_app_password)
        mail.select("INBOX", readonly=True)

        # Search for recent emails (last 2 days to handle timezone gaps)
        from datetime import timedelta
        since_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
        _, msg_ids = mail.search(None, f'(SINCE "{since_date}")')
        id_list = msg_ids[0].split()

        # Take the most recent N
        for msg_id in id_list[-max_emails:]:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject = ""
            raw_subject = msg["Subject"]
            if raw_subject:
                decoded_parts = decode_header(raw_subject)
                subject = "".join(
                    part.decode(enc or "utf-8") if isinstance(part, bytes) else part
                    for part, enc in decoded_parts
                )

            from_addr = msg.get("From", "")
            date_str = msg.get("Date", "")

            # Get plain text body snippet
            snippet = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            snippet = payload.decode("utf-8", errors="replace")[:300]
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    snippet = payload.decode("utf-8", errors="replace")[:300]

            emails.append({
                "subject": subject,
                "from": from_addr,
                "date": date_str,
                "snippet": snippet.strip(),
            })

        mail.logout()
    except Exception as e:
        logger.error("gmail_imap_read_failed", extra={"error": str(e)})

    return emails


async def _get_email_recommendations(http_client: httpx.AsyncClient) -> dict:
    """Read Gmail inbox and use LLM to suggest scheduling actions."""
    inbox = _read_gmail_inbox(max_emails=15)
    if not inbox:
        return {"emails_scanned": 0, "recommendations": []}

    # Build a summary for the LLM
    email_summaries = []
    for e in inbox:
        email_summaries.append(
            f"From: {e['from']}\nSubject: {e['subject']}\nDate: {e['date']}\n"
            f"Preview: {e['snippet'][:200]}"
        )

    email_block = "\n---\n".join(email_summaries)
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    system_prompt = (
        "You analyze a user's recent emails and recommend things they should schedule or act on. "
        "Focus on: appointments to make, deadlines approaching, events to RSVP to, follow-ups needed, "
        "bills to pay, meetings to schedule. Ignore spam, newsletters, and marketing emails.\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"recommendations": [{"action": "what to schedule/do", "urgency": "high|medium|low", '
        '"source_subject": "which email this came from", "suggested_date": "when to do it or empty string"}]}'
    )

    user_prompt = (
        f"Today is {today}. Here are my recent emails:\n\n{email_block}\n\n"
        "What should I schedule or act on?"
    )

    try:
        raw = await generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            http_client=http_client,
        )
        parsed = _parse_json(raw)
        recs = parsed.get("recommendations", [])
    except Exception as e:
        logger.error("email_rec_generation_failed", extra={"error": str(e)})
        recs = []

    return {
        "emails_scanned": len(inbox),
        "recommendations": recs,
    }


def build_briefing_html(briefing: dict) -> str:
    """Render the daily briefing as a formatted HTML email."""
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    weather = briefing.get("weather", {})
    articles = briefing.get("articles", [])
    agenda = briefing.get("agenda", {})
    email_recs = briefing.get("email_recommendations", {})

    # Weather section
    if weather.get("error"):
        weather_html = f'<p style="color:#ff9800;">{weather["error"]}</p>'
    else:
        forecast_rows = ""
        for day in weather.get("forecast", []):
            forecast_rows += (
                f'<tr style="border-bottom:1px solid #2a2d42;">'
                f'<td style="padding:6px 12px;color:#e4e6f0;">{day["date"]}</td>'
                f'<td style="padding:6px 12px;color:#4caf50;">{day.get("high", "?")}°F</td>'
                f'<td style="padding:6px 12px;color:#4f8ef7;">{day.get("low", "?")}°F</td>'
                f'<td style="padding:6px 12px;color:#b0b3c8;">{day.get("condition", "")}</td>'
                f'<td style="padding:6px 12px;color:#ff9800;">{day.get("precip_chance", 0)}%</td>'
                f'</tr>'
            )
        weather_html = f"""
        <div style="background:#222539;border-radius:8px;padding:16px;margin-bottom:16px;">
            <h3 style="color:#4f8ef7;margin:0 0 8px;">Right Now</h3>
            <p style="color:#e4e6f0;margin:0;font-size:24px;font-weight:600;">
                {weather.get('current_temp', '?')}°F
                <span style="font-size:14px;color:#8b8fa8;font-weight:400;">
                    (feels like {weather.get('feels_like', '?')}°F)
                </span>
            </p>
            <p style="color:#b0b3c8;margin:4px 0 0;">
                {weather.get('condition', '')} · Humidity {weather.get('humidity', '?')}%
                · Wind {weather.get('wind_speed', '?')} mph
            </p>
        </div>
        <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
            <thead><tr style="color:#8b8fa8;font-size:12px;text-transform:uppercase;">
                <th style="text-align:left;padding:6px 12px;">Date</th>
                <th style="text-align:left;padding:6px 12px;">High</th>
                <th style="text-align:left;padding:6px 12px;">Low</th>
                <th style="text-align:left;padding:6px 12px;">Condition</th>
                <th style="text-align:left;padding:6px 12px;">Rain</th>
            </tr></thead>
            <tbody>{forecast_rows}</tbody>
        </table>"""

    # Articles section
    if articles:
        article_rows = ""
        for i, a in enumerate(articles, 1):
            score = a.get("score", 0)
            article_rows += (
                f'<tr style="border-bottom:1px solid #2a2d42;">'
                f'<td style="padding:8px 12px;color:#8b8fa8;width:30px;vertical-align:top;">{i}.</td>'
                f'<td style="padding:8px 12px;">'
                f'<a href="{a.get("url", "#")}" style="color:#4f8ef7;text-decoration:none;font-weight:600;">'
                f'{a.get("title", "Untitled")}</a>'
                f'<br><span style="color:#8b8fa8;font-size:12px;">'
                f'{a.get("source", "")} · {a.get("topic", "")} · Score: {score:.2f}</span>'
                f'</td></tr>'
            )
        articles_html = (
            f'<table style="width:100%;border-collapse:collapse;">{article_rows}</table>'
        )
    else:
        articles_html = '<p style="color:#8b8fa8;">No new articles in the last 24 hours.</p>'

    # Calendar section
    agenda_events = agenda.get("agenda", {})
    if agenda.get("total_events", 0) > 0:
        cal_items = ""
        for date_str, events in agenda_events.items():
            cal_items += f'<h4 style="color:#4f8ef7;margin:10px 0 4px;font-size:13px;">{date_str}</h4>'
            for e in events:
                time_str = f" at {e['event_time']}" if e.get("event_time") else ""
                who = f" ({e['family_member_name']})" if e.get("family_member_name", "family") != "family" else ""
                loc = f" — {e['location']}" if e.get("location") else ""
                cal_items += (
                    f'<div style="padding:3px 0;color:#e4e6f0;font-size:13px;">'
                    f'• {e["title"]}{time_str}{who}{loc}</div>'
                )
        calendar_html = cal_items
    else:
        calendar_html = '<p style="color:#8b8fa8;">No events on your calendar today.</p>'

    # Email recommendations section
    recs = email_recs.get("recommendations", [])
    scanned = email_recs.get("emails_scanned", 0)
    if recs:
        rec_rows = ""
        for r in recs:
            urgency = r.get("urgency", "medium")
            urgency_color = {"high": "#f44336", "medium": "#ff9800", "low": "#4caf50"}.get(urgency, "#ff9800")
            rec_rows += (
                f'<div style="padding:8px 0;border-bottom:1px solid #2a2d42;">'
                f'<span style="background:{urgency_color};color:#fff;padding:2px 8px;'
                f'border-radius:4px;font-size:11px;font-weight:600;">{urgency.upper()}</span> '
                f'<span style="color:#e4e6f0;font-size:14px;">{r.get("action", "")}</span>'
                f'<br><span style="color:#8b8fa8;font-size:12px;">From: {r.get("source_subject", "")}'
            )
            if r.get("suggested_date"):
                rec_rows += f' · Suggested: {r["suggested_date"]}'
            rec_rows += '</span></div>'
        recs_html = f'<p style="color:#8b8fa8;font-size:12px;">Scanned {scanned} emails</p>{rec_rows}'
    else:
        recs_html = f'<p style="color:#8b8fa8;">No actionable items found ({scanned} emails scanned).</p>'

    return f"""
    <html>
    <body style="background:#0f1117;font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:20px;">
        <div style="max-width:700px;margin:0 auto;background:#161822;
                    border-radius:12px;overflow:hidden;border:1px solid #2a2d42;">
            <div style="background:#1a1d2e;padding:24px 30px;border-bottom:1px solid #2a2d42;">
                <h1 style="color:#4f8ef7;margin:0;font-size:22px;">
                    ☀ PAI Daily Briefing
                </h1>
                <p style="color:#8b8fa8;margin:6px 0 0;font-size:13px;">{now}</p>
            </div>

            <div style="padding:20px 30px;">
                <h2 style="color:#e4e6f0;font-size:16px;margin:0 0 12px;
                           border-bottom:1px solid #2a2d42;padding-bottom:8px;">
                    🌤 Weather
                </h2>
                {weather_html}

                <h2 style="color:#e4e6f0;font-size:16px;margin:20px 0 12px;
                           border-bottom:1px solid #2a2d42;padding-bottom:8px;">
                    📰 Top Articles
                </h2>
                {articles_html}

                <h2 style="color:#e4e6f0;font-size:16px;margin:20px 0 12px;
                           border-bottom:1px solid #2a2d42;padding-bottom:8px;">
                    📅 Today's Calendar
                </h2>
                {calendar_html}

                <h2 style="color:#e4e6f0;font-size:16px;margin:20px 0 12px;
                           border-bottom:1px solid #2a2d42;padding-bottom:8px;">
                    📧 From Your Inbox
                </h2>
                {recs_html}
            </div>

            <div style="padding:16px 30px;background:#1a1d2e;border-top:1px solid #2a2d42;">
                <p style="color:#5c6078;font-size:11px;margin:0;text-align:center;">
                    Sent by PAI Orchestrator &middot; Your Personal AI Daily Brief
                </p>
            </div>
        </div>
    </body>
    </html>
    """


def build_briefing_text(briefing: dict) -> str:
    """Build plain text fallback for the daily briefing."""
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    lines = [f"PAI Daily Briefing — {now}", "=" * 50, ""]

    # Weather
    weather = briefing.get("weather", {})
    if weather.get("error"):
        lines.append(f"WEATHER: {weather['error']}")
    else:
        lines.append(f"WEATHER: {weather.get('current_temp', '?')}°F "
                     f"(feels like {weather.get('feels_like', '?')}°F) — "
                     f"{weather.get('condition', '')}")
        for day in weather.get("forecast", []):
            lines.append(f"  {day['date']}: {day.get('low','?')}–{day.get('high','?')}°F, "
                         f"{day.get('condition','')}, {day.get('precip_chance',0)}% rain")

    lines.append("")

    # Articles
    articles = briefing.get("articles", [])
    lines.append(f"TOP ARTICLES ({len(articles)}):")
    for i, a in enumerate(articles, 1):
        source = a.get('source', '')
        score = a.get('score', 0)
        lines.append(f"  {i}. [{score:.2f}] {a.get('title', 'Untitled')}")
        lines.append(f"     Link: {a.get('url', '')}")
        if source:
            lines.append(f"     Source: {source}")
    lines.append("")

    # Calendar
    agenda = briefing.get("agenda", {})
    lines.append("CALENDAR:")
    for date_str, events in agenda.get("agenda", {}).items():
        lines.append(f"  {date_str}:")
        for e in events:
            time_str = f" at {e['event_time']}" if e.get("event_time") else ""
            lines.append(f"    • {e['title']}{time_str}")
    if not agenda.get("total_events"):
        lines.append("  No events today.")
    lines.append("")

    # Email recommendations
    recs = briefing.get("email_recommendations", {})
    lines.append(f"EMAIL ACTIONS ({recs.get('emails_scanned', 0)} emails scanned):")
    for r in recs.get("recommendations", []):
        lines.append(f"  [{r.get('urgency','').upper()}] {r.get('action', '')}")
        lines.append(f"    From: {r.get('source_subject', '')}")
    if not recs.get("recommendations"):
        lines.append("  No actionable items found.")

    lines.extend(["", "=" * 50, "Sent by PAI Orchestrator"])
    return "\n".join(lines)


async def send_daily_briefing(http_client: httpx.AsyncClient | None = None) -> bool:
    """Build and send the complete daily briefing email."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not settings.gmail_address or not settings.gmail_app_password:
        logger.warning("briefing_skip_no_gmail")
        return False

    briefing = await build_daily_briefing(http_client)
    html_body = build_briefing_html(briefing)
    text_body = build_briefing_text(briefing)

    now = datetime.now(timezone.utc).strftime("%B %d, %Y")
    subject = f"PAI Daily Briefing — {now}"

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

        logger.info("daily_briefing_sent", extra={"to": msg["To"]})
        return True
    except Exception as e:
        logger.error("daily_briefing_send_failed", extra={"error": str(e)})
        return False


def _parse_json(raw: str) -> dict:
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
