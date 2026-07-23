import hashlib
import hmac
import json
import logging
import time
from datetime import date
from urllib.parse import parse_qs

from flask import Flask, request, jsonify

from config import CONSULTANTS, TRIGGER_SHARED_SECRET, SLACK_SIGNING_SECRET
from pipeline import run_for_consultant
from clients import jira, slack
import store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)


@app.post("/trigger")
def trigger():
    if request.headers.get("X-Trigger-Secret") != TRIGGER_SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    run_date = payload.get("run_date") or date.today().isoformat()
    manual_notes = payload.get("manual_evidence")

    results = []
    for consultant in CONSULTANTS:
        email = consultant["email"]

        if store.already_ran(email, run_date):
            log.info("Skipping %s for %s — already ran (idempotency)", email, run_date)
            results.append({"email": email, "skipped": "already_ran"})
            continue

        try:
            result = run_for_consultant(consultant, run_date, manual_notes=manual_notes)
            store.mark_ran(email, run_date)
            results.append(result)
        except Exception as e:
            log.exception("Run failed for %s", email)
            results.append({"email": email, "error": str(e)})

    return jsonify({"run_date": run_date, "results": results})


def _verify_slack_signature(raw_body: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        return False
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    if not timestamp or abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{raw_body}".encode()
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    slack_signature = request.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(computed, slack_signature)


@app.post("/slack/interactions")
def slack_interactions():
    raw_body = request.get_data(as_text=True)

    if not _verify_slack_signature(raw_body):
        return jsonify({"error": "invalid signature"}), 401

    parsed = parse_qs(raw_body)
    payload = json.loads(parsed.get("payload", ["{}"])[0])
    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    response_url = payload.get("response_url")

    # Select-menu interactions (leave, missing-hours allocation) carry their
    # context in the option value itself, not a draft_id — handle separately.
    if action_id in ("leave_selection", "missing_hours_allocation"):
        selected_value = (action.get("selected_option") or {}).get("value", "")
        try:
            consultant_email, run_date, choice = selected_value.split("::", 2)
        except ValueError:
            slack.ack_selection_via_response_url(response_url, "Couldn't read that selection — please try again.")
            return "", 200

        field = "leave" if action_id == "leave_selection" else "missing_hours_allocation"
        store.save_day_meta(consultant_email, run_date, field, choice)

        if field == "leave":
            slack.ack_selection_via_response_url(response_url, f"Got it — leave recorded: {choice.replace('_', ' ')}.")
        else:
            slack.ack_selection_via_response_url(
                response_url,
                f"Got it — remaining hours logged as: {choice.replace('_', ' ')}. "
                f"(Recorded internally — this doesn't push to Tempo yet, that's a future step.)",
            )
        return "", 200

    draft_id = action.get("value")

    draft = store.get_draft(draft_id)
    if not draft:
        slack.update_message_via_response_url(response_url, "This draft is no longer available.")
        return "", 200

    if draft.get("_status") != "pending":
        slack.update_message_via_response_url(response_url, f"Already handled: {draft['_status']}.")
        return "", 200

    if action_id == "approve_draft":
        try:
            jira.add_comment(
                draft["ticket_key"],
                f"{draft['claim_text']} (source: Granola, {draft['run_date']}, "
                f"confidence {draft['confidence_score']}%, approved via Slack)",
            )
            jira.add_worklog(
                draft["ticket_key"],
                draft.get("source_timestamp") or f"{draft['run_date']}T09:00:00.000+0000",
                f"{draft.get('suggested_hours', 0)}h",
                draft["claim_text"][:200],
            )
            store.set_draft_status(draft_id, "approved")
            slack.update_message_via_response_url(response_url, f"✔ Approved — written to {draft['ticket_key']}.")
        except Exception as e:
            log.exception("Failed to write approved draft %s", draft_id)
            slack.update_message_via_response_url(response_url, f"Failed to write to Jira: {e}")
    elif action_id == "skip_draft":
        store.set_draft_status(draft_id, "skipped")
        slack.update_message_via_response_url(response_url, f"Skipped — nothing written for {draft['ticket_key']}.")

    return "", 200


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
