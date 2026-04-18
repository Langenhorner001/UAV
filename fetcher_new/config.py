import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip()
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_HASH", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set.")
# OWNER_ID is now optional — bot supports /claimowner bootstrap if unset.
if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
    raise ValueError("TELEGRAM_API and TELEGRAM_HASH environment variables are required.")

SESSION_FILE     = "owner_session.txt"
SUDO_USERS_FILE  = "sudo_users.txt"  # legacy — auto-migrated to DB on init

# If SESSION_STRING is set as an env var, it takes priority over the session file
SESSION_STRING_ENV = os.getenv("SESSION_STRING", "").strip()


# ── Sudo Users ────────────────────────────────────────────────────────────────
# DB-backed via bot.access.sudo_user_ids(). The in-memory set below is a
# CACHE that link_handler.py reads — refreshed on every add/remove and at boot.

def _load_sudo_users() -> set[int]:
    """Initial load: env var + DB (DB takes precedence after migration)."""
    users: set[int] = set()
    env_val = os.getenv("SUDO_USERS", "").strip()
    for part in env_val.split(","):
        part = part.strip()
        if part.isdigit():
            users.add(int(part))
    # DB load (best-effort — table may not exist on first boot)
    try:
        from bot import access as _ac
        users |= _ac.sudo_user_ids()
    except Exception:
        pass
    return users


def _save_sudo_users(users: set[int]) -> None:
    """Legacy stub — DB is now the source of truth, no-op for compatibility."""
    return


def refresh_sudo_users() -> None:
    """Re-read SUDO ids from DB into the in-memory cache."""
    global SUDO_USERS
    try:
        from bot import access as _ac
        SUDO_USERS = _ac.sudo_user_ids() | {OWNER_ID} if OWNER_ID else _ac.sudo_user_ids()
    except Exception:
        pass


SUDO_USERS: set[int] = _load_sudo_users()
