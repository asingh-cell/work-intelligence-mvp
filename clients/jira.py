"""Minimal Jira Cloud REST API v3 client — comment, worklog, JQL search.

Uses the site's basic-auth-with-API-token scheme:
https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
"""
import requests
from requests.auth import HTTPBasicAuth

from config import JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN

BASE_URL = f"https://{JIRA_SITE}/rest/api/3"
AUTH = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


def _adf(text: str) -> dict:
    """Wrap plain text in minimal Atlassian Document Format."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def search_candidate_tickets(project_key: str, ticket_key_hint: str | None, jql_text: str) -> list[dict]:
    """Try an explicit ticket key first; fall back to a text search within the project.

    Returns [] rather than guessing when nothing matches — callers must treat
    an empty result as "escalate to the consultant", not "pick the closest one".
    """
    if ticket_key_hint:
        resp = requests.get(f"{BASE_URL}/issue/{ticket_key_hint}", auth=AUTH, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return [resp.json()]
        return []

    jql = f'project = {project_key} AND text ~ "{jql_text}" ORDER BY updated DESC'
    resp = requests.get(
        f"{BASE_URL}/search",
        auth=AUTH,
        headers=HEADERS,
        params={"jql": jql, "maxResults": 5},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("issues", [])


def add_comment(issue_key: str, body_text: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/issue/{issue_key}/comment",
        auth=AUTH,
        headers=HEADERS,
        json={"body": _adf(body_text)},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def add_worklog(issue_key: str, started_iso: str, time_spent: str, comment_text: str) -> dict:
    """time_spent format: Jira duration syntax, e.g. '1h 30m', '45m', '2h'."""
    resp = requests.post(
        f"{BASE_URL}/issue/{issue_key}/worklog",
        auth=AUTH,
        headers=HEADERS,
        json={
            "started": started_iso,
            "timeSpent": time_spent,
            "comment": _adf(comment_text),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def create_issue(project_key: str, summary: str, description_text: str, issue_type: str = "Task") -> dict:
    resp = requests.post(
        f"{BASE_URL}/issue",
        auth=AUTH,
        headers=HEADERS,
        json={
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": _adf(description_text),
                "issuetype": {"name": issue_type},
            }
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
