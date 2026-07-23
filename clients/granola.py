"""Thin wrapper around the Granola public API.

Docs: https://docs.granola.ai/introduction
Only returns notes that have a generated AI summary and transcript —
notes still processing are excluded by the API itself.
"""
import logging
import requests
from datetime import datetime, timedelta, timezone

from config import GRANOLA_API_KEY

log = logging.getLogger(__name__)
BASE_URL = "https://public-api.granola.ai/v1"


def _headers():
    return {"Authorization": f"Bearer {GRANOLA_API_KEY}"}


def notes_for_consultant(run_date: str, consultant_email: str) -> list[dict]:
    """Filter the day's notes down to ones the consultant attended.

    Returns [] if no API key is configured (e.g. free Granola plan), or if
    any call to Granola fails for another reason (network issue, rate limit,
    etc.) — Granola being unavailable should never crash the whole run, same
    as Calendar and Tempo. The error is logged, not raised.
    """
    if not GRANOLA_API_KEY:
        return []

    try:
        all_notes = _list_notes_for_day(run_date)
        matched = []
        for note in all_notes:
            attendees = [a.lower() for a in note.get("attendees", [])]
            if consultant_email.lower() in attendees:
                matched.append(_get_note(note["id"]))
        return matched
    except requests.RequestException as e:
        log.warning("Granola fetch failed, continuing without it: %s", e)
        return []


def _list_notes_for_day(run_date: str) -> list[dict]:
    day_start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    notes = []
    cursor = None
    while True:
        params = {
            "created_after": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_before": day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{BASE_URL}/notes", headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        notes.extend(data.get("notes", []))
        if not data.get("hasMore"):
            break
        cursor = data.get("cursor")
    return notes


def _get_note(note_id: str) -> dict:
    """Get full note detail: AI summary + transcript. 404 if not yet summarized."""
    resp = requests.get(f"{BASE_URL}/notes/{note_id}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()
