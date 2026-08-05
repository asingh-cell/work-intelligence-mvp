"""
Work Intelligence Assistant — MVP
Flow: Postman POST /trigger -> pull Slack evidence for a person -> Claude drafts
timesheet items -> interactive Slack DM (approve/edit/skip/leave/manual) ->
Submit pushes approved items into Jira as worklogs.

ponytail: single process, in-memory state (PENDING dict). A Render restart or
free-tier spin-down wipes any draft that hasn't been submitted yet. Fine for a
one-person prototype tested by hand; before real multi-day/multi-user use,
swap PENDING for a small SQLite table (draft_id primary key, json blob) so
state survives restarts.
"""

import os
import re
import json
import hmac
import time
import uuid
import hashlib
import logging
from datetime import datetime, timedelta, date

import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("work-intel")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config (all from env — see .env.example)
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
TRIGGER_SHARED_SECRET = os.environ["TRIGGER_SHARED_SECRET"]

JIRA_SITE = os.environ["JIRA_SITE"].rstrip("/")          # e.g. https://yourcompany.atlassian.net
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# comma-separated list of valid Jira ticket keys Claude is allowed to match against
ALLOWED_TICKET_KEYS = {
    k.strip().upper() for k in os.environ.get("ALLOWED_TICKET_KEYS", "").split(",") if k.strip()
}

# comma-separated Slack channel IDs to scan for evidence
SLACK_EVIDENCE_CHANNELS = [
    c.strip() for c in os.environ.get("SLACK_EVIDENCE_CHANNELS", "").split(",") if c.strip()
]

# optional JSON map of {"channel_id": "TICKET-PREFIX"} to bias matching, e.g. helps
# Claude know "#help-front-end" chatter is probably FRNT-* work. Safe to leave as {}.
try:
    SLACK_CHANNEL_TICKET_MAP = json.loads(os.environ.get("SLACK_CHANNEL_TICKET_MAP", "{}"))
except json.JSONDecodeError:
    SLACK_CHANNEL_TICKET_MAP = {}

# confidence (0-100) at/above which an item gets a green dot instead of yellow/red
AUTO_WRITE_THRESHOLD = float(os.environ.get("AUTO_WRITE_THRESHOLD", "70"))

DAILY_TARGET_HOURS = 8.0

# ponytail: fixed UTC offsets, no DST math. Fine while this only runs for the
# India team (transcript says US/Central come later) — when it expands, swap
# for zoneinfo with real IANA tz names (Asia/Kolkata, America/New_York, ...).
TIMEZONES = {
    "IST": "+0530",
    "EST": "-0500",
    "CST": "-0600",
}
DEFAULT_TZ = "IST"

# ---------------------------------------------------------------------------
# In-memory draft store — see ponytail note at top of file
# ---------------------------------------------------------------------------
PENDING = {}  # draft_id -> draft dict


def new_item_id():
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def slack_api(method, **payload):
    resp = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=payload,
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        log.error("Slack API %s failed: %s", method, data)
    return data


def require_ok(resp: dict, context: str):
    """Raise a clear error with Slack's actual reason instead of letting a
    caller read a missing key and blow up with an opaque KeyError."""
    if not resp.get("ok"):
        raise RuntimeError(f"{context}: {resp.get('error', 'unknown error')}")


def verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except (ValueError, TypeError):
        return False  # missing/garbage timestamp -> reject, don't crash
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, req.headers.get("X-Slack-Signature", ""))


def fetch_person_messages(slack_user_id: str, target_date: str) -> list[dict]:
    """Pull that person's messages (top-level + thread replies) from the
    configured evidence channels for the given YYYY-MM-DD date."""
    day = datetime.strptime(target_date, "%Y-%m-%d")
    oldest = str(day.timestamp())
    latest = str((day + timedelta(days=1)).timestamp())

    collected = []
    for channel_id in SLACK_EVIDENCE_CHANNELS:
        cursor = None
        while True:
            kwargs = {"channel": channel_id, "oldest": oldest, "latest": latest, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor  # Slack rejects an explicit null cursor on the first page
            resp = slack_api("conversations.history", **kwargs)
            if not resp.get("ok"):
                break
            for msg in resp.get("messages", []):
                if msg.get("user") == slack_user_id and msg.get("text"):
                    collected.append({"channel": channel_id, "text": msg["text"], "ts": msg["ts"]})
                # thread replies
                if msg.get("reply_count"):
                    replies = slack_api(
                        "conversations.replies", channel=channel_id, ts=msg["ts"]
                    )
                    for r in replies.get("messages", []):
                        if r.get("user") == slack_user_id and r.get("text") and r["ts"] != msg["ts"]:
                            collected.append({"channel": channel_id, "text": r["text"], "ts": r["ts"]})
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    return collected


def get_slack_user_email(slack_user_id: str) -> str | None:
    """Best-effort — returns None (not raise) since this only feeds the
    optional Jira ticket-picker dropdown, not the core trigger flow."""
    resp = slack_api("users.info", user=slack_user_id)
    if not resp.get("ok"):
        return None
    return resp.get("user", {}).get("profile", {}).get("email")


def open_dm(slack_user_id: str) -> str:
    resp = slack_api("conversations.open", users=slack_user_id)
    require_ok(resp, "opening DM")
    return resp["channel"]["id"]


def resolve_slack_user_id(email: str = None, slack_user_id: str = None) -> str:
    """Accept either an email or a raw Slack user ID and return a Slack user ID.
    Email is preferred — it's what you already have for everyone, no hunting
    through Slack profile menus per person."""
    if slack_user_id:
        return slack_user_id.strip()
    if email:
        # users.lookupByEmail is one of Slack's older methods — it rejects a
        # JSON body (invalid_arguments) and wants form-encoded params instead,
        # unlike every other Slack call in this app.
        resp = requests.get(
            "https://slack.com/api/users.lookupByEmail",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"email": email.strip()},
            timeout=15,
        ).json()
        require_ok(resp, f"looking up Slack user for {email}")
        return resp["user"]["id"]
    raise RuntimeError("provide either 'email' or 'slack_user_id' in the request body")


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

EXTRACTION_PROMPT = """You are helping draft a Jira timesheet from a person's Slack activity.

Allowed Jira ticket keys (only use these; if nothing fits, use "UNMATCHED"):
{allowed_keys}

Channel-to-project hints (channel id -> likely ticket prefix, may be empty):
{channel_hints}

Below are this person's Slack messages for {target_date}, as (channel, timestamp, text) triples.
Group them into distinct pieces of work. For each piece of work return:
- ticket_key: one of the allowed keys, or "UNMATCHED"
- ticket_summary: a concise one-line description of what that ticket is about
- description: a short worklog-ready description of what was actually done, in your own words
- hours: your best estimate of time spent (numbers, can be fractional, e.g. 1.5)
- confidence: 0-100, how confident you are this mapping/estimate is correct
- start_time / end_time: 24h HH:MM in {tz} local time, your best estimate
Do not invent tickets outside the allowed list. Do not exceed a combined total of {daily_target} hours
unless the evidence clearly supports more.

Messages:
{messages}

Respond with ONLY a JSON array of objects with exactly these keys:
ticket_key, ticket_summary, description, hours, confidence, start_time, end_time
No prose, no markdown fences, just the JSON array. If there is no usable evidence, return [].
"""


def extract_timesheet_items(messages: list[dict], target_date: str) -> list[dict]:
    if not messages:
        return []

    msg_lines = "\n".join(f"({m['channel']}, {m['ts']}) {m['text']}" for m in messages)
    prompt = EXTRACTION_PROMPT.format(
        allowed_keys=", ".join(sorted(ALLOWED_TICKET_KEYS)) or "(none configured)",
        channel_hints=json.dumps(SLACK_CHANNEL_TICKET_MAP),
        target_date=target_date,
        tz=DEFAULT_TZ,
        daily_target=DAILY_TARGET_HOURS,
        messages=msg_lines,
    )

    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text").strip()
    return parse_claude_json(raw)


def parse_claude_json(raw: str) -> list[dict]:
    """Strip accidental markdown fences and parse. Returns [] on failure
    rather than raising — a bad Claude response shouldn't crash the trigger."""
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        log.error("Could not parse Claude response as JSON: %s", raw[:500])
        return []


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------
def jira_auth():
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def jira_get_assigned_tickets(email: str, max_results: int = 20) -> list[dict]:
    """Tickets currently assigned to this person, for the edit/manual-entry
    dropdowns. Returns [] on any failure — this is a convenience list, not
    something that should block the trigger if Jira hiccups."""
    try:
        who = requests.get(
            f"{JIRA_SITE}/rest/api/3/user/search",
            auth=jira_auth(), params={"query": email}, timeout=15,
        )
        who.raise_for_status()
        matches = [u for u in who.json() if u.get("emailAddress", "").lower() == email.lower()]
        if not matches:
            return []
        account_id = matches[0]["accountId"]

        search = requests.get(
            f"{JIRA_SITE}/rest/api/3/search",
            auth=jira_auth(),
            params={
                "jql": f'assignee="{account_id}" ORDER BY updated DESC',
                "maxResults": max_results,
                "fields": "summary",
            },
            timeout=15,
        )
        search.raise_for_status()
        return [
            {"key": issue["key"], "summary": issue["fields"]["summary"]}
            for issue in search.json().get("issues", [])
        ]
    except requests.RequestException as e:
        log.error("jira_get_assigned_tickets failed for %s: %s", email, e)
        return []


def jira_add_worklog(issue_key: str, hours: float, comment: str, target_date: str,
                      start_time: str, tz: str):
    started = f"{target_date}T{start_time}:00.000{TIMEZONES.get(tz, TIMEZONES[DEFAULT_TZ])}"
    body = {
        "timeSpentSeconds": int(round(hours * 3600)),
        "started": started,
        "comment": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
        },
    }
    try:
        resp = requests.post(
            f"{JIRA_SITE}/rest/api/3/issue/{issue_key}/worklog",
            auth=jira_auth(),
            json=body,
            timeout=15,
        )
    except requests.RequestException as e:
        log.error("Jira request errored for %s: %s", issue_key, e)
        return False, str(e)
    if resp.status_code >= 300:
        log.error("Jira worklog failed for %s: %s %s", issue_key, resp.status_code, resp.text)
        return False, resp.text[:300]
    return True, None


# ---------------------------------------------------------------------------
# Business logic shared helpers (kept pure so they're easy to test — see test_app.py)
# ---------------------------------------------------------------------------
def required_hours_for_leave(leave_status: str) -> float:
    return {"full": 0.0, "half": DAILY_TARGET_HOURS / 2}.get(leave_status, DAILY_TARGET_HOURS)


def confidence_dot(confidence: float) -> str:
    if confidence >= AUTO_WRITE_THRESHOLD:
        return "🟢"
    if confidence >= AUTO_WRITE_THRESHOLD - 30:
        return "🟡"
    return "🔴"


def logged_hours(draft: dict) -> float:
    total = 0.0
    for item in draft["items"].values():
        if item["status"] in ("approved", "edited"):
            total += item["hours"]
    for m in draft["manual_items"].values():
        total += m["hours"]
    return total


def progress_bar(logged: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "🟩" * width
    filled = min(width, round(width * logged / target))
    return "🟩" * filled + "⬜" * (width - filled)


# ---------------------------------------------------------------------------
# Slack Block Kit rendering
# ---------------------------------------------------------------------------
MANUAL_CATEGORIES = [
    ("existing", "🏢 Existing ticket"),
    ("internal", "🛠 Internal work"),
    ("learning", "📚 Learning"),
    ("meetings", "🗣 Meetings"),
    ("admin", "🗂 Administration"),
    ("other", "❓ Other"),
]


def render_draft_blocks(draft: dict) -> list:
    target = required_hours_for_leave(draft["leave_status"])
    logged = logged_hours(draft)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"👋 Evening! Here's what I found for *{draft['date']}*. "
                    f"Give each item a thumbs up or down, then hit Submit — "
                    f"nothing touches Jira until you do."
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{progress_bar(logged, target)}  *{logged:.1f}h / {target:.1f}h*"
                + (f"  _(leave: {draft['leave_status']})_" if draft["leave_status"] != "none" else ""),
            },
        },
        {"type": "divider"},
    ]

    for item_id, item in draft["items"].items():
        if item["status"] == "skipped":
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"~{item['ticket_key']} — {item['ticket_summary']}~  ⏭ _skipped_"},
            })
            blocks.append({
                "type": "actions",
                "block_id": f"item_actions|{draft['id']}|{item_id}",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "↩️ Undo"},
                     "action_id": "undo_item", "value": f"{draft['id']}|{item_id}"},
                ],
            })
            continue

        header = f"*{item['ticket_key']}* — {item['ticket_summary']}"
        if item["ticket_key"] not in ALLOWED_TICKET_KEYS:
            header += "  ⚠️ _not in your allowed ticket list — check before approving_"

        status_line = ""
        if item["status"] in ("approved", "edited"):
            status_line = "  ✅ *approved*" if item["status"] == "approved" else "  ✏️ *edited*"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{header}{status_line}\n"
                    f"{item['hours']}h  {confidence_dot(item['confidence'])} {item['confidence']:.0f}% confidence\n"
                    f"🕒 {item['start_time']} – {item['end_time']} {item['timezone']}, {draft['date']}\n"
                    f"{item['description']}"
                ),
            },
        })
        if item["status"] not in ("approved", "edited"):
            blocks.append({
                "type": "actions",
                "block_id": f"item_actions|{draft['id']}|{item_id}",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"},
                     "style": "primary", "action_id": "approve_item", "value": f"{draft['id']}|{item_id}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"},
                     "action_id": "edit_item", "value": f"{draft['id']}|{item_id}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "⏭ Skip"},
                     "style": "danger", "action_id": "skip_item", "value": f"{draft['id']}|{item_id}"},
                ],
            })
        else:
            blocks.append({
                "type": "actions",
                "block_id": f"item_actions|{draft['id']}|{item_id}",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✏️ Edit"},
                     "action_id": "edit_item", "value": f"{draft['id']}|{item_id}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "↩️ Undo"},
                     "action_id": "undo_item", "value": f"{draft['id']}|{item_id}"},
                ],
            })

    remaining = max(0.0, target - logged)
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"⌛ *{remaining:.1f}h* still unaccounted for. On leave, or want to log it yourself?"},
    })
    blocks.append({
        "type": "actions",
        "block_id": f"bottom_controls|{draft['id']}",
        "elements": [
            {
                "type": "static_select",
                "action_id": "manual_category",
                "placeholder": {"type": "plain_text", "text": "📋 Log the rest — pick a category"},
                "options": [
                    {"text": {"type": "plain_text", "text": label}, "value": key}
                    for key, label in MANUAL_CATEGORIES
                ],
            },
            {
                "type": "static_select",
                "action_id": "leave_status",
                "placeholder": {"type": "plain_text", "text": "🌴 Were you on leave?"},
                "options": [
                    {"text": {"type": "plain_text", "text": "No leave"}, "value": "none"},
                    {"text": {"type": "plain_text", "text": "Half-day leave"}, "value": "half"},
                    {"text": {"type": "plain_text", "text": "Full-day leave"}, "value": "full"},
                ],
            },
        ],
    })

    for m_id, m in draft["manual_items"].items():
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"📝 *{m['category']}* — {m['description']}  ({m['hours']}h)"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "block_id": f"submit|{draft['id']}",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🚀 Submit to Jira"},
             "style": "primary", "action_id": "submit_final", "value": draft["id"]},
        ],
    })
    return blocks


def assigned_ticket_dropdown_block(assigned_tickets: list, initial_key: str = None) -> dict | None:
    """Optional dropdown of the person's currently-assigned Jira tickets.
    Returns None when we have no list (Jira lookup failed or found nothing) —
    callers fall back to the free-text ticket_key field in that case."""
    if not assigned_tickets:
        return None
    options = [
        {"text": {"type": "plain_text", "text": f"{t['key']} — {t['summary']}"[:75]}, "value": t["key"]}
        for t in assigned_tickets[:100]  # Slack's static_select option limit
    ]
    block = {
        "type": "input", "block_id": "assigned_ticket", "optional": True,
        "label": {"type": "plain_text", "text": "Or pick from your assigned tickets"},
        "element": {"type": "static_select", "action_id": "value", "options": options},
    }
    match = next((o for o in options if o["value"] == initial_key), None)
    if match:
        block["element"]["initial_option"] = match
    return block


def edit_modal(draft_id, item_id, item, assigned_tickets=None):
    blocks = [
        {"type": "input", "block_id": "ticket_key", "label": {"type": "plain_text", "text": "Ticket key"},
         "element": {"type": "plain_text_input", "action_id": "value", "initial_value": item["ticket_key"]}},
    ]
    dropdown = assigned_ticket_dropdown_block(assigned_tickets, initial_key=item["ticket_key"])
    if dropdown:
        blocks.append(dropdown)
    blocks += [
        {"type": "input", "block_id": "description", "label": {"type": "plain_text", "text": "Description"},
         "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                     "initial_value": item["description"]}},
        {"type": "input", "block_id": "hours", "label": {"type": "plain_text", "text": "Hours"},
         "element": {"type": "plain_text_input", "action_id": "value", "initial_value": str(item["hours"])}},
        {"type": "input", "block_id": "start_time", "label": {"type": "plain_text", "text": "Start (HH:MM)"},
         "element": {"type": "plain_text_input", "action_id": "value", "initial_value": item["start_time"]}},
        {"type": "input", "block_id": "end_time", "label": {"type": "plain_text", "text": "End (HH:MM)"},
         "element": {"type": "plain_text_input", "action_id": "value", "initial_value": item["end_time"]}},
        {"type": "input", "block_id": "timezone", "label": {"type": "plain_text", "text": "Timezone"},
         "element": {"type": "static_select", "action_id": "value",
                     "initial_option": {"text": {"type": "plain_text", "text": item["timezone"]}, "value": item["timezone"]},
                     "options": [{"text": {"type": "plain_text", "text": tz}, "value": tz} for tz in TIMEZONES]}},
    ]
    return {
        "type": "modal",
        "callback_id": "edit_item_submit",
        "private_metadata": f"{draft_id}|{item_id}",
        "title": {"type": "plain_text", "text": "Edit entry"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": blocks,
    }


def manual_modal(draft_id, category_key, category_label, assigned_tickets=None):
    blocks = [
        {"type": "input", "block_id": "ticket_key", "optional": True,
         "label": {"type": "plain_text", "text": "Ticket key (optional)"},
         "element": {"type": "plain_text_input", "action_id": "value"}},
    ]
    dropdown = assigned_ticket_dropdown_block(assigned_tickets)
    if dropdown:
        blocks.append(dropdown)
    blocks += [
        {"type": "input", "block_id": "description", "label": {"type": "plain_text", "text": "What did you do?"},
         "element": {"type": "plain_text_input", "action_id": "value", "multiline": True}},
        {"type": "input", "block_id": "hours", "label": {"type": "plain_text", "text": "Hours"},
         "element": {"type": "plain_text_input", "action_id": "value"}},
        {"type": "input", "block_id": "timezone", "label": {"type": "plain_text", "text": "Timezone"},
         "element": {"type": "static_select", "action_id": "value",
                     "initial_option": {"text": {"type": "plain_text", "text": DEFAULT_TZ}, "value": DEFAULT_TZ},
                     "options": [{"text": {"type": "plain_text", "text": tz}, "value": tz} for tz in TIMEZONES]}},
    ]
    return {
        "type": "modal",
        "callback_id": "manual_entry_submit",
        "private_metadata": f"{draft_id}|{category_key}|{category_label}",
        "title": {"type": "plain_text", "text": category_label[:24]},
        "submit": {"type": "plain_text", "text": "Add"},
        "blocks": blocks,
    }


def update_draft_message(draft):
    slack_api(
        "chat.update",
        channel=draft["channel"],
        ts=draft["ts"],
        blocks=render_draft_blocks(draft),
        text=f"Timesheet draft for {draft['date']}",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify(status="ok", commit=os.environ.get("RENDER_GIT_COMMIT", "unknown")[:7])


@app.route("/trigger", methods=["POST"])
def trigger():
    """Postman calls this with: {"email": "person@company.com", "date": "2026-08-04"}
    (or {"slack_user_id": "U0123", ...} if you already have it)
    Header: X-Trigger-Secret: <TRIGGER_SHARED_SECRET>"""
    if request.headers.get("X-Trigger-Secret") != TRIGGER_SHARED_SECRET:
        return jsonify(error="forbidden"), 403

    body = request.get_json(force=True, silent=True) or {}
    try:
        slack_user_id = resolve_slack_user_id(
            email=body.get("email"), slack_user_id=body.get("slack_user_id")
        )
    except RuntimeError as e:
        return jsonify(error=str(e)), 400
    target_date = body.get("date") or date.today().isoformat()
    email = body.get("email") or get_slack_user_email(slack_user_id)
    assigned_tickets = jira_get_assigned_tickets(email) if email else []

    try:
        messages = fetch_person_messages(slack_user_id, target_date)
    except requests.RequestException as e:
        return jsonify(error=f"Slack fetch failed: {e}"), 502

    try:
        extracted = extract_timesheet_items(messages, target_date)
    except Exception as e:
        log.exception("Claude extraction failed")
        return jsonify(error=f"Claude extraction failed: {e}"), 502

    draft_id = uuid.uuid4().hex
    items = {}
    for row in extracted:
        item_id = new_item_id()
        items[item_id] = {
            "ticket_key": str(row.get("ticket_key", "UNMATCHED")).upper(),
            "ticket_summary": row.get("ticket_summary", ""),
            "description": row.get("description", ""),
            "hours": float(row.get("hours", 0) or 0),
            "confidence": float(row.get("confidence", 0) or 0),
            "start_time": row.get("start_time", "09:00"),
            "end_time": row.get("end_time", "10:00"),
            "timezone": DEFAULT_TZ,
            "status": "pending",
        }

    draft = {
        "id": draft_id,
        "slack_user_id": slack_user_id,
        "date": target_date,
        "items": items,
        "manual_items": {},
        "leave_status": "none",
        "submitted": False,
        "assigned_tickets": assigned_tickets,
    }

    try:
        dm_channel = open_dm(slack_user_id)
        draft["channel"] = dm_channel
        post = slack_api(
            "chat.postMessage",
            channel=dm_channel,
            blocks=render_draft_blocks(draft),
            text=f"Timesheet draft for {target_date}",
        )
        require_ok(post, "posting DM")
    except RuntimeError as e:
        return jsonify(error=str(e)), 502
    draft["ts"] = post["ts"]
    PENDING[draft_id] = draft

    return jsonify(draft_id=draft_id, items_found=len(items)), 200


@app.route("/slack/interactions", methods=["POST"])
def interactions():
    if not verify_slack_signature(request):
        return "invalid signature", 401

    try:
        return _handle_interaction(json.loads(request.form["payload"]))
    except Exception:
        # ponytail: catch-all so a bug in one action (bad payload, Slack API
        # hiccup) returns 200 instead of 500. Slack retries non-2xx responses,
        # which would re-fire the same button click. Log and move on; the
        # user can just click again if something visibly didn't update.
        log.exception("interaction handler failed")
        return "", 200


def _handle_interaction(payload):
    ptype = payload.get("type")

    if ptype == "block_actions":
        action = payload["actions"][0]
        action_id = action["action_id"]
        trigger_id = payload["trigger_id"]

        if action_id in ("approve_item", "skip_item"):
            draft_id, item_id = action["value"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                draft["items"][item_id]["status"] = "approved" if action_id == "approve_item" else "skipped"
                update_draft_message(draft)

        elif action_id == "undo_item":
            draft_id, item_id = action["value"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                draft["items"][item_id]["status"] = "pending"
                update_draft_message(draft)

        elif action_id == "edit_item":
            draft_id, item_id = action["value"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                slack_api("views.open", trigger_id=trigger_id,
                          view=edit_modal(draft_id, item_id, draft["items"][item_id],
                                          assigned_tickets=draft.get("assigned_tickets")))

        elif action_id == "leave_status":
            draft_id = payload["actions"][0]["block_id"].split("|")[1]
            draft = PENDING.get(draft_id)
            if draft:
                draft["leave_status"] = action["selected_option"]["value"]
                update_draft_message(draft)

        elif action_id == "manual_category":
            draft_id = payload["actions"][0]["block_id"].split("|")[1]
            key = action["selected_option"]["value"]
            label = dict(MANUAL_CATEGORIES)[key]
            draft = PENDING.get(draft_id)
            slack_api("views.open", trigger_id=trigger_id,
                      view=manual_modal(draft_id, key, label,
                                        assigned_tickets=draft.get("assigned_tickets") if draft else None))

        elif action_id == "submit_final":
            draft_id = action["value"]
            draft = PENDING.get(draft_id)
            if draft and not draft["submitted"]:
                submit_to_jira(draft)

        return "", 200

    if ptype == "view_submission":
        callback_id = payload["view"]["callback_id"]
        values = payload["view"]["state"]["values"]

        if callback_id == "edit_item_submit":
            draft_id, item_id = payload["view"]["private_metadata"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                item = draft["items"][item_id]
                picked = values.get("assigned_ticket", {}).get("value", {}).get("selected_option")
                typed = values["ticket_key"]["value"]["value"]
                item["ticket_key"] = (picked["value"] if picked else typed).upper()
                item["description"] = values["description"]["value"]["value"]
                item["hours"] = float(values["hours"]["value"]["value"] or 0)
                item["start_time"] = values["start_time"]["value"]["value"]
                item["end_time"] = values["end_time"]["value"]["value"]
                item["timezone"] = values["timezone"]["value"]["selected_option"]["value"]
                item["status"] = "edited"
                update_draft_message(draft)

        elif callback_id == "manual_entry_submit":
            draft_id, key, label = payload["view"]["private_metadata"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                m_id = new_item_id()
                picked = values.get("assigned_ticket", {}).get("value", {}).get("selected_option")
                typed = values["ticket_key"]["value"]["value"]
                draft["manual_items"][m_id] = {
                    "category": label,
                    "ticket_key": (picked["value"] if picked else typed) or "N/A",
                    "description": values["description"]["value"]["value"],
                    "hours": float(values["hours"]["value"]["value"] or 0),
                    "timezone": values["timezone"]["value"]["selected_option"]["value"],
                }
                update_draft_message(draft)

        return "", 200

    return "", 200


def submit_to_jira(draft):
    results = []
    for item in draft["items"].values():
        if item["status"] not in ("approved", "edited"):
            continue
        ok, err = jira_add_worklog(
            item["ticket_key"], item["hours"], item["description"],
            draft["date"], item["start_time"], item["timezone"],
        )
        results.append((item["ticket_key"], item["hours"], ok, err))

    for m in draft["manual_items"].values():
        if m["ticket_key"] and m["ticket_key"] != "N/A" and m["ticket_key"].upper() in ALLOWED_TICKET_KEYS:
            ok, err = jira_add_worklog(
                m["ticket_key"], m["hours"], f"[{m['category']}] {m['description']}",
                draft["date"], "09:00", m["timezone"],
            )
            results.append((m["ticket_key"] or m["category"], m["hours"], ok, err))
        else:
            # no valid ticket to log against — record locally only
            results.append((m["category"], m["hours"], None, "no matching ticket, logged as note only"))

    draft["submitted"] = True
    lines = ["✅ *Submitted.*"]
    for key, hours, ok, err in results:
        if ok is True:
            lines.append(f"• {key}: {hours}h logged to Jira")
        elif ok is False:
            lines.append(f"• {key}: {hours}h *failed* — {err}")
        else:
            lines.append(f"• {key}: {hours}h — {err}")
    slack_api("chat.update", channel=draft["channel"], ts=draft["ts"],
              text="Timesheet submitted",
              blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
