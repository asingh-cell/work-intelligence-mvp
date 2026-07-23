"""Per-consultant reconstruction pipeline. Isolated failures: one
consultant's error is logged and does not stop the others (see the
failure-handling table in the architecture doc).

Approval model: nothing is written to Jira automatically, regardless of
confidence score. Every claim with a resolved ticket match becomes a
pending draft with an Approve/Skip button in Slack. The actual Jira write
only happens when the consultant clicks Approve — see app.py's
/slack/interactions endpoint for that half of the flow.
"""
import logging
import uuid

from config import DEFAULT_JIRA_PROJECT
from clients import granola, claude, jira, slack, calendar, tempo
import store

log = logging.getLogger(__name__)


def run_for_consultant(consultant: dict, run_date: str, manual_notes: list[dict] | None = None) -> dict:
    email = consultant["email"]
    name = consultant["name"]

    notes = manual_notes if manual_notes is not None else granola.notes_for_consultant(run_date, email)
    calendar_events = calendar.list_events_for_day(run_date)

    if not notes and not calendar_events:
        log.info("No evidence at all for %s on %s", email, run_date)
        slack.send_text_dm(
            consultant["slack_user_id"],
            f"*Good evening, {name}!* No evidence found for {run_date}. Nothing to reconstruct.",
        )
        return {"email": email, "drafted": [], "informational": []}

    analysis = claude.analyze_evidence(email, run_date, notes, calendar_events=calendar_events)
    claims = analysis.get("claims", [])

    drafted, informational = [], []

    for claim in claims:
        if claim["classification"] != "executed_work":
            # Discussion / planning claims are never actionable as completed
            # work — informational only, same as before.
            continue

        ticket_match = claim.get("ticket_match")
        if not ticket_match:
            informational.append({**claim, "reason": "no ticket match found"})
            continue

        candidates = jira.search_candidate_tickets(
            DEFAULT_JIRA_PROJECT, ticket_match.get("ticket_key"), claim["claim_text"][:60]
        )
        if not candidates:
            informational.append({**claim, "reason": "ticket key not found in Jira"})
            continue

        ticket_key = candidates[0]["key"]

        draft_id = uuid.uuid4().hex[:12]
        store.save_draft(
            draft_id,
            {
                "consultant_email": email,
                "ticket_key": ticket_key,
                "claim_text": claim["claim_text"],
                "suggested_hours": claim.get("suggested_hours", 0),
                "confidence_score": claim["confidence_score"],
                "run_date": run_date,
                "source_timestamp": claim.get("source_timestamp"),
            },
        )
        drafted.append(
            {
                "draft_id": draft_id,
                "ticket_key": ticket_key,
                "claim_text": claim["claim_text"],
                "suggested_hours": claim.get("suggested_hours", 0),
                "confidence_score": claim["confidence_score"],
            }
        )

    hours_summary = None
    jira_account_id = consultant.get("jira_account_id")
    if jira_account_id:
        logged = tempo.get_logged_hours(jira_account_id, run_date, run_date)
        planned = tempo.get_planned_hours(jira_account_id, run_date, run_date)
        if logged is not None or planned is not None:
            hours_summary = {"logged": logged, "planned": planned}

    slack.send_review_message(consultant["slack_user_id"], name, run_date, drafted, informational, hours_summary)

    return {"email": email, "drafted": drafted, "informational": informational}
