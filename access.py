"""
Access control + redeemable code system for UAV.

Hierarchy (highest → lowest):
    OWNER (100)  — hardcoded via env OWNER_ID, supreme authority
    SUDO  (50)   — appointed by owner, can manage Premium + generate codes
    PREMIUM (10) — full bot access, no management rights
    GUEST (0)    — default; can only /start /help /redeem /myaccess /menu /ping

Two SQLite tables (in same DB as user_settings):
    bot_users     — role + status per user
    redeem_codes  — code metadata + usage tracking

Public API used by bot.py:
    init()                                 — create tables + indexes
    get_role(uid, owner_id)                — string role, with auto-expiry handling
    is_owner(uid, owner_id), is_banned(uid)
    role_level(role)                       — int 0-100
    touch_user(uid, username, first_name)  — register / update last_active
    set_role(uid, role, added_by, expires_at=None, code=None)
    ban_user(uid), unban_user(uid)
    list_role(role)                        — list of dict rows
    get_user(uid)                          — single dict
    expire_check_all()                     — sweep expired premium → guest
    create_code(created_by, duration_days, max_uses, notes)
    redeem_code(code, uid, owner_id)       — returns (ok, role|None, expires_at|None, err|None)
    list_codes(filter_by="all"|"active"|"used"|"expired")
    revoke_code(code, by_uid)
    get_code(code)
    audit(uid, action, details="")         — appends to bot's audit log

Constants:
    ROLE_OWNER, ROLE_SUDO, ROLE_PREMIUM, ROLE_GUEST  — string labels
    LEVELS                                           — dict {role: int}
    GUEST_ALLOWED_CMDS                               — set
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from db import DB_PATH, _get_conn  # reuse same SQLite file

logger = logging.getLogger(__name__)

# ── Role constants ──────────────────────────────────────────────────────────
ROLE_OWNER   = "OWNER"
ROLE_SUDO    = "SUDO"
ROLE_PREMIUM = "PREMIUM"
ROLE_GUEST   = "GUEST"

LEVELS = {
    ROLE_OWNER:   100,
    ROLE_SUDO:    50,
    ROLE_PREMIUM: 10,
    ROLE_GUEST:   0,
}

# Commands a GUEST can use (everything else is gated)
GUEST_ALLOWED_CMDS = frozenset({
    "start", "help", "redeem", "myaccess",
    "menu", "ping", "guide", "claimowner",
})

# Code format: UAV-XXXX-XXXX-XXXX (12 random chars in 3 groups)
# Charset excludes I, O, 0, 1 to avoid visual confusion
_CODE_PREFIX  = "UAV"
_CODE_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_GROUPS  = 3
_CODE_GROUP_LEN = 4

# Same audit file as bot.py uses
_AUDIT_FILE = "/tmp/uav-audit.log"
_audit_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────
def role_level(role: str) -> int:
    return LEVELS.get((role or "").upper(), 0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _now_ts() -> int:
    return int(time.time())


def fmt_expires(expires_ts: Optional[int]) -> str:
    if not expires_ts:
        return "permanent"
    return datetime.fromtimestamp(expires_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_remaining(expires_ts: Optional[int]) -> str:
    if not expires_ts:
        return "permanent"
    remain = expires_ts - _now_ts()
    if remain <= 0:
        return "expired"
    days = remain // 86400
    hours = (remain % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    mins = (remain % 3600) // 60
    return f"{hours}h {mins}m"


def audit(uid: int, action: str, details: str = ""):
    """Append a sensitive action to the bot's audit log."""
    try:
        line = f"{_now_iso()} | uid={uid} | {action}"
        if details:
            line += f"  | {details.replace(chr(10), ' ')[:200]}"
        with _audit_lock:
            with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as ex:
        logger.debug(f"access.audit write failed: {ex}")


# ── Schema init ─────────────────────────────────────────────────────────────
def init():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id          INTEGER PRIMARY KEY,
                username         TEXT    DEFAULT '',
                first_name       TEXT    DEFAULT '',
                role             TEXT    DEFAULT 'GUEST',
                added_by         INTEGER DEFAULT 0,
                added_at         INTEGER DEFAULT 0,
                expires_at       INTEGER,
                is_banned        INTEGER DEFAULT 0,
                redeem_code_used TEXT    DEFAULT '',
                last_active      INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code           TEXT    PRIMARY KEY,
                created_by     INTEGER NOT NULL,
                created_at     INTEGER NOT NULL,
                duration_days  INTEGER NOT NULL,
                max_uses       INTEGER NOT NULL DEFAULT 1,
                current_uses   INTEGER NOT NULL DEFAULT 0,
                is_active      INTEGER NOT NULL DEFAULT 1,
                expires_at     INTEGER,
                redeemed_by    TEXT    DEFAULT '[]',
                notes          TEXT    DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_users_role ON bot_users(role)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codes_active ON redeem_codes(is_active)")
        conn.commit()
    logger.info("access: schema initialized")


# ── User row helpers ────────────────────────────────────────────────────────
def _row_to_dict(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["is_banned"] = bool(d.get("is_banned"))
    return d


def get_user(uid: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bot_users WHERE user_id = ?", (uid,)
        ).fetchone()
    return _row_to_dict(row)


def touch_user(uid: int, username: str = "", first_name: str = ""):
    """Insert or update last_active + identity fields. Default role=GUEST."""
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM bot_users WHERE user_id = ?", (uid,)
        ).fetchone()
        ts = _now_ts()
        if existing:
            conn.execute(
                "UPDATE bot_users SET username=?, first_name=?, last_active=? WHERE user_id=?",
                (username or "", first_name or "", ts, uid),
            )
        else:
            conn.execute(
                "INSERT INTO bot_users (user_id, username, first_name, role, added_at, last_active) "
                "VALUES (?, ?, ?, 'GUEST', ?, ?)",
                (uid, username or "", first_name or "", ts, ts),
            )
        conn.commit()


def is_banned(uid: int) -> bool:
    row = get_user(uid)
    return bool(row and row.get("is_banned"))


def _is_owner_in_db(uid: int) -> bool:
    """Check if user is stored as OWNER in DB (bootstrap fallback when env unset)."""
    row = get_user(uid)
    return bool(row and (row.get("role") or "").upper() == ROLE_OWNER)


def is_owner(uid: int, owner_id: Optional[int]) -> bool:
    """Owner if env OWNER_ID matches OR user has OWNER role stored in DB."""
    if owner_id is not None and uid == owner_id:
        return True
    return _is_owner_in_db(uid)


def count_owners_db() -> int:
    """Count users with role=OWNER stored in DB."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM bot_users WHERE role = 'OWNER'"
        ).fetchone()
    return int(row[0]) if row else 0


def first_owner_db() -> Optional[dict]:
    """Return first OWNER row from DB (for display fallback)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bot_users WHERE role = 'OWNER' ORDER BY added_at ASC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row)


def bootstrap_owner(uid: int, username: str = "", first_name: str = "") -> bool:
    """Promote user to OWNER if no OWNER exists yet (env nor DB). Returns True if promoted."""
    if count_owners_db() > 0:
        return False
    touch_user(uid, username, first_name)
    with _get_conn() as conn:
        conn.execute(
            "UPDATE bot_users SET role='OWNER', added_by=?, added_at=?, "
            "expires_at=NULL, is_banned=0 WHERE user_id=?",
            (uid, _now_ts(), uid),
        )
        conn.commit()
    audit(uid, "bootstrap_owner", f"uid={uid} username={username}")
    logger.info(f"access: bootstrap OWNER -> uid={uid} (@{username})")
    return True


def get_owner_display(env_owner_id: Optional[int] = None,
                     env_owner_username: str = "") -> str:
    """Return a user-friendly owner mention. Prefers @username, falls back to ID."""
    # 1. Explicit env username wins
    if env_owner_username:
        u = env_owner_username.lstrip("@").strip()
        if u:
            return f"@{u}"
    # 2. DB OWNER's @username
    row = first_owner_db()
    if row and row.get("username"):
        return f"@{row['username']}"
    # 3. env OWNER_ID
    if env_owner_id is not None:
        return f"`{env_owner_id}`"
    # 4. DB OWNER's UID
    if row and row.get("user_id"):
        return f"`{row['user_id']}`"
    return "_owner_"


def get_role(uid: int, owner_id: Optional[int]) -> str:
    """Return effective role. Auto-downgrades expired PREMIUM/SUDO to GUEST."""
    if is_owner(uid, owner_id):
        return ROLE_OWNER
    row = get_user(uid)
    if not row:
        return ROLE_GUEST
    role = (row.get("role") or "GUEST").upper()
    expires = row.get("expires_at")
    if expires and _now_ts() >= expires and role in (ROLE_PREMIUM, ROLE_SUDO):
        # Expired → downgrade
        with _get_conn() as conn:
            conn.execute(
                "UPDATE bot_users SET role='GUEST', expires_at=NULL WHERE user_id=?",
                (uid,),
            )
            conn.commit()
        audit(uid, "auto_expire", f"prev_role={role}")
        logger.info(f"access: auto-expired uid={uid} from {role}")
        return ROLE_GUEST
    return role


def set_role(uid: int, role: str, added_by: int,
             expires_at: Optional[int] = None, code: str = ""):
    """Set / update a user's role. Creates row if missing."""
    role = role.upper()
    if role not in LEVELS:
        raise ValueError(f"unknown role: {role}")
    touch_user(uid)  # ensure row exists
    with _get_conn() as conn:
        conn.execute(
            "UPDATE bot_users SET role=?, added_by=?, added_at=?, "
            "expires_at=?, redeem_code_used=? WHERE user_id=?",
            (role, added_by, _now_ts(), expires_at, code or "", uid),
        )
        conn.commit()


def ban_user(uid: int, by_uid: int) -> bool:
    """Ban a user. Owner cannot be banned (caller must check first too)."""
    touch_user(uid)
    with _get_conn() as conn:
        conn.execute("UPDATE bot_users SET is_banned=1 WHERE user_id=?", (uid,))
        conn.commit()
    audit(by_uid, "ban_user", f"target={uid}")
    return True


def unban_user(uid: int, by_uid: int) -> bool:
    with _get_conn() as conn:
        conn.execute("UPDATE bot_users SET is_banned=0 WHERE user_id=?", (uid,))
        conn.commit()
    audit(by_uid, "unban_user", f"target={uid}")
    return True


def list_role(role: str) -> list[dict]:
    role = role.upper()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_users WHERE role = ? ORDER BY added_at DESC",
            (role,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_banned() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_users WHERE is_banned = 1 ORDER BY last_active DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def expire_check_all() -> int:
    """Sweep all expired PREMIUM/SUDO and downgrade. Returns count downgraded."""
    now = _now_ts()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, role FROM bot_users "
            "WHERE expires_at IS NOT NULL AND expires_at <= ? "
            "AND role IN ('PREMIUM','SUDO')",
            (now,),
        ).fetchall()
        n = len(rows)
        if n:
            conn.execute(
                "UPDATE bot_users SET role='GUEST', expires_at=NULL "
                "WHERE expires_at IS NOT NULL AND expires_at <= ? "
                "AND role IN ('PREMIUM','SUDO')",
                (now,),
            )
            conn.commit()
            for r in rows:
                audit(r["user_id"], "auto_expire", f"prev_role={r['role']}")
    if n:
        logger.info(f"access: expire sweep downgraded {n} users")
    return n


# ── Code generation / redemption ────────────────────────────────────────────
def _gen_code_string() -> str:
    parts = [
        "".join(secrets.choice(_CODE_CHARSET) for _ in range(_CODE_GROUP_LEN))
        for _ in range(_CODE_GROUPS)
    ]
    return _CODE_PREFIX + "-" + "-".join(parts)


def normalize_code(s: str) -> str:
    """Uppercase + strip whitespace. Codes stored uppercase."""
    return (s or "").strip().upper()


def generate_unique_code(max_tries: int = 10) -> str:
    with _get_conn() as conn:
        for _ in range(max_tries):
            c = _gen_code_string()
            row = conn.execute(
                "SELECT 1 FROM redeem_codes WHERE code = ?", (c,)
            ).fetchone()
            if not row:
                return c
    # Astronomically unlikely with 32^12 search space
    raise RuntimeError("code generation: collision after retries")


def create_code(created_by: int, duration_days: int,
                max_uses: int = 1, notes: str = "") -> dict:
    """Insert a new redeem code. Returns the row dict."""
    if duration_days < 0:
        raise ValueError("duration_days must be >= 0 (0 = permanent)")
    if max_uses < 1:
        raise ValueError("max_uses must be >= 1")
    code = generate_unique_code()
    now = _now_ts()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO redeem_codes "
            "(code, created_by, created_at, duration_days, max_uses, "
            " current_uses, is_active, expires_at, redeemed_by, notes) "
            "VALUES (?, ?, ?, ?, ?, 0, 1, NULL, '[]', ?)",
            (code, created_by, now, duration_days, max_uses, notes[:200] if notes else ""),
        )
        conn.commit()
    audit(created_by, "code_create",
          f"code={code} days={duration_days} max_uses={max_uses} notes={notes[:50]}")
    return get_code(code)


def get_code(code: str) -> Optional[dict]:
    code = normalize_code(code)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM redeem_codes WHERE code = ?", (code,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["redeemed_by_list"] = json.loads(d.get("redeemed_by") or "[]")
    except Exception:
        d["redeemed_by_list"] = []
    d["is_active"] = bool(d["is_active"])
    return d


def list_codes(filter_by: str = "all") -> list[dict]:
    filter_by = (filter_by or "all").lower()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM redeem_codes ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_active"] = bool(d["is_active"])
        try:
            d["redeemed_by_list"] = json.loads(d.get("redeemed_by") or "[]")
        except Exception:
            d["redeemed_by_list"] = []
        if filter_by == "active" and not (d["is_active"] and d["current_uses"] < d["max_uses"]):
            continue
        if filter_by == "used" and d["current_uses"] == 0:
            continue
        if filter_by == "expired" and d["is_active"] and d["current_uses"] < d["max_uses"]:
            continue
        out.append(d)
    return out


def revoke_code(code: str, by_uid: int) -> bool:
    code = normalize_code(code)
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE redeem_codes SET is_active=0 WHERE code=? AND is_active=1",
            (code,),
        )
        conn.commit()
        ok = cur.rowcount > 0
    if ok:
        audit(by_uid, "code_revoke", f"code={code}")
    return ok


def redeem_code(code: str, uid: int, owner_id: Optional[int]) -> tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """Try to redeem. Returns (ok, role_granted, expires_at, error_message)."""
    code = normalize_code(code)
    if not code or not code.startswith(_CODE_PREFIX):
        return False, None, None, "invalid"

    if is_banned(uid):
        return False, None, None, "banned"

    role_now = get_role(uid, owner_id)
    if role_now in (ROLE_OWNER, ROLE_SUDO):
        return False, None, None, "already_premium"

    row = get_code(code)
    if not row:
        return False, None, None, "invalid"
    if not row["is_active"]:
        return False, None, None, "expired"
    if row["current_uses"] >= row["max_uses"]:
        return False, None, None, "exhausted"
    if uid in row["redeemed_by_list"]:
        return False, None, None, "already_redeemed"

    duration_days = row["duration_days"]
    new_expiry: Optional[int] = None
    if duration_days > 0:
        # If user already PREMIUM with time left → extend, else fresh from now
        existing = get_user(uid)
        base = _now_ts()
        if existing and existing.get("expires_at") and existing.get("role") == ROLE_PREMIUM \
                and existing["expires_at"] > base:
            base = existing["expires_at"]
        new_expiry = base + duration_days * 86400

    set_role(uid, ROLE_PREMIUM, added_by=row["created_by"],
             expires_at=new_expiry, code=code)

    # Update code usage atomically
    with _get_conn() as conn:
        cur_row = conn.execute(
            "SELECT current_uses, max_uses, redeemed_by FROM redeem_codes WHERE code=?",
            (code,),
        ).fetchone()
        try:
            redeemed = json.loads(cur_row["redeemed_by"] or "[]")
        except Exception:
            redeemed = []
        if uid not in redeemed:
            redeemed.append(uid)
        new_uses = cur_row["current_uses"] + 1
        new_active = 1 if new_uses < cur_row["max_uses"] else 0
        conn.execute(
            "UPDATE redeem_codes SET current_uses=?, redeemed_by=?, is_active=? WHERE code=?",
            (new_uses, json.dumps(redeemed), new_active, code),
        )
        conn.commit()

    audit(uid, "code_redeem", f"code={code} expires={fmt_expires(new_expiry)}")
    return True, ROLE_PREMIUM, new_expiry, None


# ── Stats ───────────────────────────────────────────────────────────────────
def stats() -> dict:
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM bot_users").fetchone()["c"]
        by_role = {}
        for r in conn.execute(
            "SELECT role, COUNT(*) c FROM bot_users GROUP BY role"
        ).fetchall():
            by_role[r["role"]] = r["c"]
        banned = conn.execute(
            "SELECT COUNT(*) c FROM bot_users WHERE is_banned=1"
        ).fetchone()["c"]
        codes_total = conn.execute("SELECT COUNT(*) c FROM redeem_codes").fetchone()["c"]
        codes_active = conn.execute(
            "SELECT COUNT(*) c FROM redeem_codes WHERE is_active=1 AND current_uses<max_uses"
        ).fetchone()["c"]
        codes_redeemed = conn.execute(
            "SELECT COALESCE(SUM(current_uses),0) c FROM redeem_codes"
        ).fetchone()["c"]
    return {
        "users_total": total,
        "by_role": by_role,
        "banned": banned,
        "codes_total": codes_total,
        "codes_active": codes_active,
        "codes_redeemed": codes_redeemed,
    }
