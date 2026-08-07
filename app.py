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
from datetime import datetime, timedelta, timezone

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

# Only messages containing this marker get pulled into the pipeline at all —
# everything else in the channel is invisible to this app. Off by default
# behavior would be "read everything"; requiring an explicit tag is the
# safer default for channels that carry sensitive context.
EVIDENCE_MARKER = os.environ.get("EVIDENCE_MARKER", "@jira").lower()

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
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# In-memory draft store, backed by a best-effort JSON file so a review that
# takes longer than a few minutes has a chance of surviving a process
# restart (Render redeploy, or the free tier spinning down after idle).
# ponytail: single flat file, whole-dict last-write-wins — fine for one
# person's timesheet flow, not safe for concurrent multi-writer load. Also:
# I can't verify from here whether Render's free tier preserves local disk
# across a full spin-down (vs. just a redeploy) — test it empirically. If a
# draft still doesn't survive an hour-long gap after this, the reliable fix
# is a paid Render plan with a persistent disk, or an external store (e.g.
# Postgres) — ask and I'll wire it up.
# ---------------------------------------------------------------------------
PENDING_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_drafts.json")


def load_pending() -> dict:
    try:
        with open(PENDING_STORE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_pending():
    try:
        with open(PENDING_STORE_PATH, "w") as f:
            json.dump(PENDING, f)
    except OSError as e:
        log.error("Couldn't persist PENDING to disk: %s", e)


PENDING = load_pending()  # draft_id -> draft dict


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
    configured evidence channels for the given YYYY-MM-DD date, anchored to
    IST midnight regardless of what timezone the server itself runs in."""
    day = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=IST)
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
                if (msg.get("user") == slack_user_id and msg.get("text")
                        and EVIDENCE_MARKER in msg["text"].lower()):
                    collected.append({"channel": channel_id, "text": msg["text"], "ts": msg["ts"]})
                # thread replies
                if msg.get("reply_count"):
                    replies = slack_api(
                        "conversations.replies", channel=channel_id, ts=msg["ts"]
                    )
                    for r in replies.get("messages", []):
                        if (r.get("user") == slack_user_id and r.get("text") and r["ts"] != msg["ts"]
                                and EVIDENCE_MARKER in r["text"].lower()):
                            collected.append({"channel": channel_id, "text": r["text"], "ts": r["ts"]})
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    return collected


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
- hours: ONLY if the message states or clearly implies a duration (e.g. "spent 2 hours on X",
  "1.5h fixing Y", a start and end time). If no duration is stated anywhere, set hours to 0 —
  do not guess a plausible-sounding number. A fabricated hour count is worse than a blank one;
  the person will fill it in themselves when they see hours is 0.
- confidence: 0-100. If hours had to be set to 0 because none was stated, cap this at 40 regardless
  of how confident you are about the ticket match — the missing duration is the limiting factor.
- start_time: 24h HH:MM in {tz} local time. Only use an explicit clock time mentioned in the
  message; otherwise use "09:00" as a neutral placeholder. Do not output end_time — it's computed
  separately from start_time and hours, so it always stays consistent with the duration.
Do not invent tickets outside the allowed list. Do not exceed a combined total of {daily_target} hours
unless the evidence clearly supports more.

Messages:
{messages}

Respond with ONLY a JSON array of objects with exactly these keys:
ticket_key, ticket_summary, description, hours, confidence, start_time
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
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text").strip()
    if not raw:
        log.error(
            "Claude returned no usable text — stop_reason=%s, block types=%s. "
            "If stop_reason is 'max_tokens', the response got cut off before finishing.",
            resp.stop_reason, [b.type for b in resp.content],
        )
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


def jira_get_ticket_options(keys: set) -> list[dict]:
    """Dropdown options built from your ALLOWED_TICKET_KEYS, with live
    summaries pulled from Jira. Add a ticket to ALLOWED_TICKET_KEYS in Render
    and it shows up here automatically — no code change needed. Best-effort
    per ticket: a summary fetch failure just shows the key with no summary
    rather than dropping the ticket from the list."""
    options = []
    for key in sorted(keys):
        summary = ""
        try:
            resp = requests.get(
                f"{JIRA_SITE}/rest/api/3/issue/{key}",
                auth=jira_auth(), params={"fields": "summary"}, timeout=10,
            )
            if resp.status_code < 300:
                summary = resp.json().get("fields", {}).get("summary", "")
        except requests.RequestException as e:
            log.error("jira_get_ticket_options: couldn't fetch %s: %s", key, e)
        options.append({"key": key, "summary": summary})
    return options


def jira_add_worklog(issue_key: str, hours: float, comment: str, target_date: str,
                      start_time: str, tz: str, worklog_id: str = None):
    """Creates a new worklog, or updates one in place if worklog_id is given
    (used when re-submitting an item that was already logged, so editing and
    resubmitting doesn't create duplicate worklog entries in Jira)."""
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
    url = f"{JIRA_SITE}/rest/api/3/issue/{issue_key}/worklog"
    if worklog_id:
        url += f"/{worklog_id}"
    try:
        resp = (requests.put if worklog_id else requests.post)(url, auth=jira_auth(), json=body, timeout=15)
    except requests.RequestException as e:
        log.error("Jira request errored for %s: %s", issue_key, e)
        return False, str(e), None
    if resp.status_code >= 300:
        log.error("Jira worklog failed for %s: %s %s", issue_key, resp.status_code, resp.text)
        return False, resp.text[:300], None
    return True, None, resp.json().get("id", worklog_id)


# ---------------------------------------------------------------------------
# Business logic shared helpers (kept pure so they're easy to test — see test_app.py)
# ---------------------------------------------------------------------------
def add_hours_to_time(hhmm: str, hours: float) -> str:
    """'14:30' + 1.5 -> '16:00'. ponytail: doesn't roll over past midnight
    (clamps to 23:59) — fine for logging a single day's work, not meant for
    overnight shifts."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        h, m = 9, 0
    total_minutes = min(h * 60 + m + round(hours * 60), 23 * 60 + 59)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def required_hours_for_leave(leave_status: str) -> float:
    return {"full": 0.0, "half": DAILY_TARGET_HOURS / 2}.get(leave_status, DAILY_TARGET_HOURS)


def confidence_label(confidence: float) -> str:
    if confidence >= AUTO_WRITE_THRESHOLD:
        return "High"
    if confidence >= AUTO_WRITE_THRESHOLD - 30:
        return "Medium"
    return "Low"


def logged_hours(draft: dict) -> float:
    return sum(item["hours"] for item in draft["items"].values() if item["status"] in ("approved", "edited"))


def progress_bar(logged: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "█" * width
    filled = min(width, round(width * logged / target))
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Slack Block Kit rendering
# ---------------------------------------------------------------------------
MANUAL_CATEGORIES = [
    ("existing", "Existing ticket"),
    ("internal", "Internal work"),
    ("learning", "Learning"),
    ("meetings", "Meetings"),
    ("admin", "Administration"),
    ("other", "Other"),
]


def render_draft_blocks(draft: dict) -> list:
    target = required_hours_for_leave(draft["leave_status"])
    logged = logged_hours(draft)
    has_unsynced = any(
        item["status"] in ("approved", "edited") and not item.get("synced")
        for item in draft["items"].values()
    )
    fully_done = not has_unsynced and bool(draft.get("submission_summary"))

    if fully_done:
        # Nothing left to review or send — collapse to a clean receipt instead
        # of leaving a form full of dead buttons on screen.
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"All set for *{draft['date']}* ✓"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": draft["submission_summary"]}},
        ]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Evening! Here's what I found for *{draft['date']}*. "
                    f"Take a look, then hit Submit whenever you're ready — "
                    f"nothing touches Jira until then."
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{progress_bar(logged, target)}  *{logged:.1f}h / {target:.1f}h*"
                + (f"  _(leave: {draft['leave_status']}"
                   + (f" — {draft['leave_note']}" if draft.get("leave_note") else "")
                   + ")_" if draft["leave_status"] != "none" else ""),
            },
        },
        {"type": "divider"},
    ]
    if draft["leave_status"] == "other":
        blocks.append({
            "type": "actions",
            "block_id": f"leave_note_controls|{draft['id']}",
            "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": "Add/edit note" if draft.get("leave_note") else "Add a note"},
                 "action_id": "edit_leave_note", "value": draft["id"]},
            ],
        })

    for item_id, item in draft["items"].items():
        if item["status"] == "skipped":
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"~{item['ticket_key']} — {item['ticket_summary']}~  _Skipped_"},
            })
            blocks.append({
                "type": "actions",
                "block_id": f"item_actions|{draft['id']}|{item_id}",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Undo"},
                     "action_id": "undo_item", "value": f"{draft['id']}|{item_id}"},
                ],
            })
            continue

        if item["ticket_key"] in ALLOWED_TICKET_KEYS:
            ticket_display = f"<{JIRA_SITE}/browse/{item['ticket_key']}|{item['ticket_key']}>"
        else:
            ticket_display = item["ticket_key"]
        header = f"*{ticket_display}* — {item['ticket_summary']}"
        if item["ticket_key"] not in ALLOWED_TICKET_KEYS and item["ticket_key"] not in ("N/A", "UNMATCHED"):
            header += "  _(Not in your allowed ticket list — check before approving)_"

        status_line = ""
        if item["status"] in ("approved", "edited"):
            status_line = "  ·  *Approved*" if item["status"] == "approved" else "  ·  *Edited*"
        if item.get("jira_worklog_id"):
            status_line += "  ·  *Logged to Jira*"

        confidence_text = (
            "Manually logged" if item.get("source") == "manual"
            else f"{item['confidence']:.0f}% confidence ({confidence_label(item['confidence'])})"
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{header}{status_line}"},
        })
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"{item['hours']}h  ·  {confidence_text}  ·  {item['start_time']}–{item['end_time']} {item['timezone']}",
            }],
        })
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {item['description']}"},
        })
        if item["status"] not in ("approved", "edited"):
            blocks.append({
                "type": "actions",
                "block_id": f"item_actions|{draft['id']}|{item_id}",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
                     "style": "primary", "action_id": "approve_item", "value": f"{draft['id']}|{item_id}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Edit"},
                     "action_id": "edit_item", "value": f"{draft['id']}|{item_id}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Skip"},
                     "style": "danger", "action_id": "skip_item", "value": f"{draft['id']}|{item_id}"},
                ],
            })
        elif not item.get("synced"):
            blocks.append({
                "type": "actions",
                "block_id": f"item_actions|{draft['id']}|{item_id}",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Edit"},
                     "action_id": "edit_item", "value": f"{draft['id']}|{item_id}"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Undo"},
                     "action_id": "undo_item", "value": f"{draft['id']}|{item_id}"},
                ],
            })
        # else: already synced — nothing more to do with this one here; to
        # correct it after this point, edit the worklog directly in Jira.

    remaining = max(0.0, target - logged)
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{remaining:.1f}h* still unaccounted for. On leave, or want to log it yourself?"},
    })
    blocks.append({
        "type": "actions",
        "block_id": f"bottom_controls|{draft['id']}",
        "elements": [
            {
                "type": "static_select",
                "action_id": "manual_category",
                "placeholder": {"type": "plain_text", "text": "Log the rest — pick a category"},
                "options": [
                    {"text": {"type": "plain_text", "text": label}, "value": key}
                    for key, label in MANUAL_CATEGORIES
                ],
            },
            {
                "type": "static_select",
                "action_id": "leave_status",
                "placeholder": {"type": "plain_text", "text": "Leave status"},
                "options": [
                    {"text": {"type": "plain_text", "text": "No leave"}, "value": "none"},
                    {"text": {"type": "plain_text", "text": "Half-day leave"}, "value": "half"},
                    {"text": {"type": "plain_text", "text": "Full-day leave"}, "value": "full"},
                    {"text": {"type": "plain_text", "text": "WFH"}, "value": "wfh"},
                    {"text": {"type": "plain_text", "text": "Other"}, "value": "other"},
                ],
            },
        ],
    })

    blocks.append({"type": "divider"})
    if has_unsynced:
        blocks.append({
            "type": "actions",
            "block_id": f"submit|{draft['id']}",
            "elements": [
                {"type": "button",
                 "text": {"type": "plain_text",
                          "text": "Submit changes" if draft.get("submission_summary") else "Submit to Jira"},
                 "style": "primary", "action_id": "submit_final", "value": draft["id"]},
            ],
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_Nothing new to submit — Edit, Undo, or add an entry above to send more._"},
        })
    if draft.get("submission_summary"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": draft["submission_summary"]}})
    return blocks


NO_TICKET_SENTINEL = "__NONE__"


def _build_time_options():
    options = []
    for total_min in range(0, 24 * 60, 15):
        h24, m = divmod(total_min, 60)
        suffix = "AM" if h24 < 12 else "PM"
        h12 = h24 % 12 or 12
        options.append((f"{h24:02d}:{m:02d}", f"{h12}:{m:02d} {suffix}"))
    return options


TIME_OPTIONS = _build_time_options()  # 96 entries, 15-min increments, fits Slack's 100-option cap


def nearest_time_slot(hhmm: str) -> str:
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        h, m = 9, 0
    snapped = (round((h * 60 + m) / 15) * 15) % (24 * 60)
    return f"{snapped // 60:02d}:{snapped % 60:02d}"


def time_select_block(block_id: str, label: str, current_value: str, action_id: str = "value") -> dict:
    options = [{"text": {"type": "plain_text", "text": lbl}, "value": val} for val, lbl in TIME_OPTIONS]
    initial = next(o for o in options if o["value"] == nearest_time_slot(current_value))
    return {
        "type": "input", "block_id": block_id,
        "label": {"type": "plain_text", "text": label},
        "element": {"type": "static_select", "action_id": action_id, "options": options, "initial_option": initial},
    }


def custom_time_input_block(block_id: str) -> dict:
    """Fallback for anyone who'd rather type an exact time than scroll a
    15-minute-increment dropdown. Optional — leave blank to use the dropdown."""
    return {
        "type": "input", "block_id": block_id, "optional": True,
        "label": {"type": "plain_text", "text": "Or type a time (e.g. 2:15 PM, or 14:15)"},
        "element": {"type": "plain_text_input", "action_id": "value"},
    }


def parse_custom_time(text: str) -> str | None:
    """'2:15 PM' / '2:15pm' / '9am' / '14:15' -> '14:15'. None if unparseable
    or blank, so callers can cleanly fall back to the dropdown value."""
    if not text or not text.strip():
        return None
    t = text.strip().upper().replace(" ", "")
    m = re.match(r"^(\d{1,2}):?(\d{2})?(AM|PM)?$", t)
    if not m:
        return None
    h = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = m.group(3)
    if suffix:
        h = h % 12
        if suffix == "PM":
            h += 12
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        return None
    return f"{h:02d}:{minute:02d}"


def ticket_dropdown_block(ticket_options: list, initial_key: str = None) -> dict:
    """The sole ticket-selection control in the edit/manual-entry modals —
    sourced from ALLOWED_TICKET_KEYS (add a ticket there and it shows up
    here automatically), with a 'no ticket' fallback always available."""
    ticket_options = ticket_options or []
    options = [
        {"text": {"type": "plain_text", "text": "No ticket"}, "value": NO_TICKET_SENTINEL},
    ] + [
        {"text": {"type": "plain_text", "text": f"{t['key']} — {t['summary']}"[:75] if t["summary"] else t["key"]},
         "value": t["key"]}
        for t in ticket_options[:99]  # Slack's static_select option limit is 100
    ]
    block = {
        "type": "input", "block_id": "ticket_key",
        "label": {"type": "plain_text", "text": "Ticket"},
        "element": {"type": "static_select", "action_id": "value", "options": options},
    }
    match = next((o for o in options if o["value"] == initial_key), options[0])
    block["element"]["initial_option"] = match
    return block


def edit_modal(draft_id, item_id, item, ticket_options=None, overrides=None):
    v = {**item, **(overrides or {})}
    blocks = [
        ticket_dropdown_block(ticket_options, initial_key=v["ticket_key"]),
        {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "Title"},
         "element": {"type": "plain_text_input", "action_id": "value", "initial_value": v["ticket_summary"]}},
        {"type": "input", "block_id": "description", "label": {"type": "plain_text", "text": "Description"},
         "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                     "initial_value": v["description"]}},
        {"type": "input", "block_id": "hours", "label": {"type": "plain_text", "text": "Hours"},
         "element": {"type": "number_input", "action_id": "live_hours", "is_decimal_allowed": True,
                     "initial_value": str(v["hours"]),
                     "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]}}},
        time_select_block("start_time", "Start", v["start_time"], action_id="live_start_time"),
        custom_time_input_block("start_time_custom"),
        time_select_block("end_time", "End (auto-updates from Start + Hours, or set it yourself)", v["end_time"]),
        custom_time_input_block("end_time_custom"),
        {"type": "input", "block_id": "timezone", "label": {"type": "plain_text", "text": "Timezone"},
         "element": {"type": "static_select", "action_id": "value",
                     "initial_option": {"text": {"type": "plain_text", "text": v["timezone"]}, "value": v["timezone"]},
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


def leave_note_modal(draft_id, current_note=""):
    return {
        "type": "modal",
        "callback_id": "leave_note_submit",
        "private_metadata": draft_id,
        "title": {"type": "plain_text", "text": "Leave note"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {"type": "input", "block_id": "note", "label": {"type": "plain_text", "text": "What's the reason?"},
             "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                         "initial_value": current_note}},
        ],
    }


def manual_modal(draft_id, category_key, category_label, ticket_options=None):
    blocks = [
        ticket_dropdown_block(ticket_options),
        {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "Title"},
         "element": {"type": "plain_text_input", "action_id": "value", "initial_value": category_label}},
        {"type": "input", "block_id": "description", "label": {"type": "plain_text", "text": "What did you do?"},
         "element": {"type": "plain_text_input", "action_id": "value", "multiline": True}},
        time_select_block("start_time", "Start time", "09:00"),
        custom_time_input_block("start_time_custom"),
        {"type": "input", "block_id": "hours", "label": {"type": "plain_text", "text": "How many hours?"},
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
    save_pending()
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
    target_date = body.get("date") or datetime.now(IST).date().isoformat()
    ticket_options = jira_get_ticket_options(ALLOWED_TICKET_KEYS)

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
        hours = float(row.get("hours", 0) or 0)
        start_time = row.get("start_time", "09:00")
        items[item_id] = {
            "ticket_key": str(row.get("ticket_key", "UNMATCHED")).upper(),
            "ticket_summary": row.get("ticket_summary", ""),
            "description": row.get("description", ""),
            "hours": hours,
            "confidence": float(row.get("confidence", 0) or 0),
            "start_time": start_time,
            "end_time": add_hours_to_time(start_time, hours),
            "timezone": DEFAULT_TZ,
            "status": "pending",
            "jira_worklog_id": None,
            "synced": False,
        }

    draft = {
        "id": draft_id,
        "slack_user_id": slack_user_id,
        "date": target_date,
        "items": items,
        "leave_status": "none",
        "submitted": False,
        "ticket_options": ticket_options,
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
    save_pending()

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
                                          ticket_options=draft.get("ticket_options")))

        elif action_id == "leave_status":
            draft_id = payload["actions"][0]["block_id"].split("|")[1]
            draft = PENDING.get(draft_id)
            if draft:
                draft["leave_status"] = action["selected_option"]["value"]
                update_draft_message(draft)
                if draft["leave_status"] == "other":
                    slack_api("views.open", trigger_id=trigger_id,
                              view=leave_note_modal(draft_id, draft.get("leave_note", "")))

        elif action_id == "edit_leave_note":
            draft_id = action["value"]
            draft = PENDING.get(draft_id)
            if draft:
                slack_api("views.open", trigger_id=trigger_id,
                          view=leave_note_modal(draft_id, draft.get("leave_note", "")))

        elif action_id == "manual_category":
            draft_id = payload["actions"][0]["block_id"].split("|")[1]
            key = action["selected_option"]["value"]
            label = dict(MANUAL_CATEGORIES)[key]
            draft = PENDING.get(draft_id)
            slack_api("views.open", trigger_id=trigger_id,
                      view=manual_modal(draft_id, key, label,
                                        ticket_options=draft.get("ticket_options") if draft else None))

        elif action_id in ("live_start_time", "live_hours"):
            draft_id, item_id = payload["view"]["private_metadata"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                v = payload["view"]["state"]["values"]
                current_start = v["start_time"]["value"]["selected_option"]["value"]
                try:
                    current_hours = float(v["hours"]["value"]["value"] or 0)
                except (TypeError, ValueError):
                    current_hours = 0.0
                overrides = {
                    "ticket_key": v["ticket_key"]["value"]["selected_option"]["value"],
                    "ticket_summary": v["title"]["value"]["value"],
                    "description": v["description"]["value"]["value"],
                    "hours": current_hours,
                    "start_time": current_start,
                    "end_time": add_hours_to_time(current_start, current_hours),
                    "timezone": v["timezone"]["value"]["selected_option"]["value"],
                }
                new_view = edit_modal(draft_id, item_id, draft["items"][item_id],
                                       ticket_options=draft.get("ticket_options"), overrides=overrides)
                slack_api("views.update", view_id=payload["view"]["id"],
                          hash=payload["view"]["hash"], view=new_view)

        elif action_id == "submit_final":
            draft_id = action["value"]
            draft = PENDING.get(draft_id)
            if draft:
                submit_to_jira(draft)

        return "", 200

    if ptype == "view_submission":
        callback_id = payload["view"]["callback_id"]
        values = payload["view"]["state"]["values"]

        if callback_id == "leave_note_submit":
            draft_id = payload["view"]["private_metadata"]
            draft = PENDING.get(draft_id)
            if draft:
                draft["leave_note"] = values["note"]["value"]["value"]
                update_draft_message(draft)

        elif callback_id == "edit_item_submit":
            draft_id, item_id = payload["view"]["private_metadata"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                item = draft["items"][item_id]
                selected = values["ticket_key"]["value"]["selected_option"]["value"]
                item["ticket_key"] = "UNMATCHED" if selected == NO_TICKET_SENTINEL else selected
                item["ticket_summary"] = values["title"]["value"]["value"]
                item["description"] = values["description"]["value"]["value"]
                item["hours"] = float(values["hours"]["live_hours"]["value"] or 0)
                custom_start = parse_custom_time(values.get("start_time_custom", {}).get("value", {}).get("value"))
                custom_end = parse_custom_time(values.get("end_time_custom", {}).get("value", {}).get("value"))
                item["start_time"] = custom_start or values["start_time"]["value"]["selected_option"]["value"]
                item["end_time"] = custom_end or values["end_time"]["value"]["selected_option"]["value"]
                item["timezone"] = values["timezone"]["value"]["selected_option"]["value"]
                item["status"] = "edited"
                item["synced"] = False
                update_draft_message(draft)

        elif callback_id == "manual_entry_submit":
            draft_id, key, label = payload["view"]["private_metadata"].split("|")
            draft = PENDING.get(draft_id)
            if draft:
                selected = values["ticket_key"]["value"]["selected_option"]["value"]
                ticket_key = "N/A" if selected == NO_TICKET_SENTINEL else selected
                hours = float(values["hours"]["value"]["value"] or 0)
                custom_start = parse_custom_time(values.get("start_time_custom", {}).get("value", {}).get("value"))
                start_time = custom_start or values["start_time"]["value"]["selected_option"]["value"]
                item_id = new_item_id()
                draft["items"][item_id] = {
                    "ticket_key": ticket_key,
                    "ticket_summary": values["title"]["value"]["value"] or label,
                    "description": values["description"]["value"]["value"],
                    "hours": hours,
                    "confidence": 100.0,
                    "start_time": start_time,
                    "end_time": add_hours_to_time(start_time, hours),
                    "timezone": values["timezone"]["value"]["selected_option"]["value"],
                    "status": "approved",
                    "source": "manual",
                    "jira_worklog_id": None,
                    "synced": False,
                }
                update_draft_message(draft)

        return "", 200

    return "", 200


def build_receipt_table(results: list) -> str:
    headers = ["TICKET", "HOURS", "STATUS", "WHAT"]
    rows = []
    for r in results:
        status = "Logged" if r["ok"] is True else ("Failed" if r["ok"] is False else "No ticket")
        what = r["description"][:36] + ("…" if len(r["description"]) > 36 else "")
        rows.append([r["key"], f"{r['hours']}h", status, what])
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(4)]
    fmt_row = lambda row: "  ".join(cell.ljust(w) for cell, w in zip(row, widths))
    lines = [fmt_row(headers), "  ".join("-" * w for w in widths)] + [fmt_row(r) for r in rows]
    return "```\n" + "\n".join(lines) + "\n```"


def submit_to_jira(draft):
    results = []  # dicts: key, description, hours, ok, err
    for item in draft["items"].values():
        if item["status"] not in ("approved", "edited"):
            continue
        if item["ticket_key"] not in ALLOWED_TICKET_KEYS:
            # no real ticket to log against — nothing more to do with this one
            # until it's edited to add one, so treat it as resolved for now
            results.append({"key": item["ticket_key"] or item["ticket_summary"],
                             "description": item["description"], "hours": item["hours"],
                             "ok": None, "err": None})
            item["synced"] = True
            continue
        comment = (
            f"{item['description']}\n\n"
            f"(Logged as {item['start_time']}\u2013{item['end_time']} {item['timezone']}, {draft['date']} — "
            f"if this displays at a different date/time here, check your Jira profile's Time zone setting.)"
        )
        ok, err, worklog_id = jira_add_worklog(
            item["ticket_key"], item["hours"], comment,
            draft["date"], item["start_time"], item["timezone"],
            worklog_id=item.get("jira_worklog_id"),
        )
        if ok:
            item["jira_worklog_id"] = worklog_id
            item["synced"] = True
        results.append({"key": item["ticket_key"], "description": item["description"],
                         "hours": item["hours"], "ok": ok, "err": err})

    logged_now = sum(r["hours"] for r in results if r["ok"] is True)
    count = len(results)
    if count == 0:
        draft["submission_summary"] = "Nothing new to submit this round."
    else:
        entry_word = "entry" if count == 1 else "entries"
        summary_lines = [f"✓ Logged *{logged_now:.1f}h* across {count} {entry_word}:", build_receipt_table(results)]
        failed = [r for r in results if r["ok"] is False]
        if failed:
            summary_lines.append(
                f"Heads up — {len(failed)} didn't go through. See the STATUS column above for why."
            )
        draft["submission_summary"] = "\n".join(summary_lines)

    draft["submitted"] = True
    update_draft_message(draft)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
