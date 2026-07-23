import os
from dotenv import load_dotenv

load_dotenv()

# Optional: only required if you're on a Granola Business/Enterprise plan
# and want the live API fetch. On the free plan, leave this blank in Render
# and use "manual_evidence" in the /trigger request body instead — see
# app.py and README.md.
GRANOLA_API_KEY = os.environ.get("GRANOLA_API_KEY", "")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
JIRA_SITE = os.environ["JIRA_SITE"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
# Different from SLACK_BOT_TOKEN — used to verify that a request claiming to
# be a Slack button click genuinely came from Slack. Get it from your Slack
# app's "Basic Information" page, under "Signing Secret".
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
TRIGGER_SHARED_SECRET = os.environ["TRIGGER_SHARED_SECRET"]
AUTO_WRITE_THRESHOLD = int(os.environ.get("AUTO_WRITE_THRESHOLD", "85"))

# Optional: Tempo (planned vs logged hours). Leave blank to skip.
TEMPO_API_TOKEN = os.environ.get("TEMPO_API_TOKEN", "")

# Optional: Google Calendar. Leave all three blank to skip.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# MVP: static consultant list. Phase 2 should derive this from Jira/Tempo
# team membership instead of hardcoding it here.
CONSULTANTS = [
    {
        "name": "Ananya Singh",
        "email": "a.singh@peech.tech",
        "slack_user_id": "U0B9XDF7JAF",
        "jira_account_id": "712020:4fe1df12-0359-4e52-b070-183f631de404",
    },
]

DEFAULT_JIRA_PROJECT = "AISD2026"
