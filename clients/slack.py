"""Sends the review DM (with Approve/Skip buttons, missing-hours picker,
and leave picker) and simple text DMs.

Every interactive value encodes enough context (consultant email, date,
choice) that app.py's /slack/interactions endpoint can act on it without
needing extra lookups beyond the draft store.
"""
import requests

from config import SLACK_BOT_TOKEN

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

MISSING_HOURS_OPTIONS = [
    ("Existing ticket", "existing_ticket"),
    ("Internal work", "internal_work"),
    ("Learning", "learning"),
    ("Meetings", "meetings"),
    ("Administration", "admin"),
    ("Other", "other"),
]

LEAVE_OPTIONS = [
    ("No leave", "none"),
    ("Half day leave", "half_day"),
    ("Full day leave", "full_day"),
    ("Casual leave", "casual"),
    ("Sick leave", "sick"),
    ("Earned leave", "earned"),
    ("Public holiday", "public_holiday"),
    ("Work from home", "wfh"),
    ("Other", "other"),
]


def send_text_dm(slack_user_id: str, text: str) -> dict:
    return _post(slack_user_id, blocks=None, fallback_text=text)


def send_review_message(
    slack_user_id: str,
    consultant_email: str,
    consultant_name: str,
    run_date: str,
    drafted: list[dict],
    informational: list[dict],
    hours_summary: dict | None = None,
    target_hours: float = 8.0,
) -> dict:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Good evening, {consultant_name}!* Here's what I found for {run_date}. Nothing's written to Jira yet — take a look below.",
            },
        },
        {"type": "divider"},
    ]

    total_hours = round(sum(d.get("suggested_hours", 0) for d in drafted), 2)

    if drafted:
        for item in drafted:
            timing = item.get("source_timestamp") or "time not specified in evidence"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{item['ticket_key']}* — {item['suggested_hours']}h — "
                            f"confidence {item['confidence_score']}%\n"
                            f"_{timing}_\n"
                            f"\"{item['claim_text'][:200]}\""
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
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Total evidenced today: *{total_hours}h*"}]})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "Nothing ready to write today."}})

    if informational:
        info_lines = "\n".join(
            f"• \"{c['claim_text'][:100]}\" — {c.get('reason', 'needs more info')}" for c in informational
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Needs more info (can't target a ticket automatically)*\n{info_lines}"},
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

    blocks.append({"type": "divider"})

    missing = round(target_hours - total_hours, 2)
    if missing > 0:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"That's {total_hours}h accounted for out of {target_hours}h — where should the remaining *{missing}h* go?"},
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": "missing_hours",
                "elements": [
                    {
                        "type": "static_select",
                        "action_id": "missing_hours_allocation",
                        "placeholder": {"type": "plain_text", "text": "Allocate remaining hours"},
                        "options": [
                            {"text": {"type": "plain_text", "text": label}, "value": f"{consultant_email}::{run_date}::{value}"}
                            for label, value in MISSING_HOURS_OPTIONS
                        ],
                    }
                ],
            }
        )

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "Were you on leave at all today?"}})
    blocks.append(
        {
            "type": "actions",
            "block_id": "leave",
            "elements": [
                {
                    "type": "static_select",
                    "action_id": "leave_selection",
                    "placeholder": {"type": "plain_text", "text": "Select leave type"},
                    "options": [
                        {"text": {"type": "plain_text", "text": label}, "value": f"{consultant_email}::{run_date}::{value}"}
                        for label, value in LEAVE_OPTIONS
                    ],
                }
            ],
        }
    )

    return _post(slack_user_id, blocks=blocks, fallback_text="Your work review is ready")


def _post(slack_user_id: str, blocks, fallback_text: str) -> dict:
    body = {"channel": slack_user_id, "text": fallback_text}
    if blocks:
        body["blocks"] = blocks
    resp = requests.post(
        POST_MESSAGE_URL,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=body,
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


def ack_selection_via_response_url(response_url: str, text: str) -> None:
    """For select-menu choices (leave, missing-hours), append a confirmation
    as a new message in the same thread rather than replacing the whole
    review — the buttons above should stay usable."""
    requests.post(response_url, json={"replace_original": False, "response_type": "ephemeral", "text": text}, timeout=10)
