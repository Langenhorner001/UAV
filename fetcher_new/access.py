"""
Access control + redeemable code system for TG-POST-FETCHER-BOT.

Hierarchy (highest -> lowest):
    OWNER (100)  - hardcoded via env OWNER_ID, supreme authority
    SUDO  (50)   - appointed by owner, can manage Premium + generate codes
    PREMIUM (10) - full bot access (fetch links + commands), no management rights
    GUEST (0)   - default; can only /start /help /redeem /myaccess /ping /claimowner

Two SQLite tables in fetcher_data.db:
    bot_users     - role + status per user
    redeem_codes  - code metadata + usage tracking

This module is self-contained (own sqlite connection, own audit file).
Code prefix is FCH (vs UAV bot) so codes are not cross-redeemable.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

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
    "start", "help", "h", "redeem", "myaccess",
    "ping", "p", "claimowner",
})

# Code format: FCH-XXXX-XXXX-XXXX (12 random chars in 3 groups)
# Charset excludes I, O, 0, 1 to avoid visual confusion
_CODE_PREFIX  = "FCH"
_CODE_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_GROUPS  = 3
_CODE_GROUP_LEN = 4

DB_PATH = os.environ.get("FETCHER_DB_PATH", "fetcher_data.db")
_AUDIT_FILE = "/tmp/fetcher-audit.log"
_audit_lock = threading.Lock()
_db_lock = threading.Lock()


# ── DB connection ───────────────────────────────────────────────────────────
@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── Helpers ─────────────────────────────────────────────────────────────────
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


# ── Schema init + one-time migration from sudo_users.txt ───────────────────
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
    logger.info("access: schema initialized at %s", DB_PATH)
    _migrate_sudo_txt()


def _migrate_sudo_txt():
    """One-time migration: read sudo_users.txt and seed DB SUDO rows."""
    txt = "sudo_users.txt"
    migrated_marker = "sudo_users.txt.migrated"
    if os.path.exists(migrated_marker) or not os.path.exists(txt):
        return
    try:
        seeded = 0
        with open(txt, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    uid = int(line)
                    if not get_user(uid):
                        touch_user(uid)
                    set_role(uid, ROLE_SUDO, added_by=0, expires_at=None)
                    seeded += 1
        # Rename so we don't repeat
        os.rename(txt, migrated_marker)
        logger.info(f"access: migrated {seeded} SUDO users from sudo_users.txt -> DB")
        audit(0, "sudo_migrate_from_txt", f"count={seeded}")
    except Exception as ex:
        logger.warning(f"sudo_users.txt migration failed: {ex}")


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
    with _db_lock, _get_conn() as conn:
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
    row = get_user(uid)
    return bool(row and (row.get("role") or "").upper() == ROLE_OWNER)


def is_owner(uid: int, owner_id: Optional[int]) -> bool:
    """Owner if env OWNER_ID matches OR user has OWNER role stored in DB."""
    if owner_id is not None and uid == owner_id:
        return True
    return _is_owner_in_db(uid)


def count_owners_db() -> int:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM bot_users WHERE role = 'OWNER'"
        ).fetchone()
    return int(row[0]) if row else 0


def first_owner_db() -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bot_users WHERE role = 'OWNER' ORDER BY added_at ASC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row)


def bootstrap_owner(uid: int, username: str = "", first_name: str = "") -> bool:
    """Promote user to OWNER if no OWNER exists yet (env nor DB)."""
    if count_owners_db() > 0:
        return False
    touch_user(uid, username, first_name)
    with _db_lock, _get_conn() as conn:
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
    if env_owner_username:
        u = env_owner_username.lstrip("@").strip()
        if u:
            return f"@{u}"
    row = first_owner_db()
    if row and row.get("username"):
        return f"@{row['username']}"
    if env_owner_id is not None:
        return f"<code>{env_owner_id}</code>"
    if row and row.get("user_id"):
        return f"<code>{row['user_id']}</code>"
    return "<i>owner</i>"


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
        with _db_lock, _get_conn() as conn:
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
    role = role.upper()
    if role not in LEVELS:
        raise ValueError(f"unknown role: {role}")
    touch_user(uid)
    with _db_lock, _get_conn() as conn:
        conn.execute(
            "UPDATE bot_users SET role=?, added_by=?, added_at=?, "
            "expires_at=?, redeem_code_used=? WHERE user_id=?",
            (role, added_by, _now_ts(), expires_at, code or "", uid),
        )
        conn.commit()


def ban_user(uid: int, by_uid: int):
    touch_user(uid)
    with _db_lock, _get_conn() as conn:
        conn.execute("UPDATE bot_users SET is_banned=1 WHERE user_id=?", (uid,))
        conn.commit()
    audit(by_uid, "ban", f"target={uid}")


def unban_user(uid: int, by_uid: int):
    with _db_lock, _get_conn() as conn:
        conn.execute("UPDATE bot_users SET is_banned=0 WHERE user_id=?", (uid,))
        conn.commit()
    audit(by_uid, "unban", f"target={uid}")


def list_role(role: str) -> list[dict]:
    role = role.upper()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_users WHERE role = ? ORDER BY added_at DESC", (role,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows if r]


def list_banned() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_users WHERE is_banned = 1 ORDER BY last_active DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows if r]


def expire_check_all() -> int:
    """Sweep: downgrade all expired PREMIUM/SUDO to GUEST. Returns count."""
    now = _now_ts()
    with _db_lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, role FROM bot_users "
            "WHERE expires_at IS NOT NULL AND expires_at <= ? "
            "AND role IN ('PREMIUM', 'SUDO')",
            (now,)
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE bot_users SET role='GUEST', expires_at=NULL WHERE user_id=?",
                (r["user_id"],),
            )
            audit(r["user_id"], "auto_expire", f"prev_role={r['role']}")
        conn.commit()
    return len(rows)


# ── Code generation + redemption ───────────────────────────────────────────
def _gen_code_string() -> str:
    groups = ["".join(secrets.choice(_CODE_CHARSET) for _ in range(_CODE_GROUP_LEN))
              for _ in range(_CODE_GROUPS)]
    return f"{_CODE_PREFIX}-" + "-".join(groups)


def normalize_code(code: str) -> str:
    return (code or "").strip().upper()


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
        d["redeemed_by"] = json.loads(d.get("redeemed_by") or "[]")
    except Exception:
        d["redeemed_by"] = []
    return d


def create_code(created_by: int, duration_days: int, max_uses: int = 1,
                notes: str = "") -> dict:
    if max_uses < 1:
        raise ValueError("max_uses must be >= 1")
    if duration_days < 0:
        raise ValueError("duration_days must be >= 0")
    # Generate unique code
    for _ in range(20):
        code = _gen_code_string()
        if not get_code(code):
            break
    else:
        raise RuntimeError("Could not generate unique code after 20 attempts")
    now = _now_ts()
    with _db_lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO redeem_codes (code, created_by, created_at, duration_days, "
            "max_uses, current_uses, is_active, notes) VALUES (?, ?, ?, ?, ?, 0, 1, ?)",
            (code, created_by, now, duration_days, max_uses, notes or ""),
        )
        conn.commit()
    audit(created_by, "code_create", f"code={code} days={duration_days} max_uses={max_uses}")
    return get_code(code)


def list_codes(filter_by: str = "all") -> list[dict]:
    """filter_by: 'all', 'active', 'used' (current_uses>=max_uses), 'expired' (is_active=0)"""
    with _get_conn() as conn:
        if filter_by == "active":
            rows = conn.execute(
                "SELECT * FROM redeem_codes WHERE is_active = 1 AND current_uses < max_uses "
                "ORDER BY created_at DESC"
            ).fetchall()
        elif filter_by == "used":
            rows = conn.execute(
                "SELECT * FROM redeem_codes WHERE current_uses >= max_uses "
                "ORDER BY created_at DESC"
            ).fetchall()
        elif filter_by == "expired":
            rows = conn.execute(
                "SELECT * FROM redeem_codes WHERE is_active = 0 ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM redeem_codes ORDER BY created_at DESC"
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["redeemed_by"] = json.loads(d.get("redeemed_by") or "[]")
        except Exception:
            d["redeemed_by"] = []
        out.append(d)
    return out


def revoke_code(code: str, by_uid: int) -> bool:
    code = normalize_code(code)
    with _db_lock, _get_conn() as conn:
        cur = conn.execute(
            "UPDATE redeem_codes SET is_active = 0 WHERE code = ? AND is_active = 1",
            (code,),
        )
        conn.commit()
        if cur.rowcount > 0:
            audit(by_uid, "code_revoke", f"code={code}")
            return True
    return False


def redeem_code(code: str, uid: int, owner_id: Optional[int]) -> tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """
    Try to redeem `code` for user `uid`.
    Returns (success, granted_role, expires_at, error_key).
    Error keys: 'invalid', 'expired', 'exhausted', 'already_redeemed',
                'already_premium', 'banned'.
    """
    if is_banned(uid):
        return False, None, None, "banned"
    code = normalize_code(code)
    if not code or not code.startswith(_CODE_PREFIX):
        return False, None, None, "invalid"
    row = get_code(code)
    if not row:
        return False, None, None, "invalid"
    if not row.get("is_active"):
        return False, None, None, "expired"
    if row.get("current_uses", 0) >= row.get("max_uses", 1):
        return False, None, None, "exhausted"
    if uid in row.get("redeemed_by", []):
        return False, None, None, "already_redeemed"
    # Block sudo/owner from redeeming
    current_role = get_role(uid, owner_id)
    if role_level(current_role) >= LEVELS[ROLE_SUDO]:
        return False, None, None, "already_premium"

    # Compute new expires_at: extend if already premium with future expiry
    days = row["duration_days"]
    now = _now_ts()
    new_expires: Optional[int]
    if days <= 0:
        new_expires = None  # permanent
    else:
        existing = get_user(uid)
        base = existing.get("expires_at") if existing else None
        if current_role == ROLE_PREMIUM and base and base > now:
            new_expires = base + days * 86400  # extend
        else:
            new_expires = now + days * 86400

    # Set role + record redemption (atomic)
    with _db_lock, _get_conn() as conn:
        # Re-check inside lock to avoid race
        cur = conn.execute(
            "SELECT current_uses, max_uses, is_active, redeemed_by FROM redeem_codes WHERE code=?",
            (code,)
        ).fetchone()
        if not cur:
            return False, None, None, "invalid"
        if not cur["is_active"]:
            return False, None, None, "expired"
        if cur["current_uses"] >= cur["max_uses"]:
            return False, None, None, "exhausted"
        try:
            redeemed = json.loads(cur["redeemed_by"] or "[]")
        except Exception:
            redeemed = []
        if uid in redeemed:
            return False, None, None, "already_redeemed"
        redeemed.append(uid)
        new_uses = cur["current_uses"] + 1
        conn.execute(
            "UPDATE redeem_codes SET current_uses=?, redeemed_by=? WHERE code=?",
            (new_uses, json.dumps(redeemed), code),
        )
        # Touch + set role
        existing = conn.execute(
            "SELECT user_id FROM bot_users WHERE user_id=?", (uid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE bot_users SET role='PREMIUM', added_by=?, added_at=?, "
                "expires_at=?, redeem_code_used=?, last_active=? WHERE user_id=?",
                (row["created_by"], now, new_expires, code, now, uid),
            )
        else:
            conn.execute(
                "INSERT INTO bot_users (user_id, role, added_by, added_at, "
                "expires_at, redeem_code_used, last_active) VALUES (?, 'PREMIUM', ?, ?, ?, ?, ?)",
                (uid, row["created_by"], now, new_expires, code, now),
            )
        conn.commit()
    audit(uid, "code_redeem", f"code={code} expires_at={new_expires}")
    return True, ROLE_PREMIUM, new_expires, None


# ── Stats ────────────────────────────────────────────────────────────────────
def stats() -> dict:
    out = {"users_total": 0, "by_role": {}, "banned": 0,
           "codes_total": 0, "codes_active": 0, "codes_redeemed": 0}
    with _get_conn() as conn:
        out["users_total"] = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
        for r in conn.execute("SELECT role, COUNT(*) c FROM bot_users GROUP BY role").fetchall():
            out["by_role"][r["role"]] = r["c"]
        out["banned"] = conn.execute("SELECT COUNT(*) FROM bot_users WHERE is_banned=1").fetchone()[0]
        out["codes_total"] = conn.execute("SELECT COUNT(*) FROM redeem_codes").fetchone()[0]
        out["codes_active"] = conn.execute(
            "SELECT COUNT(*) FROM redeem_codes WHERE is_active=1 AND current_uses < max_uses"
        ).fetchone()[0]
        out["codes_redeemed"] = conn.execute(
            "SELECT COALESCE(SUM(current_uses), 0) FROM redeem_codes"
        ).fetchone()[0]
    return out


def all_user_ids() -> list[int]:
    """All known user IDs (for legacy SUDO_USERS sync + broadcast lists)."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM bot_users").fetchall()
    return [r["user_id"] for r in rows]


def sudo_user_ids() -> set[int]:
    """SUDO + OWNER role IDs (DB-side cache for legacy SUDO_USERS set)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM bot_users WHERE role IN ('SUDO', 'OWNER')"
        ).fetchall()
    return {r["user_id"] for r in rows}


def premium_or_higher_ids() -> set[int]:
    """PREMIUM + SUDO + OWNER — i.e., everyone with active fetch privileges."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM bot_users "
            "WHERE role IN ('PREMIUM', 'SUDO', 'OWNER') AND is_banned = 0"
        ).fetchall()
    return {r["user_id"] for r in rows}
