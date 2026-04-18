import sqlite3
import json
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = "bot_data.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_ALLOWED_COLUMNS = frozenset({
    "url", "click_text", "click_text2", "primary_selector", "secondary_selector",
    "delay", "page_load_wait", "loops", "timeout", "proxies",
    "identity_mode", "circuit_mode", "tor_mode",
    "break_interval", "break_duration",
})


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id     INTEGER PRIMARY KEY,
                url         TEXT    DEFAULT '',
                click_text  TEXT    DEFAULT '',
                click_text2 TEXT    DEFAULT '',
                primary_selector   TEXT DEFAULT '',
                secondary_selector TEXT DEFAULT '',
                delay          REAL    DEFAULT 5.0,
                page_load_wait REAL    DEFAULT 5.0,
                loops          INTEGER DEFAULT 0,
                timeout        INTEGER DEFAULT 30,
                proxies        TEXT    DEFAULT '[]',
                identity_mode  INTEGER DEFAULT 1,
                circuit_mode   INTEGER DEFAULT 0,
                tor_mode       INTEGER DEFAULT 0,
                break_interval INTEGER DEFAULT 50,
                break_duration INTEGER DEFAULT 60
            )
        """)
        # Add any columns that might be missing in older databases.
        # ALTER TABLE silently fails if the column already exists — that's fine.
        # IMPORTANT: every column added to CREATE TABLE above must also appear here.
        _migrations = [
            ("click_text2",         "TEXT    DEFAULT ''"),
            ("primary_selector",    "TEXT    DEFAULT ''"),
            ("secondary_selector",  "TEXT    DEFAULT ''"),
            ("page_load_wait",      "REAL    DEFAULT 5.0"),
            ("identity_mode",       "INTEGER DEFAULT 1"),
            ("circuit_mode",        "INTEGER DEFAULT 0"),
            ("tor_mode",            "INTEGER DEFAULT 0"),
            ("break_interval",      "INTEGER DEFAULT 50"),
            ("break_duration",      "INTEGER DEFAULT 60"),
        ]
        for col, typedef in _migrations:
            try:
                conn.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    logger.info("Database initialized.")


def load_user(user_id: int) -> dict:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            data = dict(row)
            data["proxies"] = json.loads(data.get("proxies") or "[]")
            return data
        return {}


def save_user(user_id: int, **fields):
    # Whitelist column names to prevent SQL injection via column name injection
    unknown = set(fields) - _ALLOWED_COLUMNS
    if unknown:
        raise ValueError(f"save_user: unknown column(s): {unknown}")

    if "proxies" in fields and isinstance(fields["proxies"], list):
        fields["proxies"] = json.dumps(fields["proxies"])

    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()

        if existing:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            values = list(fields.values()) + [user_id]
            conn.execute(
                f"UPDATE user_settings SET {set_clause} WHERE user_id = ?",
                values,
            )
        else:
            fields["user_id"] = user_id
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" * len(fields))
            conn.execute(
                f"INSERT INTO user_settings ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
        conn.commit()


def add_user(user_id: int):
    """Register user in DB if not already present (no-op if already exists)."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
            (user_id,),
        )
        conn.commit()


def get_all_users() -> list[int]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM user_settings").fetchall()
        return [r["user_id"] for r in rows]
