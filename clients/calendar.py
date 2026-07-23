"""Google Calendar API v3 client, using plain requests + a stored OAuth
refresh token — deliberately avoids the heavy google-api-python-client
dependency for just reading a day's events.

Setup is a one-time process — see get_google_token.py in this repo's root
and the README for the full walkthrough (Google Cloud Console project,
OAuth consent screen, device-flow authorization).
"""
import logging
import requests
from datetime import datetime, timedelta, timezone

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, GOOGLE_CALENDAR_ID

log = logging.getLogger(__name__)
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"


def _get_access_token() -> str | None:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
        return None
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GOOGLE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except requests.RequestException as e:
        log.warning("Google token refresh failed: %s", e)
        return None


def list_events_for_day(run_date: str) -> list[dict]:
    """run_date: YYYY-MM-DD. Returns [] if Calendar isn't configured or the
    call fails — the pipeline treats that as "no calendar evidence today,"
    not a fatal error.
    """
    token = _get_access_token()
    if not token:
        return []

    day_start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        resp = requests.get(
            f"{CALENDAR_API}/calendars/{GOOGLE_CALENDAR_ID}/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "timeMin": day_start.isoformat(),
                "timeMax": day_end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
            },
            timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [
            {
                "title": e.get("summary", "(no title)"),
                "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
                "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
                "attendees": [a.get("email") for a in e.get("attendees", [])],
            }
            for e in items
        ]
    except requests.RequestException as e:
        log.warning("Calendar fetch failed: %s", e)
        return []
