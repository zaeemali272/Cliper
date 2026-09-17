"""SQLite database management for Cliper SaaS."""
from __future__ import annotations

import os
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

from .config import DATA_DIR

DB_PATH = DATA_DIR / "cliper.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db() -> None:
    """Initialize database tables if they do not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                trial_ends_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );
        """)
        conn.commit()


def user_to_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    # calculate active pro / trial state
    now = datetime.now(timezone.utc)
    trial_ends = datetime.fromisoformat(d["trial_ends_at"])
    d["is_trial_active"] = now < trial_ends
    d["is_pro"] = d["plan"] == "pro" or d["is_trial_active"]
    if d["is_trial_active"] and d["plan"] != "pro":
        days_left = max(1, (trial_ends - now).days + 1)
        d["status_label"] = f"Pro Trial ({days_left}d left)"
    elif d["plan"] == "pro":
        d["status_label"] = "Pro Subscriber"
    else:
        d["status_label"] = "Free Plan"
    return d


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),)).fetchone()
        return user_to_dict(row)


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return user_to_dict(row)


def create_user(email: str, password_hash: str, salt: str, trial_days: int = 7) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    trial_ends = now + timedelta(days=trial_days)
    now_str = now.isoformat()
    trial_ends_str = trial_ends.isoformat()

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (email, password_hash, salt, plan, trial_ends_at, created_at)
            VALUES (?, ?, ?, 'free', ?, ?)
            """,
            (email.strip().lower(), password_hash, salt, trial_ends_str, now_str)
        )
        conn.commit()
        user_id = cursor.lastrowid
        return user_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())  # type: ignore


def create_session(user_id: int, expiry_days: int = 30) -> str:
    token = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at)
        )
        conn.commit()
    return token


def get_session_user(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    now_str = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT u.* FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now_str)
        ).fetchone()
        return user_to_dict(row)


def delete_session(token: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def check_daily_quota(user_id: int, action_type: str) -> Dict[str, Any]:
    """Check daily limit for free tier (1 video analyze/day, 1 clip generate/day). Pro & active trial get unlimited."""
    user = get_user_by_id(user_id)
    if not user:
        return {"allowed": False, "reason": "User not found", "remaining": 0, "limit": 0}

    if user["is_pro"]:
        return {"allowed": True, "remaining": 999999, "limit": 999999}

    # Free tier limit: 1 per 24-hour window
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    with get_connection() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) FROM usage_logs
            WHERE user_id = ? AND action_type = ? AND created_at >= ?
            """,
            (user_id, action_type, since)
        ).fetchone()[0]

    limit = 1
    remaining = max(0, limit - count)
    allowed = count < limit

    reason = ""
    if not allowed:
        reason = f"Free tier limit reached (1 {action_type} per 24 hours). Upgrade to Pro for unlimited processing!"

    return {
        "allowed": allowed,
        "remaining": remaining,
        "limit": limit,
        "reason": reason
    }


def record_usage(user_id: int, action_type: str) -> None:
    now_str = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO usage_logs (user_id, action_type, created_at) VALUES (?, ?, ?)",
            (user_id, action_type, now_str)
        )
        conn.commit()


def link_job_to_user(job_id: str, user_id: int, url: str, title: str = "") -> None:
    now_str = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs (job_id, user_id, url, title, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, user_id, url, title, now_str)
        )
        conn.commit()


def get_user_job_ids(user_id: int) -> List[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
        return [r["job_id"] for r in rows]


def is_job_owned_by(job_id: str, user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ? AND user_id = ?",
            (job_id, user_id)
        ).fetchone()
        return row is not None


def update_user_plan(user_id: int, plan: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
        conn.commit()
