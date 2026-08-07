# Work Intelligence Assistant — MVP

Turns Slack chatter into a Jira timesheet, with a human in the loop.

**Flow:** Postman `POST /trigger` → pulls that person's Slack messages for a
date → Claude drafts timesheet line items → interactive Slack DM
(Approve / Edit / Skip per item, plus leave and manual-logging dropdowns) →
you click **Submit to Jira** → approved items get written as Jira worklogs.

## What's in here

- `app.py` — the whole service (Flask). One file on purpose — it's a
  one-person prototype, not a platform yet.
- `test_app.py` — self-check for the pure logic (leave math, confidence
  colors, JSON parsing, progress bar). Run with `python test_app.py`. It
  doesn't touch Slack/Jira/Claude, so it works with no real credentials.
- `requirements.txt`, `Procfile` — deploy on Render as-is.
- `.env.example` — every env var the app reads.

## Setup — do this in order

**1. Deploy first, get the URL, then configure Slack.** Slack needs a real
HTTPS URL for interactivity, so:
   - Push this repo to GitHub, connect it on Render as a new Web Service
     (Python 3, build `pip install -r requirements.txt`, start command comes
     from `Procfile` automatically).
   - Set every var from `.env.example` in Render's Environment tab.
   - Deploy, then hit `https://<your-render-url>/health` in a browser — you
     should see `{"status": "ok"}`. If not, stop here and check the Render
     logs before touching Slack.

**2. Create the Slack app from the manifest.**
   - slack.com/apps → Create New App → **From an app manifest** → paste
     `app_manifest.yaml`, but first replace `REPLACE-ME.onrender.com` with
     your actual Render URL.
   - Install the app to your workspace.
   - Copy **Bot User OAuth Token** (`xoxb-...`) → `SLACK_BOT_TOKEN`.
   - Copy **Signing Secret** (Basic Information page) → `SLACK_SIGNING_SECRET`.
   - Redeploy Render with those two values filled in for real.

**3. Invite the bot to every evidence channel.** The manifest grants the
   scopes, but the bot still can't read a channel until it's a member:
   `/invite @Work Intelligence Assistant` in each channel you listed in
   `SLACK_EVIDENCE_CHANNELS`. Skipping this is the #1 cause of "it came back
   empty."

**4. Get the Slack user ID you'll test with.** Their profile → the `•••`
   menu → **Copy member ID** (looks like `U0123ABCD`). This is what you send
   in the `/trigger` call, not their name or email.

**5. Jira.** An API token (id.atlassian.com → Security → API tokens) for a
   user with permission to add worklogs on the test project.
   `JIRA_SITE` is your Atlassian domain, e.g. `https://yourco.atlassian.net`.
   Tempo reads worklogs straight from Jira, so nothing Tempo-specific is
   needed.

**6. `ALLOWED_TICKET_KEYS`.** Must be real keys that exist in your Jira
   project — pick 2-3 you'll actually reference in the test Slack messages.

**6b. `EVIDENCE_MARKER` (default `[log]`).** Only messages containing this
   marker get pulled into the pipeline — everything else in the channel is
   invisible to this app. This keeps sensitive channel chatter out of
   Claude entirely: nobody has to trust the model to ignore the wrong
   things, it never sees them. To log something, just include the marker
   anywhere in the message, e.g. *"Fixed the auth bug today [log], took
   about 2 hours."* Change the env var if you'd rather use a different tag
   — but avoid anything starting with `@` or `#`. Those are Slack's own
   trigger characters for mentions and channels, and if a real app in your
   workspace happens to share that name (e.g. an installed Jira or Fireflies
   integration), Slack's autocomplete can turn what looks like plain text
   into a resolved mention token the moment someone clicks the suggestion —
   which would silently break the filter for that message.

**7. Anthropic API key** for `ANTHROPIC_API_KEY`. Default model is
   `claude-sonnet-5`.

**8. Seed real evidence before you demo.** This calls Claude on whatever
   Slack text actually exists — if the channel's empty for that date, the
   draft comes back empty and it'll look broken. Post 2-3 realistic messages
   as the test user in an evidence channel first, e.g. *"Spent the morning
   on AISD2026-49, fixed the review-flow bug and verified all three test
   cases."* Then trigger for that date.

## Trying it

Once deployed, send this from Postman:

```
POST https://<your-render-url>/trigger
Header: X-Trigger-Secret: <TRIGGER_SHARED_SECRET>
Body (JSON):
{
  "email": "person@yourcompany.com",
  "date": "2026-08-04"
}
```

(You can pass `"slack_user_id": "U0123..."` instead of `email` if you already
have it, but email is easier — you already know everyone's, no digging
through Slack profile menus per person.)

You should get a Slack DM from **Work Intelligence Assistant** within a few
seconds. Click Approve/Edit/Skip on each item, set leave status if relevant,
use "Log the rest" for anything Slack evidence won't catch, then hit
**Submit to Jira**.

## Known limitations (deliberate, for a first pass)

- **State is in-memory.** If the Render process restarts between posting the
  DM and you hitting Submit, that draft is gone — you'd re-trigger. Fine for
  testing solo; swap `PENDING` for a SQLite table before this goes to more
  than one person or runs unattended.
- **No scheduler yet.** You're triggering by hand via Postman, matching
  what you described as phase 1. Once this works, point a Jira Automation
  rule (or a Render Cron Job) at `/trigger` on a schedule — no code changes
  needed on this end, just something calling the same endpoint with the
  same header.
- **Timezones are fixed UTC offsets** (IST/EST/CST), no DST. Matches "India
  team first" — revisit with real IANA tz names before expanding to US.
- **Ticket matching is a best-effort Claude read of Slack text**, not a
  guarantee. Anything outside `ALLOWED_TICKET_KEYS` gets a warning so you
  catch it before approving, but always skim before you hit Submit.
- **Submit is safe to click more than once.** Each item tracks the Jira
  worklog it created, so adding more entries and hitting Submit again
  updates existing worklogs in place instead of duplicating them. One gap:
  if you **Undo** an item *after* it's already been logged to Jira, the
  worklog stays in Jira — Undo only affects this draft, it doesn't delete
  anything already written. Remove it manually in Jira if that happens.
- **The ticket dropdown** (in Edit and the manual-entry modal) is built
  directly from `ALLOWED_TICKET_KEYS`, with live summaries pulled from
  Jira. Add a ticket key to that env var in Render and it shows up in the
  dropdown next time you trigger — no code change needed. If Jira can't be
  reached for a summary, the ticket still shows up with just its key.
- I couldn't run this end-to-end against live Slack/Jira from where I built
  it (no network access to those APIs in this sandbox) — the self-check
  covers the pure logic, but the Slack/Jira/Claude calls themselves need
  your first real test run on Render.
