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
    workout = await _get_todays_workout()
    villain = await _get_villain_challenge_status()

    return {
        "weather": weather,
        "articles": articles,
        "agenda": agenda,
        "email_recommendations": email_recs,
        "workout": workout,
        "villain_challenge": villain,
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


async def _get_todays_workout() -> dict:
    """Get today's scheduled workout and completed activities."""
    try:
        from app.services.workout_service import get_todays_workout
        return await get_todays_workout()
    except Exception as e:
        logger.warning("workout_fetch_failed", extra={"error": str(e)})
        return {"day": "", "scheduled": [], "completed": []}


async def _get_villain_challenge_status() -> dict:
    """Get current villain challenge status for the briefing."""
    today = datetime.now(timezone.utc)
    is_monday = today.weekday() == 0

    result = {
        "is_monday": is_monday,
        "has_challenge": False,
        "hero": None,
        "challenge": None,
        "battle_status": None,
        "weekly_assignment": None,
    }

    try:
        from app.services.villain_challenge.hero_engine import get_hero_profile
        from app.services.villain_challenge.villain_engine import get_active_challenge
        from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
        from app.services.villain_challenge.xp_engine import get_active_surges

        hero_data = await get_hero_profile()
        result["hero"] = {
            "hci": hero_data.get("hci", 0),
            "tier": hero_data.get("tier", "Street Level"),
            "archetype": hero_data.get("archetype", {}).get("name", "Recruit"),
            "level": hero_data.get("profile", {}).get("level", 1),
            "total_xp": hero_data.get("profile", {}).get("total_xp", 0),
            "domain_scores": hero_data.get("domain_scores", {}),
            "weakest": hero_data.get("weakest_domain", {}),
            "strongest": hero_data.get("strongest_domain", {}),
        }

        challenge = await get_active_challenge()
        if challenge:
            result["has_challenge"] = True
            result["challenge"] = {
                "villain_name": challenge.get("villain_name", ""),
                "villain_id": challenge.get("villain_id", ""),
                "difficulty": challenge.get("difficulty_rating", 0),
                "villain_hci": challenge.get("villain_hci", 0),
                "domain_focus": challenge.get("domain_focus", []),
                "week_start": str(challenge.get("week_start", "")),
                "week_end": str(challenge.get("week_end", "")),
                "objectives": [
                    {
                        "description": o.get("description", ""),
                        "current": o.get("current_value", 0),
                        "target": o.get("target_value", 0),
                        "completed": o.get("completed", False),
                        "domain": o.get("domain", ""),
                    }
                    for o in challenge.get("objectives", [])
                ],
                "completion_pct": challenge.get("completion_pct", 0),
            }

            battle_status = await calculate_daily_battle_probability(challenge, hero_data)
            result["battle_status"] = battle_status

            # On Monday, flag this as the weekly assignment
            if is_monday:
                from app.services.villain_challenge.models import get_villain
                villain = get_villain(challenge.get("villain_id", ""))
                result["weekly_assignment"] = {
                    "villain_name": challenge.get("villain_name", ""),
                    "tier": villain.tier if villain else "",
                    "description": villain.description if villain else "",
                    "weakness": villain.weakness_text if villain else "",
                    "domain_focus": challenge.get("domain_focus", []),
                    "objectives": result["challenge"]["objectives"],
                }

        surges = await get_active_surges()
        result["active_surges"] = surges

    except Exception as e:
        logger.warning("villain_briefing_fetch_failed", extra={"error": str(e)})

    return result


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
    workout = briefing.get("workout", {})
    villain = briefing.get("villain_challenge", {})

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

    # Workout section
    scheduled = workout.get("scheduled", [])
    completed = workout.get("completed", [])
    if scheduled or completed:
        workout_items = ""
        for s in scheduled:
            # Check if already completed
            done = any(c.get("activity", "").lower() == s.get("activity", "").lower() for c in completed)
            icon = "✅" if done else "🏋️"
            color = "#4caf50" if done else "#e4e6f0"
            # Build exercise detail HTML from notes
            notes_detail = ""
            notes_text = s.get("notes", "")
            if notes_text and "\n" in notes_text:
                detail_lines = []
                for line in notes_text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("- "):
                        detail_lines.append(
                            f'<div style="color:#b0b3c8;font-size:12px;padding:1px 0 1px 16px;">'
                            f'• {line[2:]}</div>'
                        )
                    elif line.endswith(":") or line.isupper():
                        detail_lines.append(
                            f'<div style="color:#4f8ef7;font-size:11px;font-weight:600;'
                            f'margin-top:4px;text-transform:uppercase;">{line}</div>'
                        )
                    else:
                        detail_lines.append(
                            f'<div style="color:#b0b3c8;font-size:12px;padding:1px 0 1px 16px;">'
                            f'{line}</div>'
                        )
                notes_detail = (
                    f'<div style="margin:4px 0 8px 24px;border-left:2px solid #2a2d42;padding-left:10px;">'
                    + "\n".join(detail_lines)
                    + '</div>'
                )
            elif notes_text:
                notes_detail = (
                    f'<div style="color:#8b8fa8;font-size:12px;margin:2px 0 6px 24px;">'
                    f'{notes_text}</div>'
                )
            workout_items += (
                f'<div style="padding:6px 0;color:{color};font-size:14px;font-weight:600;">'
                f'{icon} {s["activity"]} — {s["duration_minutes"]} min'
                f'</div>'
                f'{notes_detail}'
            )
        # Show any logged activities not in the schedule
        sched_names = {s.get("activity", "").lower() for s in scheduled}
        for c in completed:
            if c.get("activity", "").lower() not in sched_names:
                workout_items += (
                    f'<div style="padding:6px 0;color:#4caf50;font-size:14px;">'
                    f'✅ {c["activity"]} — {c["duration_minutes"]} min</div>'
                )
        workout_html = workout_items
    else:
        workout_html = '<p style="color:#8b8fa8;">Rest day — no workouts scheduled.</p>'

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

    # Villain Challenge section
    villain_html = ""
    if villain.get("hero"):
        hero = villain["hero"]
        status_colors = {
            "Dominating": "#4caf50", "Advantage": "#8bc34a",
            "Contested": "#ff9800", "Danger": "#f44336", "Critical": "#d32f2f",
        }

        # Hero status bar
        domain_scores = hero.get("domain_scores", {})
        domain_bars = ""
        for domain, score in sorted(domain_scores.items(), key=lambda x: x[1], reverse=True):
            bar_width = max(2, int(score))
            bar_color = "#4caf50" if score >= 60 else "#ff9800" if score >= 40 else "#f44336"
            label = domain.replace("_", " ").title()
            if len(label) > 12:
                label = label[:3].upper()
            domain_bars += (
                f'<div style="display:flex;align-items:center;margin:3px 0;">'
                f'<span style="color:#8b8fa8;font-size:11px;width:90px;">{label}</span>'
                f'<div style="flex:1;background:#222539;border-radius:4px;height:14px;margin:0 8px;">'
                f'<div style="width:{bar_width}%;background:{bar_color};border-radius:4px;'
                f'height:14px;"></div></div>'
                f'<span style="color:#e4e6f0;font-size:11px;width:30px;text-align:right;">'
                f'{score:.0f}</span></div>'
            )

        hero_html = (
            f'<div style="background:#222539;border-radius:8px;padding:14px;margin-bottom:12px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;">'
            f'<div>'
            f'<span style="color:#4f8ef7;font-size:16px;font-weight:700;">{hero["archetype"]}</span>'
            f'<span style="color:#8b8fa8;font-size:13px;"> · {hero["tier"]}</span>'
            f'</div>'
            f'<div style="text-align:right;">'
            f'<span style="color:#e4e6f0;font-size:14px;font-weight:600;">HCI {hero["hci"]:.1f}</span>'
            f'<span style="color:#8b8fa8;font-size:12px;"> · Lv {hero["level"]}'
            f' · {hero["total_xp"]} XP</span>'
            f'</div></div>'
            f'<div style="margin-top:10px;">{domain_bars}</div>'
            f'</div>'
        )

        # Challenge progress (daily)
        challenge_html = ""
        if villain.get("has_challenge"):
            ch = villain["challenge"]
            bs = villain.get("battle_status", {})
            status = bs.get("status", "Unknown")
            status_color = status_colors.get(status, "#ff9800")
            prob = bs.get("probability", 0)
            days_left = bs.get("days_remaining", 0)
            advantage = bs.get("advantage_text", "")
            actions = bs.get("recommended_actions", [])

            # Objectives progress
            obj_rows = ""
            for o in ch.get("objectives", []):
                done = o.get("completed", False)
                pct = min(100, int((o["current"] / o["target"]) * 100)) if o["target"] > 0 else 0
                check = "✅" if done else "⬜"
                pct_color = "#4caf50" if done else "#4f8ef7"
                obj_rows += (
                    f'<div style="padding:4px 0;color:#e4e6f0;font-size:13px;">'
                    f'{check} {o["description"]}'
                    f'<span style="color:{pct_color};font-size:12px;float:right;">'
                    f'{o["current"]:.0f}/{o["target"]:.0f}</span></div>'
                )

            # Action items
            action_html = ""
            if actions:
                action_items = "".join(
                    f'<div style="padding:2px 0;color:#b0b3c8;font-size:12px;">→ {a}</div>'
                    for a in actions[:3]
                )
                action_html = (
                    f'<div style="margin-top:8px;padding:8px;background:#1a1d2e;border-radius:6px;">'
                    f'<div style="color:#ff9800;font-size:11px;font-weight:600;'
                    f'text-transform:uppercase;margin-bottom:4px;">Recommended Actions</div>'
                    f'{action_items}</div>'
                )

            challenge_html = (
                f'<div style="background:#222539;border-radius:8px;padding:14px;margin-bottom:12px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'margin-bottom:10px;">'
                f'<div>'
                f'<span style="color:#e4e6f0;font-size:15px;font-weight:600;">'
                f'vs. {ch["villain_name"]}</span>'
                f'<span style="color:#8b8fa8;font-size:12px;">'
                f' · HCI {ch["villain_hci"]:.0f}</span>'
                f'</div>'
                f'<span style="background:{status_color};color:#fff;padding:3px 10px;'
                f'border-radius:4px;font-size:12px;font-weight:600;">{status}</span>'
                f'</div>'
                f'<div style="color:#8b8fa8;font-size:12px;margin-bottom:8px;">'
                f'{days_left} days remaining · {ch["completion_pct"]:.0f}% complete'
                f'{" · " + advantage if advantage else ""}</div>'
                f'{obj_rows}{action_html}'
                f'</div>'
            )

        # Monday weekly assignment
        weekly_html = ""
        if villain.get("is_monday") and villain.get("weekly_assignment"):
            wa = villain["weekly_assignment"]
            focus = ", ".join(d.replace("_", " ").title() for d in wa.get("domain_focus", []))
            weekly_html = (
                f'<div style="background:#1a2a1a;border:1px solid #2d5a2d;border-radius:8px;'
                f'padding:14px;margin-bottom:12px;">'
                f'<div style="color:#4caf50;font-size:13px;font-weight:700;'
                f'text-transform:uppercase;margin-bottom:6px;">'
                f'⚔ New Weekly Challenge Assigned</div>'
                f'<div style="color:#e4e6f0;font-size:16px;font-weight:600;margin-bottom:4px;">'
                f'{wa["villain_name"]}'
                f'<span style="color:#8b8fa8;font-size:12px;"> · {wa["tier"]}</span></div>'
                f'<div style="color:#b0b3c8;font-size:13px;margin-bottom:6px;">'
                f'{wa["description"]}</div>'
                f'<div style="color:#ff9800;font-size:12px;margin-bottom:6px;">'
                f'Weakness: {wa["weakness"]}</div>'
                f'<div style="color:#8b8fa8;font-size:12px;margin-bottom:8px;">'
                f'Targeting: {focus}</div>'
                f'</div>'
            )

        # Surges
        surge_html = ""
        surges = villain.get("active_surges", [])
        if surges:
            surge_items = "".join(
                f'<span style="background:#4a148c;color:#ce93d8;padding:3px 8px;'
                f'border-radius:4px;font-size:11px;margin-right:6px;">'
                f'⚡ {s["surge_name"]} ({s["xp_multiplier"]}x XP)</span>'
                for s in surges
            )
            surge_html = f'<div style="margin-bottom:12px;">{surge_items}</div>'

        villain_html = f'{hero_html}{surge_html}{weekly_html}{challenge_html}'
    else:
        villain_html = '<p style="color:#8b8fa8;">Villain challenge system initializing...</p>'

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
                    � Today's Workout
                </h2>
                {workout_html}

                <h2 style="color:#e4e6f0;font-size:16px;margin:20px 0 12px;
                           border-bottom:1px solid #2a2d42;padding-bottom:8px;">
                    ⚔ Villain Challenge
                </h2>
                {villain_html}

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

    # Workout
    workout = briefing.get("workout", {})
    scheduled = workout.get("scheduled", [])
    completed = workout.get("completed", [])
    if scheduled:
        lines.append("TODAY'S WORKOUT:")
        for s in scheduled:
            done = any(c.get("activity", "").lower() == s.get("activity", "").lower() for c in completed)
            mark = "[DONE]" if done else ""
            lines.append(f"  {'✓' if done else '•'} {s['activity']} — {s['duration_minutes']} min {mark}")
    else:
        lines.append("TODAY'S WORKOUT: Rest day")
    if completed:
        sched_names = {s.get("activity", "").lower() for s in scheduled}
        extras = [c for c in completed if c.get("activity", "").lower() not in sched_names]
        for c in extras:
            lines.append(f"  ✓ {c['activity']} — {c['duration_minutes']} min")
    lines.append("")

    # Villain Challenge
    villain = briefing.get("villain_challenge", {})
    if villain.get("hero"):
        hero = villain["hero"]
        lines.append("VILLAIN CHALLENGE:")
        lines.append(f"  {hero['archetype']} · {hero['tier']} · HCI {hero['hci']:.1f}"
                     f" · Lv {hero['level']} · {hero['total_xp']} XP")

        ds = hero.get("domain_scores", {})
        abbr = {"strength": "STR", "conditioning": "CND", "recovery": "REC",
                "consistency": "CST", "physique": "PHY", "nutrition_adherence": "NUT", "mobility": "MOB"}
        domain_line = " | ".join(f"{abbr.get(d, d[:3].upper())}: {s:.0f}" for d, s in sorted(ds.items(), key=lambda x: -x[1]))
        lines.append(f"  Domains: {domain_line}")

        surges = villain.get("active_surges", [])
        if surges:
            surge_names = ", ".join(f"{s['surge_name']} ({s['xp_multiplier']}x)" for s in surges)
            lines.append(f"  Active Surges: {surge_names}")

        if villain.get("is_monday") and villain.get("weekly_assignment"):
            wa = villain["weekly_assignment"]
            focus = ", ".join(d.replace("_", " ").title() for d in wa.get("domain_focus", []))
            lines.append("")
            lines.append(f"  ⚔ NEW WEEKLY CHALLENGE: {wa['villain_name']} ({wa['tier']})")
            lines.append(f"  Intel: {wa['description']}")
            lines.append(f"  Weakness: {wa['weakness']}")
            lines.append(f"  Targeting: {focus}")
            lines.append("  Objectives:")
            for o in wa.get("objectives", []):
                lines.append(f"    • {o['description']} (target: {o['target']:.0f})")

        if villain.get("has_challenge"):
            ch = villain["challenge"]
            bs = villain.get("battle_status", {})
            status = bs.get("status", "Unknown")
            days_left = bs.get("days_remaining", 0)
            lines.append("")
            lines.append(f"  vs. {ch['villain_name']} — {status} — "
                         f"{days_left} days left — {ch['completion_pct']:.0f}% complete")
            for o in ch.get("objectives", []):
                check = "✓" if o["completed"] else "•"
                lines.append(f"    {check} {o['description']} ({o['current']:.0f}/{o['target']:.0f})")
            if bs.get("advantage_text"):
                lines.append(f"  Intel: {bs['advantage_text']}")
            for a in bs.get("recommended_actions", [])[:3]:
                lines.append(f"    → {a}")
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

    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%B %d, %Y")
    villain = briefing.get("villain_challenge", {})
    if villain.get("is_monday"):
        subject = f"⚔ PAI Weekly Mission Briefing — {now}"
    else:
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
