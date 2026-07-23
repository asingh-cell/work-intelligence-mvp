"""SQLite-backed idempotency store + pending-draft store.

Idempotency: (consultant_email, run_date) -> already run today?
Drafts: draft_id -> {ticket_key, claim_text, hours, ...} for the Slack
approve/skip flow — Slack's button-click payload only carries a short
action value, not the full claim data, so we look the rest up here when
the button is clicked.
"""
import json
import sqlite3
from contextlib import contextmanager

DB_PATH = "runs.db"


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (consultant_email TEXT, run_date TEXT, "
            "PRIMARY KEY (consultant_email, run_date))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS drafts (draft_id TEXT PRIMARY KEY, data TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS day_meta (consultant_email TEXT, run_date TEXT, field TEXT, "
            "value TEXT, PRIMARY KEY (consultant_email, run_date, field))"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def already_ran(consultant_email: str, run_date: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE consultant_email = ? AND run_date = ?",
            (consultant_email, run_date),
        ).fetchone()
        return row is not None


def mark_ran(consultant_email: str, run_date: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO runs (consultant_email, run_date) VALUES (?, ?)",
            (consultant_email, run_date),
        )


def save_draft(draft_id: str, data: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drafts (draft_id, data, status) VALUES (?, ?, 'pending')",
            (draft_id, json.dumps(data)),
        )


def get_draft(draft_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT data, status FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["_status"] = row[1]
        return data


def set_draft_status(draft_id: str, status: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE drafts SET status = ? WHERE draft_id = ?", (status, draft_id))


def save_day_meta(consultant_email: str, run_date: str, field: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO day_meta (consultant_email, run_date, field, value) VALUES (?, ?, ?, ?)",
            (consultant_email, run_date, field, value),
        )


def get_day_meta(consultant_email: str, run_date: str, field: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM day_meta WHERE consultant_email = ? AND run_date = ? AND field = ?",
            (consultant_email, run_date, field),
        ).fetchone()
        return row[0] if row else None
