"""Sends the review DM (with Approve/Skip buttons) and simple text DMs.

Uses Slack Web API directly. Each interactive message includes a
draft_id in its button values — see app.py's /slack/interactions
endpoint, which is what actually processes a click.
"""
import requests

from config import SLACK_BOT_TOKEN

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def send_text_dm(slack_user_id: str, text: str) -> dict:
    resp = requests.post(
        POST_MESSAGE_URL,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": slack_user_id, "text": text},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack send failed: {data.get('error')}")
    return data


def send_review_message(
    slack_user_id: str,
    consultant_name: str,
    run_date: str,
    drafted: list[dict],
    informational: list[dict],
    hours_summary: dict | None = None,
) -> dict:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Good evening, {consultant_name}!* Work from {run_date} is ready for your review. Nothing has been written to Jira yet.",
            },
        },
        {"type": "divider"},
    ]

    if drafted:
        for item in drafted:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{item['ticket_key']}* — {item['suggested_hours']}h — "
                            f"confidence {item['confidence_score']}%\n\"{item['claim_text'][:150]}\""
                        ),
                    },
                }
            )
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"draft_{item['draft_id']}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve & write"},
                            "style": "primary",
                            "action_id": "approve_draft",
                            "value": item["draft_id"],
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Skip"},
                            "style": "danger",
                            "action_id": "skip_draft",
                            "value": item["draft_id"],
                        },
                    ],
                }
            )
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "Nothing ready to write today."}})

    if informational:
        info_lines = "\n".join(
            f"• \"{c['claim_text'][:100]}\" — {c.get('reason', 'needs more info')}" for c in informational
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Needs more info (no button — can't target a ticket automatically)*\n{info_lines}"},
            }
        )

    if hours_summary:
        logged = hours_summary.get("logged")
        planned = hours_summary.get("planned")
        text = None
        if planned is not None and logged is not None:
            delta = round(planned - logged, 2)
            text = f"Planned: {planned}h — Logged: {logged}h — {'Short by' if delta > 0 else 'Over by'} {abs(delta)}h"
        elif logged is not None:
            text = f"Logged: {logged}h (planned unavailable)"
        elif planned is not None:
            text = f"Planned: {planned}h (logged unavailable)"
        if text:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Tempo:* {text}"}]})

    resp = requests.post(
        POST_MESSAGE_URL,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": slack_user_id, "blocks": blocks, "text": "Your work review is ready"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack send failed: {data.get('error')}")
    return data


def update_message_via_response_url(response_url: str, text: str) -> None:
    """Every Slack interaction payload includes a response_url — POSTing here
    replaces the original message (e.g. to show "Approved and written")."""
    requests.post(response_url, json={"replace_original": True, "text": text}, timeout=10)
