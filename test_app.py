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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
