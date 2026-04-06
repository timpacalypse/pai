"""Google Calendar integration — read-only sync of events.

Uses OAuth 2.0 (installed app / desktop flow). The user must:
1. Create a Google Cloud project at https://console.cloud.google.com
2. Enable the Google Calendar API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download the JSON file and place at /app/credentials/google_credentials.json
   (or set GOOGLE_CREDENTIALS_PATH env var)
5. On first run, a browser-less consent flow prints a URL to visit.
   After granting access, the refresh token is stored at /app/credentials/google_token.json
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pai.google_calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _get_paths():
    """Get credential paths from settings or environment."""
    try:
        from app.core.config import settings
        cred_path = settings.google_credentials_path
        tok_path = settings.google_token_path
    except Exception:
        cred_path = os.environ.get(
            "GOOGLE_CREDENTIALS_PATH", "/app/credentials/google_credentials.json"
        )
        tok_path = os.environ.get(
            "GOOGLE_TOKEN_PATH", "/app/credentials/google_token.json"
        )
    return cred_path, tok_path


def _get_credentials():
    """Load or refresh OAuth credentials. Returns None if not configured."""
    cred_path, tok_path = _get_paths()

    if not os.path.exists(cred_path):
        logger.info("google_calendar_not_configured", extra={
            "reason": f"No credentials file at {cred_path}"
        })
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if os.path.exists(tok_path):
            creds = Credentials.from_authorized_user_file(tok_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    cred_path, SCOPES
                )
                # Use console-based auth (no browser needed on headless server)
                creds = flow.run_console()

            # Save the token for next time
            os.makedirs(os.path.dirname(tok_path), exist_ok=True)
            with open(tok_path, "w") as f:
                f.write(creds.to_json())

        return creds
    except Exception as e:
        logger.error("google_calendar_auth_failed", extra={"error": str(e)})
        return None


def _build_service(creds):
    """Build the Google Calendar API service object."""
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def is_configured() -> bool:
    """Check if Google Calendar credentials exist and are valid."""
    import asyncio
    return await asyncio.to_thread(_check_configured)


def _check_configured() -> bool:
    cred_path, _ = _get_paths()
    if not os.path.exists(cred_path):
        return False
    creds = _get_credentials()
    return creds is not None and creds.valid


async def get_google_events(days: int = 14) -> list[dict]:
    """Fetch upcoming events from the user's primary Google Calendar.

    Returns a list of dicts with keys matching our local calendar schema:
    title, event_date, event_time, end_time, location, notes, source.
    """
    import asyncio
    return await asyncio.to_thread(_fetch_events_sync, days)


def _fetch_events_sync(days: int = 14) -> list[dict]:
    """Synchronous event fetch (runs in thread pool)."""
    creds = _get_credentials()
    if not creds:
        return []

    try:
        service = _build_service(creds)

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()

        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=100,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        raw_events = events_result.get("items", [])
        parsed = []

        for event in raw_events:
            start = event.get("start", {})
            end = event.get("end", {})

            # All-day events have 'date', timed events have 'dateTime'
            if "dateTime" in start:
                start_dt = datetime.fromisoformat(start["dateTime"])
                event_date = str(start_dt.date())
                event_time = start_dt.strftime("%H:%M")
            else:
                event_date = start.get("date", "")
                event_time = None

            if "dateTime" in end:
                end_dt = datetime.fromisoformat(end["dateTime"])
                end_time = end_dt.strftime("%H:%M")
            else:
                end_time = None

            parsed.append({
                "title": event.get("summary", "(No title)"),
                "event_date": event_date,
                "event_time": event_time,
                "end_time": end_time,
                "location": event.get("location", ""),
                "notes": event.get("description", ""),
                "source": "google_calendar",
                "google_event_id": event.get("id", ""),
                "recurrence": "none",
                "category": "appointment",
                "family_member_name": "",
            })

        logger.info("google_calendar_fetched", extra={
            "event_count": len(parsed),
            "days": days,
        })
        return parsed

    except Exception as e:
        logger.error("google_calendar_fetch_failed", extra={"error": str(e)})
        return []
