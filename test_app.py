"""
Runnable self-check for the pure logic in app.py — no Slack/Jira/Claude calls.
Run: python test_app.py
"""
import os

# app.py reads these at import time — set dummies so import doesn't blow up.
for k in ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "TRIGGER_SHARED_SECRET",
          "JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN", "ANTHROPIC_API_KEY"]:
    os.environ.setdefault(k, "test")
os.environ.setdefault("JIRA_SITE", "https://example.atlassian.net")

import app  # noqa: E402


def test_required_hours_for_leave():
    assert app.required_hours_for_leave("full") == 0.0
    assert app.required_hours_for_leave("half") == 4.0
    assert app.required_hours_for_leave("none") == 8.0
    assert app.required_hours_for_leave("wfh") == 8.0
    assert app.required_hours_for_leave("other") == 8.0
    assert app.required_hours_for_leave("garbage") == 8.0  # unknown -> full day, safest default


def test_confidence_label():
    app.AUTO_WRITE_THRESHOLD = 70
    assert app.confidence_label(90) == "High"
    assert app.confidence_label(50) == "Medium"
    assert app.confidence_label(10) == "Low"


def test_parse_claude_json_handles_fences_and_garbage():
    assert app.parse_claude_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert app.parse_claude_json("[]") == []
    assert app.parse_claude_json("not json at all") == []


def test_logged_hours_only_counts_resolved_items():
    draft = {
        "items": {
            "a": {"status": "approved", "hours": 2.0},
            "b": {"status": "pending", "hours": 5.0},
            "c": {"status": "skipped", "hours": 1.0},
            "d": {"status": "edited", "hours": 1.5},
            "e": {"status": "approved", "hours": 1.0, "source": "manual"},
        },
    }
    assert app.logged_hours(draft) == 4.5  # 2.0 + 1.5 + 1.0, pending/skipped excluded


def test_progress_bar_bounds():
    assert app.progress_bar(0, 8) == "░" * 10
    assert app.progress_bar(8, 8) == "█" * 10
    assert app.progress_bar(4, 8) == "█" * 5 + "░" * 5
    assert app.progress_bar(100, 8) == "█" * 10  # never overflows past full bar


def test_add_hours_to_time():
    assert app.add_hours_to_time("09:00", 1.5) == "10:30"
    assert app.add_hours_to_time("09:00", 0) == "09:00"
    assert app.add_hours_to_time("23:00", 5) == "23:59"  # clamps, doesn't roll to next day
    assert app.add_hours_to_time("garbage", 2) == "11:00"  # falls back to 09:00 start


def test_time_options_and_rounding():
    options = dict(app.TIME_OPTIONS)
    assert options["00:00"] == "12:00 AM"
    assert options["09:00"] == "9:00 AM"
    assert options["12:00"] == "12:00 PM"
    assert options["23:45"] == "11:45 PM"
    assert len(app.TIME_OPTIONS) == 96  # 24h in 15-min steps, fits Slack's 100-option cap
    assert app.nearest_time_slot("13:07") == "13:00"
    assert app.nearest_time_slot("09:08") == "09:15"
    assert app.nearest_time_slot("garbage") == "09:00"


def test_ist_day_window_includes_early_morning_messages():
    from datetime import datetime
    # The exact failure case: a message posted at 1:10 AM IST should count as
    # "today" (IST), even though that instant falls in the *previous* UTC day.
    msg_ts = datetime(2026, 8, 6, 1, 10, tzinfo=app.IST).timestamp()
    day = datetime.strptime("2026-08-06", "%Y-%m-%d").replace(tzinfo=app.IST)
    oldest, latest = day.timestamp(), (day + app.timedelta(days=1)).timestamp()
    assert oldest <= msg_ts < latest
    # And a message from the day before should NOT leak into today's window
    prev_day_msg_ts = datetime(2026, 8, 5, 23, 0, tzinfo=app.IST).timestamp()
    assert not (oldest <= prev_day_msg_ts < latest)


def test_evidence_marker_filters_untagged_messages():
    marker = app.EVIDENCE_MARKER
    tagged = f"Fixed the review-flow bug {marker} verified all three cases"
    untagged = "just chatting about lunch plans"
    assert marker in tagged.lower()
    assert marker not in untagged.lower()
    assert not marker.startswith(("@", "#"))  # must never risk Slack's own mention/channel autocomplete


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
