# Work Intelligence Assistant — MVP (Granola-only)

Standalone webhook service that reconstructs a consultant's day from Granola
meeting evidence and writes a comment + worklog to Jira, then sends a Slack
review DM. This is the real orchestrator standing in for the manual steps
run earlier in chat — same evidence pipeline, same anti-hallucination rules,
now runnable unattended.

## How it's triggered

A **global Jira Automation rule** (Scheduled trigger, weekdays 18:00) calls
**Send web request** → `POST https://<your-host>/trigger` with:

```json
{ "event": "daily_work_reconstruction", "run_date": "2026-07-23" }
```

Add a custom header `X-Trigger-Secret: <shared secret>` in the automation
rule's web request config — the service rejects requests without it.

## What it does, per consultant in `config.py`

1. **Collect** — `clients/granola.py` lists that day's Granola notes for the
   consultant and pulls the full transcript summary for each.
2. **Reason** — `clients/claude.py` sends the evidence to Claude with the
   evidence-extraction/classification/validation prompt (same one used in
   the architecture doc). Claude returns structured JSON: claims, each
   classified as `discussion` / `planning_decision` / `executed_work`, with
   a ticket match attempt and a confidence score. Nothing is invented —
   `executed_work` is only assigned when the evidence states a completed
   outcome, not an intention.
3. **Match** — if `ticket_match.match_confidence` is low or absent, the
   pipeline does **not** guess. It surfaces the claim as unmapped in the
   Slack DM instead of writing to a ticket.
4. **Write** — `clients/jira.py` adds a comment and worklog only for claims
   classified `executed_work` with `confidence_score >= AUTO_WRITE_THRESHOLD`
   (default 85, see `config.py`). Everything else goes into the Slack DM
   for a manual decision — never silently dropped, never silently written.
5. **Notify** — `clients/slack.py` sends one consolidated DM per consultant:
   what was written automatically, what needs a decision, and the
   missing-hours / leave prompts from the architecture doc (stubbed here —
   wire up Tempo in Phase 2 per the roadmap).
6. **Idempotency** — `store.py` is a tiny SQLite-backed key-value store keyed
   on `(consultant_email, run_date)`. Re-firing the same day's trigger is a
   no-op after the first successful run.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values — never commit .env
python app.py
```

## Required credentials (you'll need to supply these — not included)

| Variable | Where to get it |
|---|---|
| `GRANOLA_API_KEY` | Granola workspace admin → Settings → API access (Business/Enterprise plan required) — optional, leave blank on the free plan |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `JIRA_SITE` | e.g. `peech-team.atlassian.net` |
| `JIRA_EMAIL` / `JIRA_API_TOKEN` | id.atlassian.com/manage-profile/security/api-tokens |
| `SLACK_BOT_TOKEN` | Slack app with `chat:write`, `im:write` scopes |
| `TRIGGER_SHARED_SECRET` | any random string, must match the Jira Automation header |
| `TEMPO_API_TOKEN` | optional — Jira → Apps → Tempo → Settings → API Integration → New Token |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | optional — see "Setting up Google Calendar" below |

## Setting up Google Calendar (optional, more involved)

1. Go to **console.cloud.google.com**, create a new project (any name).
2. In the left menu: **APIs & Services → Library**, search "Google Calendar API", click **Enable**.
3. **APIs & Services → OAuth consent screen** — choose **External**, fill in an app name and your email, add your own email as a **test user**. Save.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - Application type: **TVs and Limited Input devices** (this enables the simpler device-code flow used below).
5. Copy the **Client ID** and **Client Secret** shown.
6. On your own computer (not Render), run:
   ```
   pip install requests
   python get_google_token.py
   ```
7. Paste in your Client ID and Secret when asked. It'll print a URL and a short code — open the URL in any browser, enter the code, approve access.
8. The script prints three values — copy all three into Render's environment variables: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`.

If any of the three Google variables are left blank, the app simply skips Calendar and continues with whatever other evidence is available — it never fails the run.

## Approval model (updated)

Nothing writes to Jira automatically anymore, regardless of confidence
score. Every claim that resolves to a real ticket becomes a **pending
draft** with Approve / Skip buttons in the Slack review message. Clicking
Approve sends a request to `/slack/interactions`, which is what actually
performs the Jira write — clicking Skip just marks it done, no write
happens. Claims that can't be matched to a ticket at all are still shown
as informational text (no button — there's nothing to write to yet).

## Setting up Slack interactivity (new, required for the approval flow)

1. Go to **api.slack.com/apps**, open your existing app (the one you made earlier for `SLACK_BOT_TOKEN`).
2. Left sidebar → **Basic Information** → scroll to **App Credentials** → copy the **Signing Secret**.
3. Add it to Render as `SLACK_SIGNING_SECRET`.
4. Left sidebar → **Interactivity & Shortcuts** → toggle **On**.
5. **Request URL**: `https://work-intelligence-mvp.onrender.com/slack/interactions`
6. Click **Save Changes**.

Until both of these are done, button clicks will fail silently (the
signature check rejects them) — this is intentional, not a bug: it means a
stranger can't forge a request that writes to your Jira.

## Setting up Tempo (optional, simpler)

1. In Jira, go to **Apps → Tempo → Settings → API Integration**.
2. Click **New Token**, give it a name, copy it.
3. Add it to Render as `TEMPO_API_TOKEN`.

Note: the "planned hours" comparison depends on the `/plans` endpoint being available on your Tempo plan — if it's not, the app just shows logged hours without a planned comparison, rather than failing.

## What's stubbed / Phase 2

- Consultant list is a static array in `config.py`, not derived from Jira/Tempo yet.
- Ticket search is a simple Jira JQL text search — no team-specific tuning.
- Missing-hours and leave logic are placeholders (`pipeline.py`) — needs
  Tempo integration per Phase 2 of the roadmap.
- No retry/backoff queue yet — failures are logged and the run continues for
  other consultants (per-consultant isolation, per the failure-handling table
  in the architecture doc).
