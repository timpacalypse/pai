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
    """Load or refresh OAuth credentials. Returns None if not configured or not yet authorized."""
    cred_path, tok_path = _get_paths()

    if not os.path.exists(cred_path):
        logger.info("google_calendar_not_configured", extra={
            "reason": f"No credentials file at {cred_path}"
        })
        return None

    try:
        from google.oauth2.credentials import Credentials

        # Only load existing token — never start interactive auth here
        if not os.path.exists(tok_path):
            logger.info("google_calendar_needs_auth", extra={
                "reason": "Token file not found — run auth flow via /skills/calendar/google/auth"
            })
            return None

        creds = Credentials.from_authorized_user_file(tok_path, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            # Save refreshed token
            with open(tok_path, "w") as f:
                f.write(creds.to_json())

        if creds and creds.valid:
            return creds

        logger.warning("google_calendar_token_invalid")
        return None
    except Exception as e:
        logger.error("google_calendar_auth_failed", extra={"error": str(e)})
        return None


def _build_service(creds):
    """Build the Google Calendar API service object."""
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def is_configured() -> bool:
    """Check if Google Calendar credentials file exists."""
    import asyncio
    return await asyncio.to_thread(_check_configured)


async def is_authorized() -> bool:
    """Check if we have a valid token (auth flow completed)."""
    creds = _get_credentials()
    return creds is not None and creds.valid


def _check_configured() -> bool:
    """Check if credentials file exists (not whether auth is complete)."""
    cred_path, _ = _get_paths()
    return os.path.exists(cred_path)


def get_auth_url() -> str | None:
    """Generate the OAuth authorization URL the user must visit.

    Returns the URL string, or None if credentials file is missing.
    """
    cred_path, _ = _get_paths()
    if not os.path.exists(cred_path):
        return None

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        cred_path,
        scopes=SCOPES,
        redirect_uri="urn:ietf:wg:oauth:2.0:oob",  # manual copy-paste flow
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url


async def exchange_auth_code(code: str) -> dict:
    """Exchange an authorization code for tokens. Saves token to disk.

    Returns {"success": True} or {"success": False, "error": "..."}.
    """
    import asyncio
    return await asyncio.to_thread(_exchange_sync, code)


def _exchange_sync(code: str) -> dict:
    cred_path, tok_path = _get_paths()
    if not os.path.exists(cred_path):
        return {"success": False, "error": "No credentials file found"}

    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(
            cred_path,
            scopes=SCOPES,
            redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        os.makedirs(os.path.dirname(tok_path), exist_ok=True)
        with open(tok_path, "w") as f:
            f.write(creds.to_json())

        logger.info("google_calendar_authorized")
        return {"success": True}
    except Exception as e:
        logger.error("google_calendar_auth_exchange_failed", extra={"error": str(e)})
        return {"success": False, "error": str(e)}


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
