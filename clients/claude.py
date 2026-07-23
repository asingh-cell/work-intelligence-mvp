"""Calls Claude to turn raw Granola evidence into scored, classified claims.

Anti-hallucination contract (do not weaken this prompt without re-reading
the design principles in the architecture doc):
  - executed_work requires a stated, completed outcome — not intent.
  - No invented ticket keys, durations, or outcomes.
  - Ambiguous evidence -> requires_review = true, never a guess.
"""
import json
from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an evidence-based work reconstruction assistant. \
You are given meeting evidence for one consultant for one day. Your task:

1. Extract discrete claims of work discussed or performed, each with a timestamp \
if available.
2. Classify each claim as one of: discussion, planning_decision, executed_work. \
Only classify as executed_work if the language describes completed action with \
a clear outcome (e.g. "script is complete and passed staging tests"), not intent \
or future action (e.g. "will deploy Friday" is planning_decision, not executed_work).
3. Attempt to correlate each claim to a Jira ticket using any ticket key mentioned \
in the evidence. If no ticket key is present, set ticket_match to null — do not \
guess a ticket key.
4. Assign a confidence_score 0-100 based on how directly the evidence supports the \
executed_work classification (or the ticket match, if present).
5. Never invent a ticket key, duration, or outcome not present in the evidence.
6. If evidence is ambiguous or insufficient to support executed_work, set \
requires_review = true rather than guessing.

Return ONLY a JSON object matching this schema, no other text:
{
  "claims": [
    {
      "claim_text": string,
      "classification": "discussion" | "planning_decision" | "executed_work",
      "source_timestamp": string | null,
      "ticket_match": {"ticket_key": string, "match_confidence": number} | null,
      "suggested_hours": number,
      "confidence_score": number,
      "requires_review": boolean
    }
  ]
}

Note on calendar_events (if present): these are meeting metadata only — title, \
time, attendees — with no content. A calendar event existing is NEVER evidence \
that work was discussed or completed; use it only to help place a "notes" claim \
in time, or to note an unmapped block of time with no corresponding notes \
(which should be a low-confidence claim flagged requires_review, not assumed \
to be any particular kind of work)."""


def analyze_evidence(consultant_email: str, run_date: str, notes: list[dict], calendar_events: list[dict] | None = None) -> dict:
    """Send the day's Granola notes (and optionally Calendar events) to Claude
    and return structured claims.

    Raises ValueError if Claude's response isn't valid JSON matching the
    schema — callers should treat that as "no auto-write, escalate everything".
    """
    evidence_payload = {
        "consultant": consultant_email,
        "date": run_date,
        "notes": [
            {
                "title": n.get("title"),
                "created_at": n.get("created_at"),
                "summary": n.get("summary") or n.get("ai_summary"),
                "transcript_excerpt": (n.get("transcript") or "")[:8000],
            }
            for n in notes
        ],
        "calendar_events": calendar_events or [],
    }

    response = client.messages.create(
        model="claude-sonnet-5",  # swap for whichever current model string your org uses
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(evidence_payload)}],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude did not return valid JSON: {e}\nRaw: {text[:500]}")
