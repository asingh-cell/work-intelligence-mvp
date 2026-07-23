"""Tempo Cloud REST API v4 client — planned vs. logged hours.

Docs: https://apidocs.tempo.io/
Auth: separate Tempo API token, NOT your Jira API token — get it from
Jira > Apps > Tempo > Settings > API Integration > New Token.

Everything here degrades gracefully: if an endpoint isn't available on your
Tempo plan, or the token lacks a scope, functions return None rather than
crashing the whole pipeline run (per the failure-handling design: an
optional data source being unavailable should never take down the run).
"""
import logging
import requests

from config import TEMPO_API_TOKEN

log = logging.getLogger(__name__)
BASE_URL = "https://api.tempo.io/4"


def _headers():
    return {"Authorization": f"Bearer {TEMPO_API_TOKEN}", "Accept": "application/json"}


def get_logged_hours(jira_account_id: str, date_from: str, date_to: str) -> float | None:
    """Sum of actual worklogs for this person between date_from/date_to (YYYY-MM-DD)."""
    if not TEMPO_API_TOKEN:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/worklogs/user/{jira_account_id}",
            headers=_headers(),
            params={"from": date_from, "to": date_to, "limit": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        total_seconds = sum(w.get("timeSpentSeconds", 0) for w in results)
        return round(total_seconds / 3600, 2)
    except requests.RequestException as e:
        log.warning("Tempo logged-hours fetch failed: %s", e)
        return None


def get_planned_hours(jira_account_id: str, date_from: str, date_to: str) -> float | None:
    """Sum of planned allocation for this person between date_from/date_to.

    Note: the /plans endpoint availability depends on your Tempo plan tier —
    this may simply return None if it's not enabled, which is expected.
    """
    if not TEMPO_API_TOKEN:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/plans",
            headers=_headers(),
            params={"from": date_from, "to": date_to, "assignee.id": jira_account_id, "limit": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        total_seconds = sum(p.get("plannedSecondsPerDay", 0) for p in results)
        return round(total_seconds / 3600, 2)
    except requests.RequestException as e:
        log.warning("Tempo planned-hours fetch failed (may be unavailable on your plan): %s", e)
        return None
