import os
import time
import logging
import threading
import itertools

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from keep_alive import keep_alive, start_self_pinger
import tor_manager
import proxy_scraper

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import db
import access

_LOG_FILE = "/tmp/bot-logs.txt"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Rotating file handler — /logs command ke liye ──────────────────────────
try:
    from logging.handlers import RotatingFileHandler as _RFH
    _file_handler = _RFH(_LOG_FILE, maxBytes=4 * 1024 * 1024, backupCount=1,
                         encoding="utf-8", delay=False)
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except Exception as _log_ex:
    logger.warning(f"Log file handler setup failed: {_log_ex}")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set. Bot cannot start.")

# Optional: only this user gets online/offline alerts (must be a valid integer Telegram user ID)
_owner_raw = os.environ.get("OWNER_ID", "").strip()
OWNER_ID: int | None = int(_owner_raw) if _owner_raw.lstrip("-").isdigit() else None
OWNER_USERNAME: str = os.environ.get("OWNER_USERNAME", "").strip().lstrip("@")

# Comma-separated Telegram user IDs allowed to use the bot.
# Leave empty to allow ALL users (open bot).
# Example in .env: ALLOWED_USERS=123456789,987654321
ALLOWED_USERS: set[int] = set(
    int(x.strip()) for x in os.environ.get("ALLOWED_USERS", "").split(",")
    if x.strip().lstrip("-").isdigit()
)

# ── ENH-004: Maintenance mode (owner-only toggle) ────────────────────────────
_MAINT_MODE: bool = False
# Commands that remain available even in maintenance mode
_MAINT_ALLOWED_CMDS = frozenset({
    "maint", "s", "status", "ping", "start", "help", "menu",
})

# ── Access control: command level requirements ───────────────────────────────
# Owner-only: never executable by SUDO/PREMIUM/GUEST
_OWNER_ONLY_CMDS = frozenset({
    "addsudo", "removesudo", "sudolist",
    "ban", "unban", "banned",
    "maint", "audit", "backup",
    "diag", "logs",
})
# SUDO or higher (Owner + SUDO can run)
_SUDO_PLUS_CMDS = frozenset({
    "addpremium", "removepremium", "premiumlist",
    "code", "codes", "revokecode",
    "userinfo", "accstats",
})

# ── ENH-001: Per-user rate limiter (sliding window) ──────────────────────────
# Heavy commands → max N calls per WINDOW seconds per user.
import collections as _coll
_RATE_LIMITED_CMDS = frozenset({
    "run", "restart", "rs", "chkpxy", "chk",
    "redeem",   # prevent brute-forcing codes
})
_RATE_LIMIT_MAX    = 3        # max calls
_RATE_LIMIT_WINDOW = 60       # seconds
_rate_lock = threading.Lock()
_rate_history: dict[int, dict[str, _coll.deque]] = {}

def _rate_limited(uid: int, cmd: str) -> tuple[bool, int]:
    """Return (is_limited, retry_after_s). cmd is normalized command name."""
    if cmd not in _RATE_LIMITED_CMDS:
        return False, 0
    now = time.time()
    with _rate_lock:
        per_user = _rate_history.setdefault(uid, {})
        dq = per_user.setdefault(cmd, _coll.deque(maxlen=_RATE_LIMIT_MAX + 1))
        # Drop entries outside the window
        while dq and now - dq[0] > _RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT_MAX:
            retry = int(_RATE_LIMIT_WINDOW - (now - dq[0])) + 1
            return True, max(retry, 1)
        dq.append(now)
        return False, 0

# ── ENH-005: Audit log for sensitive actions ─────────────────────────────────
_AUDIT_FILE = "/tmp/uav-audit.log"
_audit_lock = threading.Lock()

def _audit(uid: int, action: str, details: str = ""):
    """Append a line to the audit log. Thread-safe, non-blocking on failure."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}  uid={uid}  {action}"
        if details:
            # Trim to keep log readable
            details = details.replace("\n", " ").replace("\r", " ")[:200]
            line += f"  | {details}"
        with _audit_lock:
            with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as ex:
        logger.debug(f"audit write failed: {ex}")


def _normalize_cmd(update: Update) -> str:
    """Extract a command name from /cmd or .cmd messages (lowercase, no @bot)."""
    if not update.message or not update.message.text:
        return ""
    txt = update.message.text.lstrip()
    if not txt or txt[0] not in ("/", "."):
        return ""
    body = txt[1:].split(maxsplit=1)[0]
    if "@" in body:
        body = body.split("@", 1)[0]
    return body.lower()


async def _auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject unauthorized users; enforce maintenance mode + rate limiting."""
    from telegram.ext import ApplicationHandlerStop
    if not update.effective_user:
        raise ApplicationHandlerStop
    uid = update.effective_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        logger.warning(f"Unauthorized access attempt: user_id={uid}, name={update.effective_user.full_name!r}")
        raise ApplicationHandlerStop
    # Auto-register user in DB on first contact
    db.add_user(uid)
    # Track presence in access registry too (for role/last_active)
    try:
        u = update.effective_user
        access.touch_user(uid, u.username or "", u.first_name or "")
    except Exception as _ex:
        logger.debug(f"access.touch_user failed for {uid}: {_ex}")

    cmd_name = _normalize_cmd(update)

    # ── Access control gates (role-based) ───────────────────────────────
    # Owner bypasses every gate below.
    if not access.is_owner(uid, OWNER_ID):
        # 1. Banned users → hard block
        if access.is_banned(uid):
            if update.message and cmd_name:
                await update.message.reply_text(
                    "🚫 Aap is bot se ban kar diye gaye hain.\n"
                    "Owner se contact karein agar yeh ghalti se hua hai."
                )
            raise ApplicationHandlerStop

        role = access.get_role(uid, OWNER_ID)  # auto-downgrades expired users

        # 2. Owner-only commands → block non-owners
        if cmd_name in _OWNER_ONLY_CMDS:
            if update.message:
                await update.message.reply_text(
                    "⛔ Sirf *Owner* is command ko use kar sakta hai.",
                    parse_mode="Markdown",
                )
            raise ApplicationHandlerStop

        # 3. SUDO-or-higher commands → block GUEST/PREMIUM
        if cmd_name in _SUDO_PLUS_CMDS and access.role_level(role) < access.LEVELS[access.ROLE_SUDO]:
            if update.message:
                await update.message.reply_text(
                    "⛔ Sirf *SUDO* ya *Owner* is command ko use kar sakte hain.",
                    parse_mode="Markdown",
                )
            raise ApplicationHandlerStop

        # 4. GUEST → only allow whitelist (start/help/redeem/myaccess/menu/ping)
        if role == access.ROLE_GUEST and cmd_name and cmd_name not in access.GUEST_ALLOWED_CMDS:
            if update.message:
                owner_link = access.get_owner_display(OWNER_ID, OWNER_USERNAME)
                # If still no owner exists anywhere, hint the bootstrap path
                no_owner_hint = ""
                if OWNER_ID is None and access.count_owners_db() == 0:
                    no_owner_hint = (
                        "\n\n⚙️  *Setup pending:*\n"
                        "_Agar aap is bot ke owner hain to_ `/claimowner` _bhejein._"
                    )
                await update.message.reply_text(
                    "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    "   🔒 *ACCESS RESTRICTED*\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "_Yeh bot invite-only hai._\n\n"
                    "🎟  *Code hai?*\n"
                    "   `/redeem YOUR-CODE`\n\n"
                    "💬  *Access chahiye?*\n"
                    f"   Owner se contact karein:  {owner_link}\n\n"
                    f"_Aapki ID:_ `{uid}`"
                    f"{no_owner_hint}",
                    parse_mode="Markdown",
                )
            raise ApplicationHandlerStop

    # ENH-004: Maintenance mode — non-owner blocked from most commands
    if _MAINT_MODE and uid != OWNER_ID and cmd_name and cmd_name not in _MAINT_ALLOWED_CMDS:
        if update.message:
            await update.message.reply_text(
                "🔧 Bot abhi maintenance mein hai — kuch der baad try karein.\n"
                "(Admin ne briefly disable kiya hai. Status ke liye /s allowed hai.)"
            )
        raise ApplicationHandlerStop

    # ENH-001: Rate limit on heavy commands
    if cmd_name:
        limited, retry = _rate_limited(uid, cmd_name)
        if limited:
            if update.message:
                await update.message.reply_text(
                    f"⏳ Rate limit: `/{cmd_name}` zyada use ho raha hai.\n"
                    f"{retry}s mein dobara try karein.\n"
                    f"(Limit: {_RATE_LIMIT_MAX} per {_RATE_LIMIT_WINDOW}s)",
                    parse_mode="Markdown",
                )
            logger.info(f"Rate-limited /{cmd_name} for uid={uid} (retry in {retry}s)")
            raise ApplicationHandlerStop


def _find_binary(*candidates) -> str:
    import shutil
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    for c in candidates:
        if c:
            found = shutil.which(c.split("/")[-1])
            if found:
                return found
    logger.warning(f"_find_binary: none of the candidates found: {candidates}")
    return candidates[0]

def _is_valid_chromedriver(path: str) -> bool:
    """Run path --version and confirm output contains 'ChromeDriver'."""
    import subprocess
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        out = subprocess.check_output(
            [path, "--version"], stderr=subprocess.STDOUT, timeout=5
        ).decode(errors="ignore")
        return "ChromeDriver" in out
    except Exception:
        return False


def _get_major_version(binary_path: str, is_chromedriver: bool = False) -> int | None:
    """Return major version number (e.g. 124) from chromium/chromedriver --version output."""
    import subprocess, re
    if not binary_path or not os.path.isfile(binary_path):
        return None
    try:
        out = subprocess.check_output(
            [binary_path, "--version"], stderr=subprocess.STDOUT, timeout=5
        ).decode(errors="ignore")
        m = re.search(r"(\d+)\.\d+\.\d+", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _check_version_match(chromedriver_path: str, chromium_path: str) -> bool:
    """
    Return True if major versions match (or if either version can't be read).
    Mismatch is the #1 cause of 'Status code 1' error.
    """
    cd_ver = _get_major_version(chromedriver_path, is_chromedriver=True)
    ch_ver = _get_major_version(chromium_path) if chromium_path else None
    if cd_ver and ch_ver and abs(cd_ver - ch_ver) > 2:
        logger.warning(
            f"⚠️  VERSION MISMATCH — ChromeDriver={cd_ver} Chromium={ch_ver}. "
            f"Run: sudo apt update && sudo apt install --reinstall chromium-browser chromium-chromedriver"
        )
        return False
    if cd_ver and ch_ver:
        logger.info(f"Version match OK — ChromeDriver={cd_ver} Chromium={ch_ver}")
    return True


def _resolve_browser_pair():
    """
    Resolve (chromium_path, chromedriver_path) as a matched pair.

    Strategy (learned from production failures):
    - Prefer APT chromedriver (/usr/bin/chromedriver) — real binary, no snap wrapper issues,
      reliable in systemd service restarts.
    - When APT chromedriver is used, set chromium_path="" so _build_driver skips
      binary_location and lets chromedriver find chromium on PATH via /usr/bin/chromium-browser
      (snap wrapper) — this works because chromedriver uses the snap wrapper properly.
    - Avoid snap chromedriver (/snap/bin/chromium.chromedriver) — it's a shell script that
      calls 'snap run' and fails with exit code 1 on rebuild in systemd (snap state still
      closing from previous session).
    Returns (chromium_path, chromedriver_path). chromium_path may be "" (no binary_location).
    """
    import shutil

    # ── Priority 0: explicit env vars (non-snap only) ─────────────────
    env_chromium     = os.environ.get("CHROMIUM_PATH", "").strip()
    env_chromedriver = os.environ.get("CHROMEDRIVER_PATH", "").strip()

    def _is_snap(p: str) -> bool:
        if not p: return False
        if "snap" in p: return True
        try:
            with open(p, "rb") as f:
                return "snap" in f.read(512).decode(errors="ignore").lower()
        except Exception:
            return False

    if env_chromedriver and not _is_snap(env_chromedriver):
        if _is_valid_chromedriver(env_chromedriver):
            ch = "" if _is_snap(env_chromium) else env_chromium
            logger.info(f"Using env chromedriver: {env_chromedriver}, chromium: {ch or '(PATH)'}")
            return ch, env_chromedriver
        logger.warning(f"Env CHROMEDRIVER_PATH={env_chromedriver!r} invalid — auto-detecting")

    # ── Priority 1: Nix store (Replit environment) ──────────────────
    nix_chromium     = "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium"
    nix_chromedriver = "/nix/store/8zj50jw4w0hby47167kqqsaqw4mm5bkd-chromedriver-unwrapped-138.0.7204.100/bin/chromedriver"
    if os.path.isfile(nix_chromium) and _is_valid_chromedriver(nix_chromedriver):
        logger.info(f"Using Nix pair")
        return nix_chromium, nix_chromedriver

    # ── Priority 2: Non-snap chromedriver + best available chromium ────
    # Ubuntu 22.04+ ships chromium as snap-only. Snap chromedriver fails in
    # systemd system service (needs user session / snap env).
    # Prefer: Google Chrome (non-snap) + /usr/local/bin/chromedriver (installed manually).
    apt_chromedriver_candidates = [
        "/usr/local/bin/chromedriver",      # manually installed (best — always non-snap)
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]
    chromium_candidates = [
        "/usr/bin/google-chrome-stable",    # Google Chrome — non-snap, works in systemd
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for cd in apt_chromedriver_candidates:
        if _is_snap(cd):
            logger.debug(f"Skipping snap chromedriver at Priority 2: {cd}")
            continue
        if not _is_valid_chromedriver(cd):
            continue
        for ch in chromium_candidates:
            if os.path.isfile(ch) and os.access(ch, os.X_OK):
                _check_version_match(cd, ch)   # logs warning if mismatch
                logger.info(f"Using non-snap pair: chromedriver={cd}, chromium={ch}")
                return ch, cd
        # No chromium found — return cd only; chromedriver will discover via PATH
        logger.info(f"Using non-snap chromedriver {cd} — chromium via PATH")
        return "", cd

    # ── Priority 3: webdriver-manager auto-download (Google Chrome ke saath) ─
    # Snap chromedriver skip — fails in systemd. Try webdriver-manager first.
    for chrome_bin in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"):
        if os.path.isfile(chrome_bin) and os.access(chrome_bin, os.X_OK):
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                cd = ChromeDriverManager().install()
                if _is_valid_chromedriver(cd):
                    logger.info(f"Using webdriver-manager chromedriver: {cd}, Chrome: {chrome_bin}")
                    return chrome_bin, cd
            except Exception as e:
                logger.warning(f"webdriver-manager (Chrome) fallback failed: {e}")
            break

    # ── Priority 4: webdriver-manager with Chromium ──────────────────
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from webdriver_manager.core.os_manager import ChromeType
        cd = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
        if _is_valid_chromedriver(cd):
            logger.info(f"Using webdriver-manager chromium chromedriver: {cd}")
            return "", cd
    except Exception as e:
        logger.warning(f"webdriver-manager chromium fallback failed: {e}")

    # ── Priority 5: snap chromedriver (LAST RESORT — may fail in systemd) ─
    snap_cd = "/snap/bin/chromium.chromedriver"
    if os.path.isfile(snap_cd) and os.access(snap_cd, os.X_OK):
        logger.warning(
            "⚠️  Using SNAP chromedriver — this WILL fail in systemd service! "
            "Fix: install Google Chrome (non-snap) + /usr/local/bin/chromedriver. "
            "See /diag in Telegram for instructions."
        )
        return "/usr/bin/chromium-browser", snap_cd

    logger.error("No valid chromedriver found! Run /diag in Telegram.")
    return "", ""


CHROMIUM_PATH, CHROMEDRIVER_PATH = _resolve_browser_pair()

HELP_TEXT = """╭━━━━━━━━━━━━━━━━━━━━━━╮
   💎 *UAV — HELP CENTER* 💎
╰━━━━━━━━━━━━━━━━━━━━━━╯
_Real Browser • Tor Anonymity • Proxy Rotation_
_✦ crafted by ѕonιc ✦_

━━━━━━━━━━━━━━━━━━━━━━
『 ⚙️ *SETUP* 』
🔗  `/url <link>` — _Target URL set karein_
➕  `/ap <p1> <p2>` — _Proxies add (HTTP/SOCKS4/5)_
📋  `/lp` — _Saved proxies list_
🗑  `/clrp` — _Sab proxies delete_
🩺  `/chk` — _Sab proxies live check_
🔎  `/chk <proxy>` — _Single proxy check_

━━━━━━━━━━━━━━━━━━━━━━
『 🧅 *TOR & IP ROTATION* 』
🟢  `/ton`  •  🔴  `/toff` — _Tor mode on/off_
🆔  `/idon` •  ⛔  `/idoff` — _New Identity (~10s, full IP)_
🔀  `/con`  •  ⛔  `/coff` — _New Circuit (~3s, fast)_

━━━━━━━━━━━━━━━━━━━━━━
『 🖱 *CLICK AUTOMATION* 』
👆  `/ct1 <text>` — _Button 1 text se click_
👆  `/ct2 <text>` — _Button 2 text se click_
🎯  `/sel <css>` — _CSS selector 1 (advanced)_
🎯  `/sel2 <css>` — _CSS selector 2 (advanced)_

━━━━━━━━━━━━━━━━━━━━━━
『 ⏱ *TIMING* 』
⏳  `/wait <s>` — _Page load wait (5s)_
🔁  `/delay <s>` — _Loops ke beech gap (5s)_
🔢  `/loops <n>` — _Total loops (0 = ∞)_
⌛  `/tout <s>` — _Element timeout (30s)_
☕  `/bint <n>` — _N loops baad break (50)_
⏸  `/bwait <s>` — _Break duration (60s)_

━━━━━━━━━━━━━━━━━━━━━━
『 🚀 *CONTROL* 』
🚀  `/run` — _Visiting loop START_
🛑  `/stop` — _Loop STOP_
♻️  `/restart` — _Stop + dobara start_
📋  `/menu` — _Premium inline menu_

━━━━━━━━━━━━━━━━━━━━━━
『 📊 *STATUS* 』
📡  `/s` — _Live session panel_
📈  `/stats` — _Runtime + system stats_
🏓  `/ping` — _Bot response time_
💡  `/help` — _Yeh menu_

━━━━━━━━━━━━━━━━━━━━━━
『 🛡 *ADMIN ONLY* 』
🔧  `/maint on|off` — _Maintenance mode toggle_
👑  `/audit [n]` — _Sensitive actions log_
💾  `/backup` — _SQLite DB snapshot_

━━━━━━━━━━━━━━━━━━━━━━
『 💡 *QUICK START* 』
1️⃣  `/url https://yoursite.com`
2️⃣  `/ton` → `/idon`
3️⃣  `/ct1 Sign Up`
4️⃣  `/run`

━━━━━━━━━━━━━━━━━━━━━━
✨ _Tip: kisi command pe tap karke instant run_
🆘 _Stuck? Owner se contact karein_
━━━━━━━━━━━━━━━━━━━━━━"""


# Owner-only addendum appended to HELP_TEXT for the owner only.
_HELP_OWNER_EXTRA = """

━━━━━━━━━━━━━━━━━━━━━━
『 👑 *OWNER-ONLY DIAGNOSTICS* 』
🔬  `/diag` — _Browser + driver check_
📜  `/logs` — _Bot log file download_
━━━━━━━━━━━━━━━━━━━━━━"""


def _is_owner_uid(uid: int | None) -> bool:
    """Owner = env OWNER_ID match OR stored as OWNER role in DB."""
    if uid is None:
        return False
    if OWNER_ID is not None and uid == OWNER_ID:
        return True
    try:
        return access.is_owner(uid, None)  # checks DB
    except Exception:
        return False


def _help_for(uid: int | None) -> str:
    """Return help text — owner sees diag/logs section, others don't."""
    if _is_owner_uid(uid):
        return HELP_TEXT + _HELP_OWNER_EXTRA
    return HELP_TEXT


class VisitorSession:
    def __init__(self):
        self.url: str = ""
        self.proxies: list[str] = []
        self.delay: float = 5.0
        self.page_load_wait: float = 5.0
        self.loops: int = 0
        self.timeout: int = 30
        self.primary_selector: str = ""
        self.secondary_selector: str = ""
        self.click_text: str = ""
        self.click_text2: str = ""
        self.tor_mode: bool = False
        self.identity_mode: bool = True
        self.circuit_mode: bool = False
        self.break_interval: int = 50    # pause after every N loops (0 = disabled)
        self.break_duration: int = 60    # pause duration in seconds
        self.running: bool = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.loop_count: int = 0
        self.error_count: int = 0
        self.start_time: float = 0.0
        self.last_ip: str = ""
        self.last_status: str = "Idle"
        self.last_error: str = ""


_sessions: dict[int, VisitorSession] = {}


def get_session(user_id: int) -> VisitorSession:
    if user_id not in _sessions:
        s = VisitorSession()
        data = db.load_user(user_id)
        if data:
            s.url                = data.get("url", "")
            s.click_text         = data.get("click_text", "")
            s.click_text2        = data.get("click_text2", "")
            s.primary_selector   = data.get("primary_selector", "")
            s.secondary_selector = data.get("secondary_selector", "")
            s.delay              = float(data.get("delay", 5.0))
            s.page_load_wait     = float(data.get("page_load_wait", 5.0))
            s.loops              = int(data.get("loops", 0))
            s.timeout            = int(data.get("timeout", 30))
            s.proxies            = data.get("proxies", [])
            s.identity_mode      = bool(data.get("identity_mode", 1))
            s.circuit_mode       = bool(data.get("circuit_mode", 0))
            s.tor_mode           = bool(data.get("tor_mode", 0))
            s.break_interval     = int(data.get("break_interval", 50))
            s.break_duration     = int(data.get("break_duration", 60))
        _sessions[user_id] = s
    return _sessions[user_id]


def _save_session(user_id: int, session: VisitorSession):
    db.save_user(
        user_id,
        url=session.url,
        click_text=session.click_text,
        click_text2=session.click_text2,
        primary_selector=session.primary_selector,
        secondary_selector=session.secondary_selector,
        delay=session.delay,
        page_load_wait=session.page_load_wait,
        loops=session.loops,
        timeout=session.timeout,
        proxies=session.proxies,
        identity_mode=int(session.identity_mode),
        circuit_mode=int(session.circuit_mode),
        tor_mode=int(session.tor_mode),
        break_interval=session.break_interval,
        break_duration=session.break_duration,
    )


_SOCKS5_PORTS = {1080, 1081, 1085, 9050, 9150, 9051}
_SOCKS4_PORTS = {1080, 1081}


def _auto_scheme(port: int, explicit: str | None) -> str:
    if explicit:
        return explicit
    if port in _SOCKS5_PORTS:
        return "socks5h"
    return "http"


def _parse_proxy_parts(proxy_str: str):
    import re
    s = proxy_str.strip()
    if not s:
        return None
    # Strip optional {Notes} and [Refresh URL] parts
    s = re.sub(r'\{[^}]*\}', '', s)
    s = re.sub(r'\[[^\]]*\]', '', s)
    s = s.strip()
    if not s:
        return None
    try:
        # Detect explicit scheme prefix
        explicit_scheme = None
        scheme_match = re.match(r'^(socks5h?|socks4a?|https?)://', s, re.IGNORECASE)
        if scheme_match:
            raw = scheme_match.group(1).lower()
            if raw in ("https", "http"):
                explicit_scheme = "http"
            elif raw in ("socks4", "socks4a"):
                explicit_scheme = "socks4"
            else:
                explicit_scheme = "socks5h"
            s = s[scheme_match.end():]

        user = password = ""
        if "@" in s:
            # Format: user:pass@host:port
            auth, hostport = s.rsplit("@", 1)
            user, password = auth.split(":", 1) if ":" in auth else (auth, "")
            host, port = hostport.rsplit(":", 1)
        else:
            parts = s.split(":")
            if len(parts) == 4:
                # Format: host:port:user:pass
                host, port, user, password = parts
            elif len(parts) == 2:
                # Format: host:port
                host, port = parts
            else:
                return None

        port_int = int(port.strip())
        scheme = _auto_scheme(port_int, explicit_scheme)
        return scheme, host.strip(), port_int, user.strip(), password.strip()
    except Exception:
        return None


def _proxy_uri(scheme: str, host: str, port: int, user: str, password: str) -> str:
    """URI for requests library — socks5h resolves DNS at proxy."""
    auth = f"{user}:{password}@" if user else ""
    return f"{scheme}://{auth}{host}:{port}"


def _rebuild_driver_for_proxy(driver, proxy_raw: str | None):
    """
    Quit the current driver and launch a fresh one with the new proxy.
    With plain selenium (no selenium-wire), Chrome's proxy is baked into launch
    args, so we must rebuild on every proxy change.
    Returns the new driver (caller must replace their reference).
    """
    import subprocess as _sp
    _safe_quit(driver)
    for proc in ("chromium.chromedriver", "chromedriver", "chromium-browser", "chromium"):
        try:
            _sp.run(["pkill", "-9", "-f", proc], timeout=5,
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
    time.sleep(3)
    return _build_driver(proxy_raw)


def _check_single_proxy(proxy_raw: str) -> dict:
    import requests as _req
    parts = _parse_proxy_parts(proxy_raw)
    if not parts:
        return {"status": "INVALID", "ip": "", "country": "", "city": "", "isp": "", "error": "Bad format"}
    scheme, host, port, user, password = parts
    uri = _proxy_uri(scheme, host, port, user, password)
    # Use socks5h:// for requests when scheme is socks5 (remote DNS via proxy)
    req_uri = uri.replace("socks5://", "socks5h://", 1) if uri.startswith("socks5://") else uri
    prx = {"http": req_uri, "https": req_uri}
    try:
        # Test HTTPS — plain HTTP proxies that can't CONNECT-tunnel will fail here,
        # preventing false-LIVE proxies that then fail inside Chrome on HTTPS sites.
        r = _req.get("https://ipinfo.io/json", proxies=prx, timeout=15)
        if r.status_code == 200:
            data = r.json()
            ext_ip = data.get("ip", "")
            country = data.get("country", "")
            city = data.get("city", "")
            isp = data.get("org", "")
            return {
                "status": "LIVE",
                "ip": ext_ip,
                "country": country,
                "city": city,
                "isp": isp,
                "error": "",
            }
        return {"status": "DEAD", "ip": "", "country": "", "city": "", "isp": "", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"status": "DEAD", "ip": "", "country": "", "city": "", "isp": "", "error": str(e)[:80]}


def _build_driver(proxy_raw: str | None):
    # Plain selenium — no selenium-wire (eliminates "Can't find free port" bug).
    # Proxy is configured via Chrome's native --proxy-server flag.
    if not CHROMEDRIVER_PATH:
        raise RuntimeError(
            "ChromeDriver nahi mila! EC2 par yeh command chalayen:\n"
            "  which chromedriver\n"
            "  find / -name 'chromedriver' 2>/dev/null\n"
            "Phir CHROMEDRIVER_PATH env var set karein systemd service mein."
        )
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    options = Options()

    # ── binary_location: snap chromium ke liye SET NAHI KARTE ────────
    # Snap chromedriver apna snap chromium khud dhundh leta hai.
    # binary_location set karne se snap chromium shell wrapper fail karta hai
    # kyunki snap env vars (SNAP_USER_DATA etc.) available nahi hote.
    # Sirf real non-snap binaries ke liye set karo.
    def _is_snap_bin(p: str) -> bool:
        if not p:
            return True   # empty = don't set binary_location
        if "snap" in p:
            return True
        try:
            with open(p, "rb") as f:
                return "snap" in f.read(512).decode(errors="ignore").lower()
        except Exception:
            return False

    # Set binary_location whenever CHROMIUM_PATH is provided.
    # NOTE: /usr/bin/chromium-browser (snap wrapper) WORKS as binary_location
    # with apt chromedriver — proven in production (165 loops). Do not skip it.
    if CHROMIUM_PATH:
        options.binary_location = CHROMIUM_PATH
        logger.info(f"binary_location set to: {CHROMIUM_PATH}")
    else:
        logger.info(f"binary_location NOT set — chromedriver will discover chromium via PATH")

    options.page_load_strategy = "eager"

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-namespace-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--window-size=1280,800")

    # ── UNIQUE user-data-dir per launch ──────────────────────────────────────
    # Without this, leftover `/tmp/.org.chromium.Chromium.*` lockfiles from a
    # crashed prior instance prevent DevToolsActivePort file creation →
    # "session not created: DevToolsActivePort file doesn't exist".
    #
    # CRITICAL — Snap-aware path selection:
    # Ubuntu 22.04+ ships chromium ONLY as a snap. Snap's AppArmor profile
    # DENIES writes to `/tmp/...` — Chrome silently redirects --user-data-dir
    # to `$HOME/snap/chromium/common/...` and writes DevToolsActivePort there,
    # while selenium keeps polling the original /tmp/ path → 60s timeout →
    # "DevToolsActivePort file doesn't exist". The launch actually succeeds!
    # We detect snap chromium and place the profile inside the snap-allowed
    # tree so selenium and chromium agree on the path.
    import tempfile as _tf
    _is_snap_chromium = (
        ("/snap/" in (CHROMIUM_PATH or "")) or
        ("/snap/" in (CHROMEDRIVER_PATH or "")) or
        _is_snap_bin(CHROMIUM_PATH or "/usr/bin/chromium-browser")
    )
    if _is_snap_chromium:
        _snap_root = os.path.expanduser("~/snap/chromium/common/uav-profiles")
        os.makedirs(_snap_root, mode=0o700, exist_ok=True)
        _profile_dir = _tf.mkdtemp(prefix="uav-", dir=_snap_root)
        logger.info(f"Snap chromium detected — profile in snap tree: {_profile_dir}")
    else:
        _profile_dir = _tf.mkdtemp(prefix="uav-chrome-profile-", dir="/tmp")
        logger.info(f"Non-snap chromium — profile in /tmp: {_profile_dir}")
    options.add_argument(f"--user-data-dir={_profile_dir}")
    options.add_argument(f"--data-path={_profile_dir}")
    options.add_argument(f"--disk-cache-dir={_profile_dir}/cache")
    options.add_argument(f"--crash-dumps-dir={_profile_dir}/crashes")
    # Let chromium pick a free debugging port — avoids collision with leftover instances.
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--no-first-run")
    options.add_argument("--mute-audio")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    )

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
    })

    # Configure proxy via Chrome native flags (no third-party proxy threads)
    if proxy_raw:
        parts = _parse_proxy_parts(proxy_raw)
        if parts:
            scheme, host, port, user, password = parts
            if scheme.startswith("socks5"):
                proxy_flag = f"socks5://{host}:{port}"
            elif scheme.startswith("socks4"):
                proxy_flag = f"socks4://{host}:{port}"
            else:
                proxy_flag = f"http://{host}:{port}"
            options.add_argument(f"--proxy-server={proxy_flag}")
            options.add_argument("--proxy-bypass-list=<-loopback>")

    # Log → /tmp/chromedriver-bot.log for post-mortem debugging.
    # DO NOT pass --verbose via service_args — ChromeDriver 115+ removed that flag;
    # passing it causes immediate exit with code 1 (was the bug we were chasing).
    # log_output was added in selenium 4.6 — fall back to no logging for older installs.
    try:
        _cd_log_f = open("/tmp/chromedriver-bot.log", "w")
        service = Service(executable_path=CHROMEDRIVER_PATH, log_output=_cd_log_f)
    except TypeError:
        # Older selenium (<4.6) doesn't have log_output param — use basic Service
        service = Service(executable_path=CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    # Store proxy_raw on driver for IP detection later
    driver._proxy_raw = proxy_raw
    try:
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception:
        pass
    return driver


def _safe_quit(driver, timeout: int = 12):
    """driver.quit() with hard timeout — kills chromium if selenium-wire proxy hangs."""
    import subprocess as _sp

    def _do_quit():
        try:
            driver.quit()
        except Exception:
            pass

    t = threading.Thread(target=_do_quit, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning(f"driver.quit() timed out after {timeout}s — force-killing chromium")
        for sig in ["chromedriver", "chromium", "chromium-browser"]:
            try:
                _sp.run(["pkill", "-9", "-f", sig], timeout=3)
            except Exception:
                pass


def _detect_ip_via_driver(driver) -> str:
    """
    Detect external IP by hitting an IP-check API via the same proxy as the driver.
    Reads proxy from driver._proxy_raw (set by _build_driver) — no selenium-wire dep.
    """
    import requests as _req
    import json

    # Build requests proxy dict from the raw proxy string stored on the driver.
    proxy_uri = None
    try:
        proxy_raw = getattr(driver, "_proxy_raw", None)
        if proxy_raw:
            parts = _parse_proxy_parts(proxy_raw)
            if parts:
                scheme, host, port, user, password = parts
                uri = _proxy_uri(scheme, host, port, user, password)
                # socks5h: requests resolves DNS via proxy (avoids DNS leaks)
                proxy_uri = uri.replace("socks5://", "socks5h://", 1)
    except Exception:
        pass

    apis = [
        ("https://api.ipify.org?format=json", "ip"),
        ("https://api.my-ip.io/v2/ip.json",   "ip"),
        ("https://api.myip.com",               "ip"),
    ]

    for api_url, key in apis:
        try:
            prx = {"http": proxy_uri, "https": proxy_uri} if proxy_uri else None
            r = _req.get(api_url, proxies=prx, timeout=10)
            if r.status_code == 200:
                data = r.json()
                ip = data.get(key, "")
                if ip:
                    return ip
        except Exception:
            continue

    return "unknown"


def _click_selector(driver, selector: str, timeout: int) -> bool:
    if not selector or not selector.strip():
        return True
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selector.strip()))
        )
        el.click()
        return True
    except TimeoutException:
        logger.warning(f"Timeout waiting for selector: {selector[:50]}")
        return False
    except (NoSuchElementException, WebDriverException) as e:
        logger.warning(f"Click error ({selector[:40]}): {e}")
        return False


def _xpath_safe_str(val: str) -> str:
    """Build an XPath string literal safe for any input — handles embedded quotes."""
    if "'" not in val:
        return f"'{val}'"
    # Use concat() to handle embedded single quotes
    parts = val.split("'")
    concat_args = ", \"'\", ".join(f"'{p}'" for p in parts)
    return f"concat({concat_args})"


def _click_by_text(driver, text: str, timeout: int) -> bool:
    if not text or not text.strip():
        return True
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
    t = text.strip()
    xs = _xpath_safe_str(t)   # XPath-safe string literal — prevents injection
    xpaths = [
        f"//button[normalize-space()={xs}]",
        f"//a[normalize-space()={xs}]",
        f"//button[contains(normalize-space(), {xs})]",
        f"//a[contains(normalize-space(), {xs})]",
        f"//*[@role='button' and contains(normalize-space(), {xs})]",
        f"//*[contains(normalize-space(), {xs}) and (self::button or self::a or self::span or self::div)]",
    ]
    for xpath in xpaths:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            el.click()
            logger.info(f"Clicked by text '{t}' using xpath: {xpath}")
            return True
        except (TimeoutException, NoSuchElementException):
            continue
        except WebDriverException as e:
            logger.warning(f"click_by_text WebDriverException: {e}")
            continue
    logger.warning(f"Could not find clickable element with text: '{t}'")
    return False


_CHECKPOINT_SIGS = [
    "vercel security checkpoint",
    "just a moment",
    "checking your browser",
    "attention required",
    "please wait",
    "ddos-guard",
    "enable javascript and cookies",
    "cloudflare",
    "403 forbidden",
    "access denied",
    "robot or human",
    "are you a bot",
]


def _is_checkpoint_page(driver) -> tuple[bool, str]:
    """Return (True, reason) if the page looks like a bot-challenge/block page."""
    try:
        title = (driver.title or "").strip()
        tl = title.lower()
        for sig in _CHECKPOINT_SIGS:
            if sig in tl:
                return True, title
        # Also check meta-refresh / cf-chl markers in DOM
        try:
            body = driver.find_element("tag name", "body")
            body_text = (body.text or "").lower()[:500]
            for sig in ("cf-chl-widget", "cf_captcha_kind", "__cf_bm", "vercel-challenge"):
                if sig in body_text:
                    return True, title or "Security Checkpoint"
        except Exception:
            pass
    except Exception:
        pass
    return False, ""


def _wait_bypass_checkpoint(driver, stop_event, edit_cb, base_msg,
                             max_wait: int = 45) -> bool:
    """
    Stay on the page and poll every 2s waiting for the checkpoint to auto-resolve.
    Returns True if bypassed/cleared, False if timed out.
    """
    start = time.time()
    attempt = 0
    while not stop_event.is_set():
        elapsed = int(time.time() - start)
        remaining = max_wait - elapsed
        if remaining <= 0:
            return False
        attempt += 1
        try:
            is_blocked, reason = _is_checkpoint_page(driver)
        except Exception:
            return True
        if not is_blocked:
            edit_cb(base_msg + f"[CHECKPOINT] Bypass SUCCESS! (attempt #{attempt}, {elapsed}s mein clear hua)")
            return True
        for tick in range(2, 0, -1):
            if stop_event.is_set():
                return False
            edit_cb(base_msg +
                f"[CHECKPOINT] '{reason}' — auto-bypass wait #{attempt}\n"
                f"Remaining: {remaining}s | Next check: {tick}s..."
            )
            time.sleep(1)
    return False


def _sleep_check(seconds: float, stop_event: threading.Event) -> bool:
    interval = 0.2
    elapsed = 0.0
    while elapsed < seconds:
        if stop_event.is_set():
            return False
        time.sleep(interval)
        elapsed += interval
    return True


def _wait_for_page_ready(driver, stop_event, edit_cb, base_msg,
                         target_selector=None, target_text=None,
                         max_wait=60) -> tuple:
    """
    Phase 1: Poll document.readyState until 'complete'.
    Phase 2: Wait for the target click element to appear in DOM and be visible.
    Sends live Telegram edits every 2 sec.
    Returns (success: bool, note: str)
    """
    start = time.time()
    POLL = 0.5
    UPDATE_EVERY = 2.0
    last_upd = 0.0
    state = "loading"

    # --- Phase 1: readyState == complete ---
    while True:
        if stop_event.is_set():
            return False, "stopped"
        elapsed = time.time() - start
        if elapsed >= max_wait:
            return True, f"timeout ({state})"
        try:
            state = driver.execute_script("return document.readyState") or "loading"
        except Exception:
            pass
        if elapsed - last_upd >= UPDATE_EVERY:
            edit_cb(base_msg + f"[1] Page load kar raha hai... ({int(elapsed)}s | {state})")
            last_upd = elapsed
        if state == "complete":
            break
        time.sleep(POLL)

    # --- Phase 2: target element visible ---
    if target_selector or target_text:
        lbl = (target_text or target_selector or "")[:30]
        while True:
            if stop_event.is_set():
                return False, "stopped"
            elapsed = time.time() - start
            if elapsed >= max_wait:
                return True, "element timeout"
            if elapsed - last_upd >= UPDATE_EVERY:
                edit_cb(base_msg + f"[1] Page ready! '{lbl}' dhundh raha hai... ({int(elapsed)}s)")
                last_upd = elapsed
            found = False
            try:
                if target_text:
                    t = target_text.strip()
                    xs = _xpath_safe_str(t)   # prevents XPath injection on any input
                    for xp in [
                        f"//button[contains(normalize-space(),{xs})]",
                        f"//a[contains(normalize-space(),{xs})]",
                        f"//*[@role='button' and contains(normalize-space(),{xs})]",
                        f"//input[@value and contains(normalize-space(@value),{xs})]",
                        f"//*[contains(normalize-space(),{xs}) and (self::button or self::a or self::span or self::div)]",
                    ]:
                        try:
                            els = driver.find_elements("xpath", xp)
                            if any(e.is_displayed() for e in els if e):
                                found = True
                                break
                        except Exception:
                            continue
                elif target_selector:
                    els = driver.find_elements("css selector", target_selector.strip())
                    if any(e.is_displayed() for e in els if e):
                        found = True
            except Exception:
                pass
            if found:
                return True, "element found"
            time.sleep(POLL)

    return True, "complete"


def _run_loop(session: VisitorSession, send_msg, edit_msg):
    session.loop_count = 0
    session.error_count = 0
    session.start_time = time.time()
    session.last_error = ""
    max_loops = session.loops
    use_tor = session.tor_mode

    proxy_label_short = lambda p: (p.split("@")[-1] if p and "@" in p else (p or "Direct"))

    if use_tor:
        proxy_cycle = itertools.cycle(["__tor__"])
    elif session.proxies:
        proxy_cycle = itertools.cycle(session.proxies)
    else:
        proxy_cycle = itertools.cycle([None])

    if use_tor:
        if session.identity_mode and session.circuit_mode:
            tor_sub = "New Identity + New Circuit"
        elif session.identity_mode:
            tor_sub = "New Identity per loop"
        elif session.circuit_mode:
            tor_sub = "New Circuit per loop"
        else:
            tor_sub = "No rotation"
        mode_label = f"Tor Mode ({tor_sub})"
    else:
        mode_label = f"{len(session.proxies)} proxies"

    send_msg(
        f"=== Bot Start ===\n"
        f"URL: {session.url}\n"
        f"Mode: {mode_label}\n"
        f"Click 1: {session.click_text or session.primary_selector or 'None'}\n"
        f"Click 2: {session.click_text2 or session.secondary_selector or 'None'}\n"
        f"Wait: {session.page_load_wait}s | Delay: {session.delay}s | Loops: {'inf' if max_loops == 0 else max_loops}"
    )

    current_proxy = next(proxy_cycle)

    def _resolve_proxy(p):
        if p == "__tor__":
            return tor_manager.get_proxy()
        return p

    driver = None
    MAX_CONSECUTIVE_FAILS  = 5   # loop-level errors before cooldown
    MAX_LAUNCH_FAILS       = 3   # consecutive browser-launch failures before hard stop
    consecutive_fails      = 0   # resets on successful loop
    consecutive_launch_fails = 0 # resets on successful browser start

    def _kill_leftover_browsers():
        """Kill leftover chromedriver + chromium processes and clean temp profile dirs."""
        import subprocess as _sp, glob as _glob, shutil as _sh
        for proc in ("chromium.chromedriver", "chromedriver",
                     "chromium-browser", "chromium",
                     "google-chrome", "chrome"):
            try:
                _sp.run(["pkill", "-9", "-f", proc], timeout=5,
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except Exception:
                pass
        time.sleep(2)
        # Purge stale lockfiles + previous profile dirs that block DevToolsActivePort.
        cleanup_patterns = [
            "/tmp/.org.chromium.Chromium.*",
            "/tmp/.com.google.Chrome.*",
            "/tmp/.X*-lock",
            "/tmp/uav-chrome-profile-*",
            "/tmp/scoped_dir*",
            "/tmp/chrome_*",
        ]
        for pat in cleanup_patterns:
            for path in _glob.glob(pat):
                try:
                    if os.path.isdir(path):
                        _sh.rmtree(path, ignore_errors=True)
                    else:
                        os.unlink(path)
                except Exception:
                    pass
        time.sleep(1)

    def _chromedriver_log_tail(n=20) -> str:
        """Return last n lines of chromedriver verbose log (for error reporting)."""
        try:
            with open("/tmp/chromedriver-bot.log", "r", errors="replace") as f:
                lines = f.readlines()
            tail = "".join(lines[-n:]).strip()
            return f"\n\nChromeDriver log (last {n} lines):\n{tail}" if tail else ""
        except Exception:
            return ""

    def _try_build_driver(proxy_raw, max_attempts=3):
        """Browser launch karo — max 3 attempts, exponential backoff."""
        last_ex = None
        for i in range(1, max_attempts + 1):
            try:
                _kill_leftover_browsers()
                d = _build_driver(proxy_raw)
                return d
            except Exception as ex:
                last_ex = ex
                logger.warning(f"Browser launch attempt {i}/{max_attempts} failed: {ex}")
                if i < max_attempts:
                    wait = 8 * i   # backoff: 8s, 16s
                    logger.info(f"Waiting {wait}s before retry...")
                    if not _sleep_check(wait, session._stop_event):
                        raise RuntimeError("Bot stop ho gaya during browser launch wait.")
        cd_log = _chromedriver_log_tail()
        raise RuntimeError(f"Browser {max_attempts} attempts ke baad bhi launch nahi hua: {last_ex}{cd_log}")

    try:
        actual_proxy = _resolve_proxy(current_proxy)
        proxy_disp = "Tor" if use_tor else proxy_label_short(actual_proxy)
        # ── Initial browser launch ────────────────────────────────────────
        _init_msg = send_msg(
            f"⏳ Browser shuru ho raha hai...\nProxy: {proxy_disp}"
        )
        driver = _try_build_driver(actual_proxy)
        edit_msg(_init_msg,
            f"✅ Browser ready!\nProxy: {proxy_disp}\nLoop shuru ho raha hai..."
        )

        while not session._stop_event.is_set():
            loop_num = session.loop_count + 1
            actual_proxy = _resolve_proxy(current_proxy)
            proxy_disp = "Tor" if use_tor else proxy_label_short(actual_proxy)
            click_label_1 = session.click_text or session.primary_selector
            click_label_2 = session.click_text2 or session.secondary_selector

            # ── Auto-rebuild driver if it crashed earlier ─────────────
            if driver is None:
                consecutive_launch_fails += 1
                if consecutive_launch_fails > MAX_LAUNCH_FAILS:
                    send_msg(
                        f"⛔ Browser {MAX_LAUNCH_FAILS} baar launch fail hua.\n"
                        f"Bot band ho raha hai. Fix ke baad /run bhejein."
                    )
                    break
                rb_msg = send_msg(
                    f"🔄 Browser rebuild #{consecutive_launch_fails}/{MAX_LAUNCH_FAILS} | Proxy: {proxy_disp}\n"
                    f"Browser restart ho raha hai..."
                )
                try:
                    driver = _try_build_driver(actual_proxy, max_attempts=2)
                    consecutive_launch_fails = 0   # reset on success
                    edit_msg(rb_msg,
                        f"✅ Browser ready! Loop #{loop_num} shuru...\nProxy: {proxy_disp}"
                    )
                except Exception as rb_ex:
                    edit_msg(rb_msg,
                        f"❌ Rebuild fail ({consecutive_launch_fails}/{MAX_LAUNCH_FAILS}): {str(rb_ex)[:80]}\n"
                        f"{'Bot band — /run dobara bhejein.' if consecutive_launch_fails >= MAX_LAUNCH_FAILS else '30s mein retry...'}"
                    )
                    if consecutive_launch_fails >= MAX_LAUNCH_FAILS:
                        break
                    if not _sleep_check(30, session._stop_event):
                        break
                    continue

            # ── New message per loop (old system) ────────────────────
            live_id = send_msg(
                f"--- Loop #{loop_num} ---\n"
                f"Proxy: {proxy_disp}\n"
                f"[1] URL load ho rahi hai..."
            )

            try:
                logger.info(f"Loop #{loop_num} — navigating to {session.url}")
                driver.get(session.url)

                _loop_base_hdr = (
                    f"--- Loop #{loop_num} ---\n"
                    f"Proxy: {proxy_disp}\n"
                )

                pg_ok, pg_note = _wait_for_page_ready(
                    driver,
                    stop_event=session._stop_event,
                    edit_cb=lambda msg: edit_msg(live_id, msg),
                    base_msg=_loop_base_hdr,
                    target_selector=session.primary_selector if not session.click_text else None,
                    target_text=session.click_text or None,
                    max_wait=max(session.timeout, session.page_load_wait),
                )

                if not pg_ok:
                    break

                try:
                    page_title = (driver.title or "")[:50]
                except Exception:
                    page_title = ""

                if session._stop_event.is_set():
                    break

                step_base = (
                    f"--- Loop #{loop_num} ---\n"
                    f"Proxy: {proxy_disp}\n"
                    f"Page: {page_title or 'N/A'} ({pg_note})\n"
                )

                # ── Checkpoint detection ──────────────────────────────────
                checkpoint_failed = False
                c1_status = ""
                c2_status = ""
                is_blocked, block_reason = _is_checkpoint_page(driver)
                if is_blocked:
                    edit_msg(live_id, step_base +
                        f"[CHECKPOINT] '{block_reason}' — bypass try kar raha hai..."
                    )
                    bypassed = _wait_bypass_checkpoint(
                        driver, session._stop_event,
                        lambda msg: edit_msg(live_id, msg),
                        step_base,
                        max_wait=45,
                    )
                    if bypassed:
                        try:
                            page_title = (driver.title or "")[:50]
                        except Exception:
                            pass
                        step_base = (
                            f"--- Loop #{loop_num} ---\n"
                            f"Proxy: {proxy_disp}\n"
                            f"Page: {page_title or 'N/A'} (checkpoint bypassed)\n"
                        )
                    else:
                        checkpoint_failed = True
                        c1_status = f"[CHECKPOINT] bypass fail — IP change hoga\n"
                        session.last_status = "Checkpoint"
                        session.last_error  = f"Checkpoint: {block_reason}"

                # ── Click logic (skipped if checkpoint blocked) ───────────
                clicked_primary = False
                clicked_secondary = False
                if not checkpoint_failed:
                    if click_label_1:
                        edit_msg(live_id, step_base + f"[2] Click 1 '{click_label_1[:25]}' kar raha hai...")
                        if session.click_text:
                            clicked_primary = _click_by_text(driver, session.click_text, session.timeout)
                        elif session.primary_selector:
                            clicked_primary = _click_selector(driver, session.primary_selector, session.timeout)
                        if clicked_primary:
                            time.sleep(1)

                    c1_status = f"Click 1 '{(click_label_1 or '')[:20]}': {'✅' if clicked_primary else '❌'}\n" if click_label_1 else ""

                    if session._stop_event.is_set():
                        break

                    if click_label_2:
                        edit_msg(live_id, step_base + c1_status + f"[3] Click 2 '{click_label_2[:25]}' kar raha hai...")
                        if session.click_text2:
                            clicked_secondary = _click_by_text(driver, session.click_text2, session.timeout)
                        elif session.secondary_selector:
                            clicked_secondary = _click_selector(driver, session.secondary_selector, session.timeout)
                        if clicked_secondary:
                            time.sleep(1)

                    c2_status = f"Click 2 '{(click_label_2 or '')[:20]}': {'✅' if clicked_secondary else '❌'}\n" if click_label_2 else ""

                edit_msg(live_id, step_base + c1_status + c2_status + "[4] IP detect kar raha hai...")

                ip = _detect_ip_via_driver(driver)
                session.last_ip = ip
                session.loop_count += 1
                session.last_status = "OK"
                session.last_error = ""
                consecutive_fails = 0
                consecutive_launch_fails = 0  # successful loop = browser working fine

                logger.info(f"Loop #{session.loop_count} done | IP: {ip} | Proxy: {proxy_disp}")

                tor_status = ""
                if use_tor and not session._stop_event.is_set():
                    do_identity = session.identity_mode
                    do_circuit  = session.circuit_mode and not session.identity_mode

                    if do_identity or do_circuit:
                        action_label = "New Identity" if do_identity else "New Circuit"
                        old_ip = ip

                        def _tor_hdr():
                            return (
                                f"✅ Loop #{session.loop_count} DONE | Proxy: Tor\n"
                                f"Page: {page_title or 'N/A'} ({pg_note})\n"
                                + c1_status + c2_status
                                + f"IP: {old_ip}\n"
                            )

                        edit_msg(live_id, _tor_hdr() + "TOR: Page band kar raha hai...")
                        try:
                            driver.get("about:blank")
                        except Exception:
                            pass
                        _safe_quit(driver)
                        driver = None

                        edit_msg(live_id, _tor_hdr() + f"TOR: {action_label} signal bhej raha hai...")
                        if do_identity:
                            sig_ok = tor_manager.new_identity()
                        else:
                            sig_ok = tor_manager.new_circuit()

                        MAX_VERIFY  = 8
                        VERIFY_WAIT = 3
                        new_ip_verified = ""

                        for attempt in range(1, MAX_VERIFY + 1):
                            if session._stop_event.is_set():
                                break
                            edit_msg(live_id, _tor_hdr() +
                                f"TOR: IP verify kar raha hai ({attempt}/{MAX_VERIFY})..."
                            )
                            candidate = tor_manager.check_ip_via_tor(timeout=8)
                            if candidate and candidate != old_ip:
                                new_ip_verified = candidate
                                break
                            if attempt < MAX_VERIFY and not session._stop_event.is_set():
                                for secs in range(VERIFY_WAIT, 0, -1):
                                    if session._stop_event.is_set():
                                        break
                                    edit_msg(live_id, _tor_hdr() +
                                        f"TOR: IP same ({candidate or '?'}) — {secs}s mein retry ({attempt}/{MAX_VERIFY})"
                                    )
                                    time.sleep(1)

                        edit_msg(live_id, _tor_hdr() +
                            (f"TOR: {old_ip} → {new_ip_verified}\n" if new_ip_verified else f"TOR: IP same raha ({old_ip})\n") +
                            "TOR: Browser restart kar raha hai..."
                        )
                        driver = _try_build_driver(actual_proxy)
                        consecutive_launch_fails = 0  # successful browser start — reset

                        if new_ip_verified:
                            tor_status = f"TOR {action_label}: {old_ip} → {new_ip_verified}\n"
                            session.last_ip = new_ip_verified
                        else:
                            tor_status = f"TOR {action_label}: {'OK' if sig_ok else 'FAIL'} | IP same ({old_ip})\n"

                edit_msg(live_id,
                    f"✅ Loop #{session.loop_count} DONE | Proxy: {proxy_disp}\n"
                    f"Page: {page_title or 'N/A'} ({pg_note})\n"
                    + c1_status
                    + c2_status
                    + f"IP: {ip}\n"
                    + tor_status
                    + f"⏳ Next loop {session.delay}s mein... (Total: {session.loop_count} | Errors: {session.error_count})"
                )

                if max_loops > 0 and session.loop_count >= max_loops:
                    send_msg(f"✅ {session.loop_count} loops complete. Bot done!")
                    break

                if session._stop_event.is_set():
                    break

                if not _sleep_check(session.delay, session._stop_event):
                    break

                # ── Break pause: har N loops ke baad thora rukna ─────────────
                bint = session.break_interval
                if bint > 0 and session.loop_count % bint == 0:
                    bdur = session.break_duration
                    pause_msg = send_msg(
                        f"☕ *Break Time!*\n"
                        f"Loop #{session.loop_count} complete — {bint} loops ho gaye!\n"
                        f"Bot {bdur}s ke liye pause kar raha hai...\n"
                        f"_(Visiting automatically resume hogi)_"
                    )
                    _safe_quit(driver)
                    driver = None  # browser band karo break ke dauran (memory save)
                    for remaining_sec in range(bdur, 0, -1):
                        if session._stop_event.is_set():
                            break
                        if remaining_sec % 15 == 0 or remaining_sec <= 5:
                            try:
                                edit_msg(pause_msg,
                                    f"☕ *Break Time!*\n"
                                    f"Loop #{session.loop_count} complete — {bint} loops ho gaye!\n"
                                    f"Resume hogi: {remaining_sec}s mein..."
                                )
                            except Exception:
                                pass
                        time.sleep(1)
                    if session._stop_event.is_set():
                        break
                    try:
                        edit_msg(pause_msg,
                            f"▶️ *Break khatam!* Loop #{session.loop_count + 1} shuru ho raha hai..."
                        )
                    except Exception:
                        pass

                # ── Proxy rotation (non-Tor): rebuild driver with new proxy ──
                current_proxy = next(proxy_cycle)
                if not use_tor and driver is not None:
                    new_actual = _resolve_proxy(current_proxy)
                    if new_actual != actual_proxy:
                        driver = _rebuild_driver_for_proxy(driver, new_actual)
                        logger.info(f"Proxy rotated → {proxy_label_short(new_actual)}")

            except Exception as e:
                session.last_error = str(e)[:100]
                session.last_status = "Error"
                err_short = str(e)[:120]
                session.error_count += 1
                consecutive_fails += 1
                logger.error(f"Loop #{loop_num} error (fail #{consecutive_fails}/{MAX_CONSECUTIVE_FAILS}): {e}")

                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    send_msg(
                        f"⛔ {MAX_CONSECUTIVE_FAILS} consecutive loop errors.\n"
                        f"Bot band ho raha hai. Browser/proxy issue fix karke /run dobara bhejein.\n"
                        f"Last error: {err_short}"
                    )
                    break

                send_msg(
                    f"⚠️ Loop #{loop_num} ERROR ({consecutive_fails}/{MAX_CONSECUTIVE_FAILS})\n"
                    f"Proxy: {proxy_disp}\nError: {err_short}\n"
                    f"15s mein browser restart hoga..."
                )

                if not _sleep_check(15, session._stop_event):
                    break

                _safe_quit(driver)
                driver = None
                # Next iteration ka driver is None block rebuild handle karega
                current_proxy = next(proxy_cycle)

    except Exception as e:
        session.last_error = str(e)[:100]
        session.last_status = "Launch Error"
        logger.error(f"Driver launch error: {e}")
        send_msg(f"❌ Browser launch error:\n{str(e)[:200]}")

    finally:
        if driver:
            _safe_quit(driver)
        session.running = False
        if session._stop_event.is_set():
            send_msg(f"✅ Stopped. Total loops completed: {session.loop_count}")
        elif session.loop_count > 0:
            send_msg(
                f"⚠️ Loop band hua.\n"
                f"Loops done: {session.loop_count} | Errors: {session.error_count}\n"
                f"Dobara shuru karne ke liye /run bhejein."
            )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    # Attach premium inline menu so 📖 guide + categories are one tap away
    try:
        kb = _menu_keyboard(uid)
    except Exception:
        kb = None
    await update.message.reply_text(_help_for(uid), parse_mode="Markdown", reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    # Attach the same premium inline menu for quick navigation
    try:
        kb = _menu_keyboard(uid)
    except Exception:
        kb = None
    await update.message.reply_text(_help_for(uid), parse_mode="Markdown", reply_markup=kb)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = time.time()
    msg = await update.message.reply_text("🏓 Pong!")
    ms = int((time.time() - t0) * 1000)
    await msg.edit_text(f"🏓 Pong!\n⚡ Response: {ms}ms")


async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import subprocess, shutil
    lines = ["🔧 *BROWSER DIAGNOSTICS*\n"]

    # ── Active pair ───────────────────────────────────────────────
    cd_valid  = _is_valid_chromedriver(CHROMEDRIVER_PATH)
    ch_exists = bool(CHROMIUM_PATH and os.path.isfile(CHROMIUM_PATH))

    cd_icon = "✅" if cd_valid  else "❌"
    ch_icon = "✅" if ch_exists else "❌"

    lines.append(f"{cd_icon} *ChromeDriver (active):*\n`{CHROMEDRIVER_PATH or 'NOT FOUND'}`")
    if cd_valid:
        try:
            ver = subprocess.check_output([CHROMEDRIVER_PATH, "--version"],
                stderr=subprocess.STDOUT, timeout=5).decode(errors="ignore").strip()
            lines.append(f"   `{ver}`")
        except Exception:
            pass

    lines.append(f"\n{ch_icon} *Chromium (active):*\n`{CHROMIUM_PATH or 'NOT FOUND'}`")
    if ch_exists:
        try:
            ver = subprocess.check_output([CHROMIUM_PATH, "--version"],
                stderr=subprocess.STDOUT, timeout=5).decode(errors="ignore").strip()
            lines.append(f"   `{ver}`")
        except Exception:
            pass

    # snap flag — check file content, not just path name
    def _diag_is_snap(p: str) -> bool:
        if not p:
            return False
        if "snap" in p:
            return True
        try:
            with open(p, "rb") as f:
                return "snap" in f.read(512).decode(errors="ignore").lower()
        except Exception:
            return False

    is_snap = _diag_is_snap(CHROMIUM_PATH)
    cd_snap  = _diag_is_snap(CHROMEDRIVER_PATH)
    pair_ok  = (is_snap == cd_snap) or (not cd_snap)  # apt chromedriver + snap chromium = OK
    pair_icon = "✅" if pair_ok else "⚠️"
    if is_snap and cd_snap:
        pair_note = "snap+snap (matched)"
    elif not is_snap and not cd_snap:
        pair_note = "apt+apt (matched)"
    elif not cd_snap and is_snap:
        pair_note = "apt chromedriver + snap chromium ✅ (proven working combo)"
    else:
        pair_note = "snap chromedriver + apt chromium ⚠️ (may fail on rebuild)"
    lines.append(f"\n{pair_icon} *Pair:* {pair_note}")

    # ── PATH scan ─────────────────────────────────────────────────
    lines.append("\n🔍 *Available on PATH:*")
    for name in ("chromedriver", "chromium.chromedriver", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            lines.append(f"  `{name}` → `{found}`")

    # ── Env var override status ───────────────────────────────────
    raw_ch = os.environ.get("CHROMIUM_PATH", "")
    raw_cd = os.environ.get("CHROMEDRIVER_PATH", "")
    if raw_ch or raw_cd:
        lines.append("\n⚙️ *Systemd Env Vars (set hain):*")
        lines.append(f"  `CHROMIUM_PATH` = `{raw_ch or '(not set)'}`")
        lines.append(f"  `CHROMEDRIVER_PATH` = `{raw_cd or '(not set)'}`")
        if raw_ch and "snap" not in raw_ch:
            # Check if chromium-browser is snap wrapper
            snap_w = False
            try:
                with open(raw_ch, "rb") as f:
                    snap_w = "snap" in f.read(512).decode(errors="ignore").lower()
            except Exception:
                pass
            if snap_w:
                lines.append("  ⚠️ `CHROMIUM_PATH` is snap wrapper — env vars ignored, snap pair used")
        if (raw_ch and "snap" in raw_ch) or (raw_cd and "snap" in raw_cd):
            lines.append("  ⚠️ Snap in env vars — env vars ignored, snap pair used instead")

    # ── Snap warning ─────────────────────────────────────────────
    def _diag_is_snap2(p: str) -> bool:
        if not p: return False
        if "snap" in p: return True
        try:
            with open(p, "rb") as f:
                return "snap" in f.read(512).decode(errors="ignore").lower()
        except Exception:
            return False

    using_snap_cd = _diag_is_snap2(CHROMEDRIVER_PATH)
    if using_snap_cd:
        lines.append(
            "\n❌ *SNAP ChromeDriver detect hua — systemd mein kaam nahi karta!*\n"
            "Yahi 'Status code was: 1' error ki wajah hai.\n\n"
            "✅ *Fix — Google Chrome install karo (non-snap):*\n"
            "```\n"
            "wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb\n"
            "sudo apt install -y ./google-chrome-stable_current_amd64.deb\n"
            "```\n"
            "Phir chromedriver install karo:\n"
            "```\n"
            "CHROME_VER=$(google-chrome-stable --version | grep -oP '\\d+' | head -1)\n"
            "wget -q \"https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VER}.0.0.0/linux64/chromedriver-linux64.zip\" -O /tmp/cd.zip\n"
            "sudo unzip -o /tmp/cd.zip chromedriver-linux64/chromedriver -d /tmp/\n"
            "sudo mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver\n"
            "sudo chmod +x /usr/local/bin/chromedriver\n"
            "chromedriver --version\n"
            "```\n"
            "Phir restart:\n"
            "`sudo systemctl restart visitor-bot`"
        )
    elif not cd_valid or not ch_exists or not pair_ok:
        lines.append(
            "\n⚠️ *Fix — systemd mein env vars set karo:*\n"
            "```\nsudo systemctl edit visitor-bot\n\n"
            "[Service]\n"
            "Environment=CHROMIUM_PATH=/usr/bin/google-chrome-stable\n"
            "Environment=CHROMEDRIVER_PATH=/usr/local/bin/chromedriver\n```\n"
            "Phir:\n"
            "`sudo systemctl daemon-reload && sudo systemctl restart visitor-bot`"
        )
    else:
        lines.append("\n✅ *Sab theek hai! Browser launch hona chahiye.*")

    # ── ChromeDriver log tail ────────────────────────────────────
    try:
        with open("/tmp/chromedriver-bot.log", "r", errors="replace") as _f:
            _tail = "".join(_f.readlines()[-15:]).strip()
        if _tail:
            lines.append(f"\n📋 *ChromeDriver log (last 15 lines):*\n```\n{_tail[:1200]}\n```")
        else:
            lines.append("\n📋 *ChromeDriver log:* (empty — bot abhi tak launch nahi hua)")
    except FileNotFoundError:
        lines.append("\n📋 *ChromeDriver log:* (missing — /run se pehle /diag nahi chala)")
    except Exception as _e:
        lines.append(f"\n📋 *ChromeDriver log error:* `{_e}`")

    # ── Tor log tail ─────────────────────────────────────────────
    try:
        with open("/tmp/tor-bot.log", "r", errors="replace") as _f:
            _ttail = "".join(_f.readlines()[-8:]).strip()
        if _ttail:
            lines.append(f"\n🧅 *Tor log (last 8 lines):*\n```\n{_ttail[:600]}\n```")
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # ── Version match check ──────────────────────────────────────
    cd_ver = _get_major_version(CHROMEDRIVER_PATH)
    ch_ver = _get_major_version(CHROMIUM_PATH) if CHROMIUM_PATH else None
    if cd_ver and ch_ver:
        ver_diff = abs(cd_ver - ch_ver)
        ver_icon = "✅" if ver_diff <= 2 else "❌"
        lines.append(
            f"\n{ver_icon} *Version Check:* ChromeDriver={cd_ver} | Chromium={ch_ver}"
        )
        if ver_diff > 2:
            lines.append(
                "  ⚠️ VERSION MISMATCH — yahi 'Status code 1' error ki wajah hai!\n"
                "  Fix:\n"
                "  `sudo apt update && sudo apt install --reinstall chromium-browser chromium-chromedriver`\n"
                "  `sudo systemctl restart visitor-bot`"
            )
    elif cd_ver:
        lines.append(f"\nℹ️ *ChromeDriver version:* {cd_ver} (Chromium version unreadable)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Last N lines of bot log file ko as document send karo."""
    import io
    uid = update.effective_user.id
    lines_to_send = 300

    # context se custom line count lo
    if context.args:
        try:
            lines_to_send = max(50, min(2000, int(context.args[0])))
        except ValueError:
            pass

    log_path = _LOG_FILE

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        await update.message.reply_text(
            "⚠️ Log file abhi tak nahi bani. Bot shuru hone ke baad `/run` chalao phir try karo."
        )
        return
    except Exception as e:
        await update.message.reply_text(f"❌ Log file read error: `{e}`", parse_mode="Markdown")
        return

    tail = all_lines[-lines_to_send:]
    total_lines = len(all_lines)
    content = "".join(tail)

    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = "bot-logs.txt"

    caption = (
        f"📋 *Bot Logs*\n"
        f"Showing last {len(tail)} of {total_lines} lines\n"
        f"File: `{log_path}`\n"
        f"_Yeh file share karo error fix karne ke liye_"
    )

    await update.message.reply_document(
        document=buf,
        filename="bot-logs.txt",
        caption=caption,
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import psutil, datetime
    uid = update.effective_user.id
    session = _sessions.get(uid)

    lines = ["📊 *Session Stats*\n"]
    if session and session.start_time:
        elapsed = int(time.time() - session.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h:02d}:{m:02d}:{s:02d}"
        lines += [
            f"🔄 Loops done   : `{session.loop_count}`",
            f"❌ Errors       : `{session.error_count}`",
            f"🌐 Last IP      : `{session.last_ip or 'N/A'}`",
            f"📍 Status       : `{session.last_status}`",
            f"⏱ Session time : `{uptime_str}`",
            f"🔁 Running      : `{'Yes' if session.running else 'No'}`",
            "",
        ]
    else:
        lines += ["_Koi active session nahi_\n"]

    try:
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")
        boot = datetime.datetime.fromtimestamp(psutil.boot_time())
        up_since = boot.strftime("%d %b, %I:%M %p")
        if swap.total > 0:
            swap_line = f"Swap   : `{swap.percent}%` ({swap.used // 1024 // 1024}MB / {swap.total // 1024 // 1024}MB)"
        else:
            swap_line = "Swap   : `none`"
        lines += [
            "🖥 *System Info*\n",
            f"CPU    : `{cpu}%`",
            f"RAM    : `{ram.percent}%` ({ram.used // 1024 // 1024}MB / {ram.total // 1024 // 1024}MB)",
            swap_line,
            f"Disk   : `{disk.percent}%` used",
            f"Up since: `{up_since}`",
        ]
    except Exception:
        lines.append("_(System info unavailable)_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_set_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(f"Usage: /set_url <url>\nCurrent: {session.url or 'Not set'}")
        return
    url = context.args[0].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    session.url = url
    _save_session(update.effective_user.id, session)
    _audit(update.effective_user.id, "set_url", url[:120])
    await update.message.reply_text(f"URL set: {url}")


async def cmd_add_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import re
    session = get_session(update.effective_user.id)

    raw_text = update.message.text or ""
    # Strip the command prefix (/addpxy or /addpxy@botname)
    body = re.sub(r'^/\S+\s*', '', raw_text, count=1).strip()

    # If user replied to a message, also pull text from that message
    reply = update.message.reply_to_message
    if reply:
        reply_text = reply.text or reply.caption or ""
        if reply_text.strip():
            body = (body + "\n" + reply_text).strip()

    if not body:
        await update.message.reply_text(
            "Usage: /addpxy <proxies>\n\n"
            "Accepts any format — space, newline, or comma separated:\n\n"
            "HTTP:\n"
            "  host:port\n"
            "  host:port:User:Pass\n"
            "  User:Pass@host:port\n"
            "  http://User:Pass@host:port\n\n"
            "SOCKS4/SOCKS5:\n"
            "  socks5://host:port\n"
            "  socks4://host:port\n"
            "  socks5://User:Pass@host:port\n"
            "  socks4://User:Pass@host:port\n\n"
            "Optional suffixes ignored: {Notes} [RefreshURL]\n\n"
            "Example (list):\n"
            "/addpxy\n"
            "user1:pass1@host1.com:8080\n"
            "user2:pass2@host2.com:8080\n"
            "host3.com:3128:user3:pass3"
        )
        return

    # Split by newlines, commas, semicolons, or spaces — any combo
    tokens = re.split(r'[\n\r,;\s]+', body)

    MAX_PROXIES = 500
    added = []
    invalid = []
    duplicates = []
    limit_reached = False

    for token in tokens:
        proxy = token.strip()
        if not proxy:
            continue
        if _parse_proxy_parts(proxy) is None:
            invalid.append(proxy)
        elif proxy in session.proxies:
            duplicates.append(proxy)
        elif len(session.proxies) >= MAX_PROXIES:
            limit_reached = True
        else:
            session.proxies.append(proxy)
            added.append(proxy)

    if added:
        _save_session(update.effective_user.id, session)
        _audit(update.effective_user.id, "add_proxy", f"added={len(added)} dup={len(duplicates)} invalid={len(invalid)}")

    lines = []
    for p in added:
        display = p.split("@")[-1] if "@" in p else p
        lines.append(f"Added: {display}")
    for p in duplicates:
        display = p.split("@")[-1] if "@" in p else p
        lines.append(f"Duplicate (skipped): {display}")
    for p in invalid:
        lines.append(f"Invalid (skipped): {p[:40]}")
    if limit_reached:
        lines.append(f"⚠️ Limit ({MAX_PROXIES}) full — kuch proxies skip hue. /clrp se hatayen pehle.")

    lines.append(f"\nTotal proxies: {len(session.proxies)}")
    await update.message.reply_text("\n".join(lines))


async def cmd_list_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not session.proxies:
        await update.message.reply_text("Koi proxy nahi hai. /addpxy se add karo.")
        return
    lines = [f"{i}. {p.split('@')[-1] if '@' in p else p}" for i, p in enumerate(session.proxies, 1)]
    await update.message.reply_text("Proxies:\n" + "\n".join(lines))


async def cmd_clear_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    count = len(session.proxies)
    session.proxies.clear()
    _save_session(update.effective_user.id, session)
    _audit(update.effective_user.id, "clear_proxies", f"removed={count}")
    await update.message.reply_text(f"{count} proxies hata diye gaye.")


async def cmd_tor_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    session = get_session(update.effective_user.id)
    if session.running:
        await update.message.reply_text("Pehle /stop karo, phir Tor mode change karo.")
        return

    await update.message.reply_text(
        "Tor mode ON kar raha hai...\n"
        "Tor Network se connect ho raha hai, 30-45 second lag sakte hain..."
    )

    loop = asyncio.get_running_loop()

    uid = update.effective_user.id

    def _start_tor():
        ok = tor_manager.start()
        if ok:
            session.tor_mode = True
            _save_session(uid, session)   # persist tor_mode=True across bot restarts
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(
                    "Tor Mode: ON\n"
                    "Har loop ke baad New Identity (new IP) milega.\n"
                    "Proxies ignore honge — Tor use hoga.\n\n"
                    "Ab /run karo!"
                ),
                loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(
                    "Tor start FAILED.\n"
                    "Tor Mode OFF hai, normal proxies use honge."
                ),
                loop
            )

    threading.Thread(target=_start_tor, daemon=True).start()


async def cmd_tor_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if session.running:
        await update.message.reply_text("Pehle /stop karo, phir Tor mode change karo.")
        return
    session.tor_mode = False
    _save_session(update.effective_user.id, session)   # persist tor_mode=False
    tor_manager.stop()
    await update.message.reply_text(
        "Tor Mode: OFF\n"
        "Ab normal proxies use honge."
    )


async def cmd_identity_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session.identity_mode = True
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(
        "New Identity: ON (Ctrl+Shift+U)\n"
        "Har loop ke baad NEWNYM signal — sab circuits replace, full IP change (~10s wait).\n"
        "Tor ON hona zaroori hai /tor_on"
    )


async def cmd_identity_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session.identity_mode = False
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(
        "New Identity: OFF\n"
        "Ab loop ke baad identity change nahi hogi."
    )


async def cmd_circuit_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session.circuit_mode = True
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(
        "New Circuit: ON (Ctrl+Shift+L)\n"
        "Har loop ke baad active circuits close — sirf routing change, faster (~3s wait).\n"
        "Note: Identity ON ho to Identity prefer hoti hai. Dono off karo circuit-only ke liye.\n"
        "Tor ON hona zaroori hai /tor_on"
    )


async def cmd_circuit_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session.circuit_mode = False
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(
        "New Circuit: OFF\n"
        "Ab loop ke baad circuit change nahi hoga."
    )


async def cmd_check_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    session = get_session(update.effective_user.id)

    # Collect proxies from args OR replied message OR session
    raw_text = " ".join(context.args) if context.args else ""
    reply = update.message.reply_to_message
    if reply:
        raw_text = (raw_text + "\n" + (reply.text or reply.caption or "")).strip()

    if raw_text.strip():
        import re as _re
        tokens = _re.split(r'[\n\r,;\s]+', raw_text.strip())
        proxies_to_check = [t for t in tokens if t.strip()]
    else:
        proxies_to_check = list(session.proxies)

    if not proxies_to_check:
        await update.message.reply_text(
            "Koi proxy nahi hai check karne ke liye.\n\n"
            "Usage:\n"
            "  /chkpxy                   — Sab saved proxies check karo\n"
            "  /chkpxy host:port         — Specific proxy check karo\n"
            "  (reply to proxy list msg) — Replied message se check karo"
        )
        return

    total = len(proxies_to_check)
    main_loop = asyncio.get_running_loop()
    WORKERS = min(20, total)

    def _send(text):
        fut = asyncio.run_coroutine_threadsafe(
            update.message.reply_text(text), main_loop
        )
        try:
            return fut.result(timeout=15)
        except Exception:
            return None

    def _edit(msg_id, text):
        if not msg_id:
            return
        asyncio.run_coroutine_threadsafe(
            context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_id,
                text=text,
            ),
            main_loop,
        )

    def _check_timed(proxy_raw: str):
        t0 = _time.time()
        res = _check_single_proxy(proxy_raw)
        elapsed = round(_time.time() - t0, 2)
        return proxy_raw, res, elapsed

    def _run_checks():
        # --- Header message: edited live with counter ---
        header_msg = _send(f"🔄 0/{total} | ✅ 0 Live | ❌ 0 Dead")
        header_id = header_msg.message_id if header_msg else None

        live_proxies = []   # raw host:port strings for final code block
        checked = 0
        dead_count = 0
        lock = threading.Lock()

        # Rolling result buffer — flushed when full
        buf_lines = []
        buf_id = [None]     # message_id of current result-batch message

        def _flush_buf():
            """Send or edit the current buffer as a Telegram message."""
            if not buf_lines:
                return
            text = "\n".join(buf_lines)
            if buf_id[0]:
                _edit(buf_id[0], text)
            else:
                msg = _send(text)
                if msg:
                    buf_id[0] = msg.message_id

        def _append_line(line: str):
            """Add line to buffer; if buffer would exceed 3800 chars, flush first."""
            current = "\n".join(buf_lines)
            if buf_lines and len(current) + len(line) + 1 > 3800:
                _flush_buf()
                buf_lines.clear()
                buf_id[0] = None
            buf_lines.append(line)
            # Update the buffer message every line so it looks live
            _flush_buf()

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_check_timed, p): p for p in proxies_to_check}
            for fut in as_completed(futs):
                proxy_raw, res, elapsed = fut.result()
                display = proxy_raw.split("@")[-1] if "@" in proxy_raw else proxy_raw
                display = display.strip()

                with lock:
                    checked += 1
                    if res["status"] == "LIVE":
                        live_proxies.append(display)
                        line = f"✅ {display} | ({elapsed}s)"
                    else:
                        dead_count += 1
                        err = (res.get("error") or "timeout")[:60]
                        line = f"❌ {display} | {err}"

                    _append_line(line)
                    # Update header counter
                    icon = "🔄" if checked < total else "✅"
                    _edit(header_id,
                        f"{icon} {checked}/{total} | ✅ {len(live_proxies)} Live | ❌ {dead_count} Dead"
                    )

        # Flush any remaining lines
        with lock:
            _flush_buf()

        # Final header
        _edit(header_id,
            f"✅ {total}/{total} | ✅ {len(live_proxies)} Live | ❌ {total - len(live_proxies)} Dead"
        )

        # Send LIVE proxies in a copyable code block
        if live_proxies:
            header_line = f"✅ LIVE PROXIES:\n"
            block_lines = live_proxies[:]
            # Split into chunks that fit in 4096 chars
            chunk_header = header_line + "```\n"
            chunk = chunk_header
            for p in block_lines:
                entry = p + "\n"
                if len(chunk) + len(entry) + 4 > 4000:
                    _send(chunk + "```")
                    chunk = "```\n"
                chunk += entry
            _send(chunk + "```")
        else:
            _send("❌ Koi LIVE proxy nahi mili.")

    threading.Thread(target=_run_checks, daemon=True).start()


async def cmd_set_selector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            f"Usage: /set_selector <css_selector>\n"
            f"Example: /set_selector button.vote-btn\n"
            f"Current: {session.primary_selector or 'None'}"
        )
        return
    session.primary_selector = " ".join(context.args).strip()
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(f"Primary selector set: {session.primary_selector}")


async def cmd_set_selector2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            f"Usage: /set_selector2 <css_selector>\n"
            f"Current: {session.secondary_selector or 'None'}"
        )
        return
    session.secondary_selector = " ".join(context.args).strip()
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(f"Secondary selector set: {session.secondary_selector}")


async def cmd_set_click_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            f"Usage: /set_click_text <button text>\n"
            f"Example: /set_click_text Sign Up\n"
            f"Current: {session.click_text or 'None'}\n\n"
            f"Bot page pe us text wala button dhundh ke click karega."
        )
        return
    session.click_text = " ".join(context.args).strip()
    session.primary_selector = ""
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(
        f"Click text set: '{session.click_text}'\n"
        f"Bot '{session.click_text}' button ko click karega."
    )


async def cmd_set_click_text2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            f"Usage: /set_click_text2 <button text>\n"
            f"Example: /set_click_text2 Continue\n"
            f"Current: {session.click_text2 or 'None'}"
        )
        return
    session.click_text2 = " ".join(context.args).strip()
    session.secondary_selector = ""
    _save_session(update.effective_user.id, session)
    await update.message.reply_text(
        f"Click text 2 set: '{session.click_text2}'"
    )


async def cmd_set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(f"Usage: /set_delay <seconds>\nCurrent: {session.delay}s")
        return
    try:
        val = float(context.args[0])
        if not (0 <= val <= 3600):
            raise ValueError
        session.delay = val
        _save_session(update.effective_user.id, session)
        await update.message.reply_text(f"Delay set: {val}s")
    except ValueError:
        await update.message.reply_text("Invalid value. 0 se 3600 ke beech number daalein (e.g. 5 ya 2.5)")


async def cmd_set_wait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(f"Usage: /set_wait <seconds>\nCurrent: {session.page_load_wait}s")
        return
    try:
        val = float(context.args[0])
        if not (0 <= val <= 300):
            raise ValueError
        session.page_load_wait = val
        _save_session(update.effective_user.id, session)
        await update.message.reply_text(f"Page load wait set: {val}s")
    except ValueError:
        await update.message.reply_text("Invalid value. 0 se 300 ke beech number daalein (e.g. 5)")


async def cmd_set_loops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        current = "infinite" if session.loops == 0 else str(session.loops)
        await update.message.reply_text(f"Usage: /set_loops <n>\nCurrent: {current}\n(0 = infinite)")
        return
    try:
        val = int(context.args[0])
        if not (0 <= val <= 100000):
            raise ValueError
        session.loops = val
        _save_session(update.effective_user.id, session)
        await update.message.reply_text(f"Loops set: {'infinite ♾' if val == 0 else val}")
    except ValueError:
        await update.message.reply_text("Invalid value. 0 (infinite) ya 1-100000 ke beech integer daalein")


async def cmd_set_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(f"Usage: /set_timeout <seconds>\nCurrent: {session.timeout}s")
        return
    try:
        val = int(context.args[0])
        if not (5 <= val <= 300):
            raise ValueError
        session.timeout = val
        _save_session(update.effective_user.id, session)
        await update.message.reply_text(f"Timeout set: {val}s")
    except ValueError:
        await update.message.reply_text("Invalid value. 5 se 300 ke beech integer daalein (e.g. 30)")


async def cmd_bint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set break interval — pause after every N loops (0 = disabled)."""
    uid = update.effective_user.id
    session = get_session(uid)
    bint = session.break_interval
    bdur = session.break_duration
    if not context.args:
        status = f"OFF (0)" if bint == 0 else f"har {bint} loops ke baad {bdur}s break"
        await update.message.reply_text(
            f"Break interval: *{status}*\n\n"
            f"Usage: `/bint <loops>`\n"
            f"Example: `/bint 50` → har 50 loops ke baad pause\n"
            f"`/bint 0` → break feature band karo",
            parse_mode="Markdown",
        )
        return
    try:
        val = int(context.args[0])
        if not (0 <= val <= 10000):
            raise ValueError
        session.break_interval = val
        _save_session(uid, session)
        if val == 0:
            await update.message.reply_text("⏸ Break feature *band* kar diya.", parse_mode="Markdown")
        else:
            await update.message.reply_text(
                f"✅ Break interval set: har *{val} loops* ke baad *{bdur}s* pause.\n"
                f"(Break duration change karne ke liye: `/bwait <seconds>`)",
                parse_mode="Markdown",
            )
    except ValueError:
        await update.message.reply_text("Invalid value. 0 (off) ya 1-10000 ke beech number daalein.")


async def cmd_bwait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set break duration — how long to pause at each break."""
    uid = update.effective_user.id
    session = get_session(uid)
    if not context.args:
        await update.message.reply_text(
            f"Break duration: *{session.break_duration}s*\n\n"
            f"Usage: `/bwait <seconds>`\n"
            f"Example: `/bwait 120` → 2 minute ka break",
            parse_mode="Markdown",
        )
        return
    try:
        val = int(context.args[0])
        if not (10 <= val <= 86400):
            raise ValueError
        session.break_duration = val
        _save_session(uid, session)
        bint = session.break_interval
        status = f"har {bint} loops ke baad" if bint > 0 else "(break interval abhi OFF hai — /bint se on karo)"
        await update.message.reply_text(
            f"✅ Break duration set: *{val}s* pause {status}.",
            parse_mode="Markdown",
        )
    except ValueError:
        await update.message.reply_text("Invalid value. 10 se 86400 ke beech seconds daalein (e.g. 60 ya 120)")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio

    session = get_session(update.effective_user.id)

    if session.running:
        await update.message.reply_text("Bot pehle se chal raha hai. /stop karo pehle.")
        return
    if not session.url:
        await update.message.reply_text("Pehle URL set karo: /set_url <url>")
        return

    session._stop_event.clear()
    session.running = True
    _audit(
        update.effective_user.id,
        "run_start",
        f"url={session.url[:80]} mode={'tor' if session.tor_mode else 'proxy'} loops={session.loops}",
    )

    chat_id = update.effective_chat.id
    main_loop = asyncio.get_running_loop()

    def send_msg(text: str):
        try:
            future = asyncio.run_coroutine_threadsafe(
                context.bot.send_message(chat_id=chat_id, text=text),
                main_loop,
            )
            msg = future.result(timeout=15)
            return msg.message_id
        except Exception as ex:
            logger.error(f"send_msg error: {ex}")
            return None

    _edit_last_text: dict[int, str] = {}   # msg_id → last sent text (debounce)
    _edit_last_time: dict[int, float] = {}  # msg_id → last send time
    _EDIT_MIN_GAP = 2.5                    # minimum seconds between edits (Telegram rate limit safe)

    def edit_msg(msg_id, text: str):
        """Non-blocking fire-and-forget edit with debounce — does NOT slow down the main loop."""
        if not msg_id:
            return
        now = time.time()
        # Debounce: skip if same text or too soon since last edit
        if _edit_last_text.get(msg_id) == text:
            return
        if now - _edit_last_time.get(msg_id, 0) < _EDIT_MIN_GAP:
            return
        _edit_last_text[msg_id] = text
        _edit_last_time[msg_id] = now

        def _do_edit():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=text,
                    ),
                    main_loop,
                )
                future.result(timeout=10)
            except Exception as ex:
                logger.debug(f"edit_msg error (non-critical): {ex}")

        threading.Thread(target=_do_edit, daemon=True).start()

    def _launch():
        if session.tor_mode:
            # ── Auto-start Tor if not running (bot restart ke baad) ──────
            if not tor_manager.is_running():
                send_msg(
                    "Tor chal nahi raha — auto-start ho raha hai...\n"
                    "30-45 second wait karein, phir visit shuru hoga."
                )
                ok = tor_manager.start()
                if not ok:
                    send_msg(
                        "Tor start FAIL hua!\n"
                        "Options:\n"
                        "1. /ton dobara karo\n"
                        "2. Ya /toff karo aur proxy mode use karo"
                    )
                    session.running = False
                    return
                send_msg("Tor ready! Browser shuru ho raha hai...")
            else:
                send_msg("Browser launch ho raha hai (10-15 second)...")
            _run_loop(session, send_msg, edit_msg)
            return

        scrape_id = send_msg(
            "Proxy scrape ho rahi hai...\n"
            "Public sources se live proxies dhundh raha hai, please wait..."
        )

        def _progress(scraped, checked, live, msg):
            edit_msg(scrape_id,
                f"Proxy Scraper chal raha hai...\n"
                f"Scraped: {scraped} | Checked: {checked} | Live: {live}\n"
                f"{msg}"
            )

        try:
            live_proxies = proxy_scraper.scrape_and_check(
                max_check=300,
                max_live=30,
                workers=50,
                progress_cb=_progress,
            )
        except Exception as e:
            live_proxies = []
            logger.error(f"Proxy scrape error: {e}")

        if live_proxies:
            session.proxies = live_proxies
            _save_session(update.effective_user.id, session)
            edit_msg(scrape_id,
                f"Proxy Scrape DONE!\n"
                f"Live proxies mili: {len(live_proxies)}\n"
                f"Ab browser shuru ho raha hai..."
            )
        else:
            edit_msg(scrape_id,
                "Koi live proxy nahi mili!\n"
                "Direct connection se try karta hai..."
            )

        if session._stop_event.is_set():
            session.running = False
            return

        send_msg("Shuru ho raha hai! Browser launch hone mein 10-15 second lagte hain...")
        _run_loop(session, send_msg, edit_msg)

    thread = threading.Thread(target=_launch, daemon=True)
    session._thread = thread
    thread.start()

    # ── Saved settings summary — user ko confirm karne mein madad ──────
    uid = update.effective_user.id
    mode_line = "Mode: Tor" if session.tor_mode else "Mode: Proxy Scraper"
    rot_line  = ""
    if session.tor_mode:
        if session.identity_mode:
            rot_line = "Rotation: New Identity (full IP change)"
        elif session.circuit_mode:
            rot_line = "Rotation: New Circuit (fast)"
        else:
            rot_line = "Rotation: None"
    loops_line = f"Loops: {'Infinite' if session.loops == 0 else session.loops}"
    click_line = ""
    if session.click_text:
        click_line = f"Click 1: {session.click_text[:30]}\n"
    if session.click_text2:
        click_line += f"Click 2: {session.click_text2[:30]}\n"

    start_msg = (
        f"Bot shuru ho raha hai!\n"
        f"URL: {session.url[:50]}\n"
        f"{mode_line}\n"
        f"{rot_line + chr(10) if rot_line else ''}"
        f"{click_line}"
        f"{loops_line}\n"
        f"Delay: {session.delay}s | Timeout: {session.timeout}s\n"
        f"(Ye settings pichli baar se saved hain — change karne ke liye commands use karein)"
    ) if session.tor_mode else (
        f"Auto Proxy Scraper + Visitor shuru ho raha hai!\n"
        f"URL: {session.url[:50]}\n"
        f"{click_line}"
        f"{loops_line} | Delay: {session.delay}s\n"
        f"Pehle live proxies scrape hongi, phir visit shuru hoga..."
    )

    await update.message.reply_text(start_msg)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not session.running:
        await update.message.reply_text("Abhi kuch chal nahi raha.")
        return
    session._stop_event.set()
    _audit(update.effective_user.id, "stop", f"loops_done={session.loop_count}")
    await update.message.reply_text("Stop signal bhej diya. Current loop khatam hone ke baad rukega.")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    session = get_session(update.effective_user.id)

    if not session.url:
        await update.message.reply_text("URL set nahi hai. Pehle /url <link> se set karo.")
        return

    if session.running:
        session._stop_event.set()
        await update.message.reply_text("Pehla session rok raha hai... thoda wait karo.")
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not session.running:
                break

    session._stop_event.clear()
    session.running = False
    await update.message.reply_text("Restart ho raha hai! Ab dobara shuru karta hai...")
    await cmd_run(update, context)


def _progress_bar(used: float, total: float, width: int = 18) -> str:
    """Return a █░ progress bar string with percentage."""
    if total <= 0:
        return f"{'░' * width} N/A"
    pct = min(used / total, 1.0)
    filled = round(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct*100:.1f}%"


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import datetime
    import psutil

    session = get_session(update.effective_user.id)

    # ── Session info ──────────────────────────────────────────────
    state_icon = "🟢" if session.running else "🔴"
    state_text = "Running" if session.running else "Stopped"
    loops_label = "∞" if session.loops == 0 else str(session.loops)
    loops_done  = session.loop_count

    if session.tor_mode:
        tor_running = tor_manager.is_running()
        proxy_mode  = f"Tor {'🟢' if tor_running else '🟡'}"
        if session.identity_mode:
            proxy_mode += " | New Identity"
        elif session.circuit_mode:
            proxy_mode += " | New Circuit"
    else:
        proxy_mode = f"Proxy ({len(session.proxies)} saved)"

    click1 = session.click_text or session.primary_selector or "—"
    click2 = session.click_text2 or session.secondary_selector or "—"

    url_display = session.url if session.url else "Not set"
    if len(url_display) > 35:
        url_display = url_display[:33] + "…"

    now_str = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    # Uptime (since session start)
    if session.running and hasattr(session, "start_time") and session.start_time:
        elapsed = int(time.time() - session.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"
    else:
        uptime_str = "—"

    # ── System resources ──────────────────────────────────────────
    ram  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    ram_used_mb  = ram.used  // (1024 ** 2)
    ram_total_mb = ram.total // (1024 ** 2)
    ram_free_mb  = ram.available // (1024 ** 2)
    ram_bar      = _progress_bar(ram.used, ram.total)

    swap_used_mb  = swap.used  // (1024 ** 2)
    swap_total_mb = swap.total // (1024 ** 2)
    swap_free_mb  = swap_total_mb - swap_used_mb
    swap_bar      = _progress_bar(swap.used, swap.total) if swap.total > 0 else ""

    disk_used_gb  = disk.used  / (1024 ** 3)
    disk_total_gb = disk.total / (1024 ** 3)
    disk_free_gb  = disk.free  / (1024 ** 3)
    disk_bar      = _progress_bar(disk.used, disk.total)

    # ── Build message ─────────────────────────────────────────────
    SEP = "━━━━━━━━━━━━━━━━━━━━"

    msg = (
        f"🤖 *BOT STATUS* 🤖\n"
        f"`{SEP}`\n"
        f"{state_icon} *State:*    {state_text}\n"
        f"⏱ *Uptime:*   {uptime_str}\n"
        f"🔁 *Loops:*    {loops_done} / {loops_label}\n"
        f"🌐 *Proxy:*    {proxy_mode}\n"
        f"🆔 *Last IP:*  {session.last_ip or 'N/A'}\n"
        f"🔗 *URL:*      `{url_display}`\n"
        f"📅 *Time:*     {now_str}\n"
    )

    if click1 != "—" or click2 != "—":
        msg += (
            f"`{SEP}`\n"
            f"🖱 *CLICK CONFIG*\n"
            f"  Click 1: `{click1}`\n"
            f"  Click 2: `{click2}`\n"
            f"  Wait: {session.page_load_wait}s | Delay: {session.delay}s | Timeout: {session.timeout}s\n"
        )

    _bint = session.break_interval
    _bdur = session.break_duration
    if _bint > 0:
        _next_break = _bint - (session.loop_count % _bint) if session.loop_count > 0 else _bint
        msg += (
            f"`{SEP}`\n"
            f"☕ *BREAK*: har {_bint} loops → {_bdur}s pause\n"
            f"  Agla break: {_next_break} loop{'s' if _next_break != 1 else ''} mein\n"
        )

    if session.last_error:
        msg += (
            f"`{SEP}`\n"
            f"⚠️ *Last Error:*\n"
            f"`{session.last_error[:120]}`\n"
        )

    msg += (
        f"`{SEP}`\n"
        f"💾 *MEMORY (RAM)*\n"
        f"`{ram_used_mb}MB / {ram_total_mb}MB {ram_bar}`\n"
        f"Free: {ram_free_mb}MB\n"
    )
    if swap_total_mb > 0:
        msg += (
            f"`{SEP}`\n"
            f"🌀 *SWAP*\n"
            f"`{swap_used_mb}MB / {swap_total_mb}MB {swap_bar}`\n"
            f"Free: {swap_free_mb}MB\n"
        )
    else:
        msg += (
            f"`{SEP}`\n"
            f"🌀 *SWAP:* `none` ⚠️\n"
        )
    msg += (
        f"`{SEP}`\n"
        f"💽 *STORAGE (Disk)*\n"
        f"`{disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB {disk_bar}`\n"
        f"Free: {disk_free_gb:.1f}GB\n"
        f"`{SEP}`\n"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")


async def _notify_all(app, text: str):
    """Send text to OWNER_ID if set, otherwise to every known user."""
    # OWNER_ID is already int | None (parsed at startup)
    targets = [OWNER_ID] if OWNER_ID else db.get_all_users()

    for uid in targets:
        try:
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Status notify failed for {uid}: {e}")


async def _on_startup(app):
    import datetime, time as _t, socket, platform, shutil
    import psutil

    # ── Ping: Telegram API round-trip ─────────────────────────────
    try:
        t0 = _t.perf_counter()
        await app.bot.get_me()
        ping_ms = int((_t.perf_counter() - t0) * 1000)
    except Exception:
        ping_ms = -1
    if ping_ms < 0:
        ping_str = "❌ N/A"
    elif ping_ms < 200:
        ping_str = f"🟢 {ping_ms} ms"
    elif ping_ms < 600:
        ping_str = f"🟡 {ping_ms} ms"
    else:
        ping_str = f"🔴 {ping_ms} ms"

    # ── Server identity ───────────────────────────────────────────
    hostname = socket.gethostname()
    try:
        public_ip = os.popen("curl -s --max-time 3 ifconfig.me").read().strip() or "—"
    except Exception:
        public_ip = "—"

    # ── System resources ──────────────────────────────────────────
    ram  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    cpu  = psutil.cpu_percent(interval=0.4)
    try:
        load1, load5, _ = os.getloadavg()
        load_str = f"{load1:.2f} / {load5:.2f}"
    except Exception:
        load_str = "—"
    boot_secs = int(_t.time() - psutil.boot_time())
    bd, br = divmod(boot_secs, 86400)
    bh, bm = divmod(br // 60, 60)
    server_uptime = (f"{bd}d {bh}h" if bd else f"{bh}h {bm}m")

    ram_used_mb  = ram.used  // (1024 ** 2)
    ram_total_mb = ram.total // (1024 ** 2)
    swap_used_mb  = swap.used  // (1024 ** 2)
    swap_total_mb = swap.total // (1024 ** 2)
    disk_used_gb  = disk.used  / (1024 ** 3)
    disk_total_gb = disk.total / (1024 ** 3)
    ram_bar  = _progress_bar(ram.used,  ram.total)
    swap_bar = _progress_bar(swap.used, swap.total) if swap.total > 0 else ""
    disk_bar = _progress_bar(disk.used, disk.total)

    # ── Components ────────────────────────────────────────────────
    chromium_ok  = "✅" if (CHROMIUM_PATH and shutil.which(CHROMIUM_PATH.split("/")[-1])) or (CHROMIUM_PATH and os.path.exists(CHROMIUM_PATH)) else "❌"
    driver_ok    = "✅" if (CHROMEDRIVER_PATH and os.path.exists(CHROMEDRIVER_PATH)) else "❌"
    try:
        tor_ok = "🟢" if tor_manager.is_running() else "⚪️"
    except Exception:
        tor_ok = "—"

    now = datetime.datetime.now().strftime("%d %b %Y · %I:%M:%S %p")
    SEP = "━━━━━━━━━━━━━━━━━━━━"

    msg = (
        f"🛸⚡ *UAV — ONLINE* ⚡🛸\n"
        f"_by ѕonιc_\n"
        f"`{SEP}`\n"
        f"🟢 *Status:*  Active & Polling\n"
        f"📡 *Ping:*    {ping_str}\n"
        f"🕐 *Booted:*  `{now}`\n"
        f"`{SEP}`\n"
        f"🖥 *SERVER*\n"
        f"  🏷  Host:    `{hostname}`\n"
        f"  🌍  IP:      `{public_ip}`\n"
        f"  🐧  OS:      `{platform.system()} {platform.release().split('-')[0]}`\n"
        f"  ⏱  Uptime:  {server_uptime}\n"
        f"`{SEP}`\n"
        f"⚙️ *RESOURCES*\n"
        f"  🔥 CPU:     `{cpu:5.1f}%   load {load_str}`\n"
        f"  💾 RAM:     `{ram_used_mb}MB / {ram_total_mb}MB {ram_bar}`\n"
        + (f"  🌀 Swap:    `{swap_used_mb}MB / {swap_total_mb}MB {swap_bar}`\n" if swap_total_mb > 0 else "  🌀 Swap:    `none` ⚠️\n") +
        f"  💽 Disk:    `{disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB {disk_bar}`\n"
        f"`{SEP}`\n"
        f"🧩 *COMPONENTS*\n"
        f"  {chromium_ok} Chromium\n"
        f"  {driver_ok} ChromeDriver\n"
        f"  {tor_ok} Tor daemon\n"
        f"`{SEP}`\n"
        f"💡 _Type_ `/help` _for command list_\n"
        f"📊 _Type_ `/s` _for live session status_"
    )
    await _notify_all(app, msg)


async def _on_shutdown(app):
    import datetime
    now = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    await _notify_all(
        app,
        f"🔴 *Bot Offline Ho Gaya!*\n\n"
        f"🕐 Time: `{now}`\n"
        f"⚠️ Bot band ho raha hai — thodi der mein wapas aayega.",
    )


# ── ENH-002: Premium inline-keyboard menu ────────────────────────────────────
# Centralized UI constants & builders — keep callback_data values unchanged.
_UI = {
    "BACK":    "🔙 Back",
    "HOME":    "🏠 Home",
    "CLOSE":   "❌ Close",
    "REFRESH": "🔄 Refresh",
}

_MENU_SECTIONS = {
    "setup": {
        "label":  "⚙️ Setup",
        "title":  "⚙️ SETUP PANEL",
        "tag":    "_Target, clicks aur timing configure karein_",
        "body": (
            "🔗  /url <link>        Target URL set karein\n"
            "🖱  /ct1 <text>        Pehle click ka text\n"
            "🖱  /ct2 <text>        Doosre click ka text\n"
            "🎯  /sel <css>         Primary CSS selector\n"
            "🎯  /sel2 <css>        Secondary CSS selector\n"
            "⏱  /delay <s>         Loop ke baad pause\n"
            "⏳  /wait <s>          Page load wait\n"
            "🔁  /loops <n>         Loops (0 = infinite)\n"
            "⌛  /tout <s>          Element wait timeout\n"
            "☕  /bint <n>          Har N loops pe break\n"
            "💤  /bwait <s>         Break duration"
        ),
    },
    "proxy": {
        "label":  "🌐 Proxies",
        "title":  "🌐 PROXY MANAGER",
        "tag":    "_HTTP / SOCKS proxies add aur verify karein_",
        "body": (
            "➕  /addpxy <list>     Proxies add karein\n"
            "📋  /lp                Saved proxies list\n"
            "🗑  /clrp              Sab proxies hata dein\n"
            "🩺  /chk               Live proxy health check"
        ),
    },
    "tor": {
        "label":  "🧅 Tor",
        "title":  "🧅 TOR NETWORK",
        "tag":    "_Anonymity mode aur identity rotation_",
        "body": (
            "🟢  /ton    🔴  /toff    Tor mode on / off\n"
            "🆔  /idon   ⛔  /idoff   New Identity rotation\n"
            "🔀  /con    ⛔  /coff    New Circuit rotation"
        ),
    },
    "ctrl": {
        "label":  "▶️ Control",
        "title":  "🚀 CONTROL CENTER",
        "tag":    "_Visits start, stop ya restart karein_",
        "body": (
            "🚀  /run               Visit start karein\n"
            "🛑  /stop              Current loop ke baad ruk\n"
            "♻️  /rs                Restart\n"
            "📋  /menu              Yahi menu dobara\n"
            "🔧  /maint on|off      (owner) maintenance mode"
        ),
    },
    "stats": {
        "label":  "📊 Status",
        "title":  "📊 STATUS",
        "tag":    "_Runtime stats aur history_",
        "body": (
            "📡  /s                 Live status panel\n"
            "📈  /stats             Runtime + system stats\n"
            "🏓  /ping              Bot alive check"
        ),
        # Lines visible only to OWNER inside this section
        "owner_extra": (
            "\n📜  /logs [n]          (owner) Recent log lines\n"
            "🛡  /audit [n]         (owner) Audit log"
        ),
    },
    "diag": {
        "label":  "🛠 Diagnostics",
        "title":  "🛠 DIAGNOSTICS",
        "tag":    "_Browser pair, backup aur help_",
        "owner_only": True,   # Whole section hidden from non-owner
        "body": (
            "🔬  /diag              Chromium/driver check\n"
            "💾  /backup            (owner) DB snapshot\n"
            "💡  /help              Full help text"
        ),
    },
    "info": {
        "label":  "💡 Help",
        "title":  "💡 HELP & INFO",
        "tag":    "_Quick references_",
        "body": (
            "💡  /help              Full help text\n"
            "📋  /menu              Yahi menu dobara\n"
            "🆔  /myaccess          Apna role check\n"
            "🎟  /redeem CODE       Premium activate"
        ),
    },
}

_MENU_HEADER = (
    "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
    "   ✨ *UAV CONTROL HUB* ✨\n"
    "╰━━━━━━━━━━━━━━━━━━━━━━╯\n"
    "_Kisi category pe tap karein_"
)


def _btn(label_key: str, cb: str):
    """Tiny factory for repeated nav buttons — keeps style identical everywhere."""
    from telegram import InlineKeyboardButton
    return InlineKeyboardButton(_UI[label_key], callback_data=cb)


def _menu_keyboard(uid: int | None = None):
    """Root menu: sections in grid + How-To Guide CTA + bottom nav row.
    Diagnostics tile only shown to OWNER; non-owner sees Help tile instead."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    s = _MENU_SECTIONS
    is_owner = _is_owner_uid(uid)
    third_tile_key = "diag" if is_owner else "info"
    buttons = [
        [InlineKeyboardButton(s["setup"]["label"], callback_data="menu:setup"),
         InlineKeyboardButton(s["proxy"]["label"], callback_data="menu:proxy")],
        [InlineKeyboardButton(s["tor"]["label"],   callback_data="menu:tor"),
         InlineKeyboardButton(s["ctrl"]["label"],  callback_data="menu:ctrl")],
        [InlineKeyboardButton(s["stats"]["label"], callback_data="menu:stats"),
         InlineKeyboardButton(s[third_tile_key]["label"], callback_data=f"menu:{third_tile_key}")],
        # Full-width primary CTA → opens A-to-Z guide (handled by guide.py)
        [InlineKeyboardButton("📖 How To Use — Full Guide", callback_data="guide:open")],
        [_btn("REFRESH", "menu:back"), _btn("CLOSE", "menu:close")],
    ]
    return InlineKeyboardMarkup(buttons)


def _section_keyboard():
    """Sub-section view: consistent Back + Home row at bottom."""
    from telegram import InlineKeyboardMarkup
    return InlineKeyboardMarkup([[_btn("BACK", "menu:back"), _btn("CLOSE", "menu:close")]])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        _MENU_HEADER,
        reply_markup=_menu_keyboard(uid),
        parse_mode="Markdown",
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatcher for menu:* callbacks. Callback values UNCHANGED for back-compat."""
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()

    # Close → remove the panel cleanly
    if q.data == "menu:close":
        try:
            await q.edit_message_text("✅ _Menu band kar diya._", parse_mode="Markdown")
        except Exception:
            try:
                await q.delete_message()
            except Exception:
                pass
        return

    uid = q.from_user.id if q.from_user else None
    is_owner = _is_owner_uid(uid)

    # Back / Home → root menu
    if q.data == "menu:back":
        try:
            await q.edit_message_text(
                _MENU_HEADER,
                reply_markup=_menu_keyboard(uid),
                parse_mode="Markdown",
            )
        except Exception:
            await q.message.reply_text(
                _MENU_HEADER,
                reply_markup=_menu_keyboard(uid),
                parse_mode="Markdown",
            )
        return

    if not q.data.startswith("menu:"):
        return
    key = q.data.split(":", 1)[1]
    section = _MENU_SECTIONS.get(key)
    if not section:
        return

    # Owner-only sections (e.g., diag) — block non-owner from URL-spoofed callback
    if section.get("owner_only") and not is_owner:
        try:
            await q.answer("Sirf owner ke liye.", show_alert=True)
        except Exception:
            pass
        return

    body = section["body"]
    if is_owner and section.get("owner_extra"):
        body = body + section["owner_extra"]

    text = (
        f"╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        f"   ✦ *{section['title']}* ✦\n"
        f"╰━━━━━━━━━━━━━━━━━━━━━━╯\n"
        f"{section['tag']}\n\n"
        f"```\n{body}\n```"
    )
    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=_section_keyboard())
    except Exception:
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=_section_keyboard())


# ── ENH-003: SQLite backup ───────────────────────────────────────────────────
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if OWNER_ID is None or uid != OWNER_ID:
        await update.message.reply_text("⛔ Sirf owner is command ko use kar sakta hai.")
        return
    import gzip, shutil as _sh, tempfile
    src = db.DB_PATH if hasattr(db, "DB_PATH") else "bot_data.db"
    if not os.path.isfile(src):
        await update.message.reply_text(f"DB file nahi mili: {src}")
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = f"/tmp/bot_backup_{ts}.db.gz"
    try:
        # Copy first to avoid locking issues, then gzip
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp_path = tmp.name
        _sh.copy2(src, tmp_path)
        with open(tmp_path, "rb") as fin, gzip.open(out, "wb", compresslevel=6) as fout:
            _sh.copyfileobj(fin, fout)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        size_kb = os.path.getsize(out) / 1024
        with open(out, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=os.path.basename(out),
                caption=f"📦 Backup ({size_kb:.1f} KB) — {ts}",
            )
        _audit(uid, "backup", f"file={os.path.basename(out)} size={size_kb:.1f}KB")
    except Exception as ex:
        logger.error(f"Backup failed: {ex}")
        await update.message.reply_text(f"❌ Backup fail hua: {ex}")


# ── ENH-004: Maintenance toggle ──────────────────────────────────────────────
async def cmd_maint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _MAINT_MODE
    uid = update.effective_user.id
    if OWNER_ID is None or uid != OWNER_ID:
        await update.message.reply_text("⛔ Sirf owner is command ko use kar sakta hai.")
        return
    if not context.args:
        state = "ON 🔧" if _MAINT_MODE else "OFF ✅"
        await update.message.reply_text(
            f"Maintenance mode: *{state}*\nUsage: `/maint on` ya `/maint off`",
            parse_mode="Markdown",
        )
        return
    val = context.args[0].lower()
    if val in ("on", "1", "true", "yes"):
        _MAINT_MODE = True
        _audit(uid, "maint_on")
        await update.message.reply_text("🔧 Maintenance mode *ON* — non-owner commands block hain.\n"
                                        "(Allowed: /s, /ping, /menu, /maint)", parse_mode="Markdown")
    elif val in ("off", "0", "false", "no"):
        _MAINT_MODE = False
        _audit(uid, "maint_off")
        await update.message.reply_text("✅ Maintenance mode *OFF* — sab commands available.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Invalid. Use: `/maint on` ya `/maint off`", parse_mode="Markdown")


# ── ENH-005: Audit log viewer ────────────────────────────────────────────────
async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if OWNER_ID is None or uid != OWNER_ID:
        await update.message.reply_text("⛔ Sirf owner is command ko use kar sakta hai.")
        return
    n = 30
    if context.args:
        try:
            n = max(1, min(500, int(context.args[0])))
        except ValueError:
            await update.message.reply_text("Usage: `/audit [n]`  (1-500, default 30)", parse_mode="Markdown")
            return
    if not os.path.isfile(_AUDIT_FILE):
        await update.message.reply_text("Audit log abhi khali hai.")
        return
    try:
        with open(_AUDIT_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        tail = lines[-n:]
        if not tail:
            await update.message.reply_text("Koi entries nahi hain.")
            return
        body = "".join(tail)
        # Telegram 4096-char message limit
        if len(body) > 3800:
            body = body[-3800:]
            body = body[body.find("\n") + 1:]  # avoid mid-line cut
        await update.message.reply_text(
            f"📜 *Audit log — last {len(tail)} entries*\n```\n{body}```",
            parse_mode="Markdown",
        )
    except Exception as ex:
        await update.message.reply_text(f"Read fail: {ex}")


# ── ENH-006: /health JSON provider ───────────────────────────────────────────
_BOT_BOOT_TS = time.time()

def _health_payload() -> dict:
    """Snapshot of runtime state for /health endpoint."""
    try:
        import psutil as _ps
        mem_pct = _ps.virtual_memory().percent
    except Exception:
        mem_pct = None
    db_ok = "ok"
    try:
        # Light DB ping — schema-only query
        with db._get_conn() as _c:
            _c.execute("SELECT 1").fetchone()
    except Exception as ex:
        db_ok = f"err: {ex}"
    active = sum(1 for s in _sessions.values() if getattr(s, "running", False))
    return {
        "status": "ok" if db_ok == "ok" else "degraded",
        "db": db_ok,
        "uptime_s": int(time.time() - _BOT_BOOT_TS),
        "active_sessions": active,
        "total_sessions": len(_sessions),
        "mem_pct": mem_pct,
        "maint_mode": _MAINT_MODE,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Access control & redeem code commands
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_target_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, str | None]:
    """Extract target user_id from a /reply or first arg. Returns (uid, error)."""
    msg = update.message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id, None
    if context.args:
        raw = context.args[0].strip().lstrip("@")
        if raw.lstrip("-").isdigit():
            return int(raw), None
        return None, "User ID integer hona chahiye (ya kisi message pe reply karein)."
    return None, "User ID do ya kisi user ke message pe reply karke command bhejein."


def _fmt_user_line(row: dict | None, uid: int | None = None) -> str:
    if not row:
        return f"`{uid}` _(unknown)_"
    name = row.get("first_name") or ""
    uname = row.get("username") or ""
    parts = [f"`{row['user_id']}`"]
    if name:
        parts.append(f"_{name[:30]}_")
    if uname:
        parts.append(f"@{uname[:30]}")
    return " ".join(parts)


# ── /claimowner — one-time bootstrap when no OWNER exists ─────────────────
async def cmd_claimowner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First-run bootstrap: promotes caller to OWNER if no OWNER set anywhere."""
    user = update.effective_user
    uid = user.id
    uname = user.username or ""
    fname = user.first_name or ""

    # Already an env-owner
    if OWNER_ID is not None and uid == OWNER_ID:
        await update.message.reply_text(
            "👑 Aap pehle se environment OWNER ho — claim ki zaroorat nahi.",
            parse_mode="Markdown",
        )
        return

    # Is there *any* owner already?
    if OWNER_ID is not None or access.count_owners_db() > 0:
        owner_link = access.get_owner_display(OWNER_ID, OWNER_USERNAME)
        await update.message.reply_text(
            "⛔ *Owner pehle se set hai.*\n"
            f"Owner: {owner_link}\n\n"
            "_Yeh command sirf pehli baar setup ke liye thi._",
            parse_mode="Markdown",
        )
        return

    # No owner anywhere → promote caller
    promoted = access.bootstrap_owner(uid, uname, fname)
    if not promoted:
        await update.message.reply_text(
            "⚠️ Bootstrap fail — kisi aur user ne pehle claim kar liya.",
        )
        return

    logger.warning(f"OWNER bootstrapped via /claimowner: uid={uid} (@{uname})")
    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   👑 *OWNER CLAIMED*\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"✅  Aap ab is bot ke *OWNER* hain.\n\n"
        f"🆔  *ID:*  `{uid}`\n"
        + (f"🏷  *Username:*  @{uname}\n" if uname else "")
        + "\n"
        "_Ab full access available hai._\n"
        "_Try:_  `/help`  •  `/menu`  •  `/diag`",
        parse_mode="Markdown",
    )


# ── /myaccess ───────────────────────────────────────────────────────────────
async def cmd_myaccess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    role = access.get_role(uid, OWNER_ID)
    row = access.get_user(uid)
    expires = row.get("expires_at") if row else None
    code_used = row.get("redeem_code_used") if row else ""
    icon = {"OWNER": "👑", "SUDO": "🛡", "PREMIUM": "💎", "GUEST": "🔒"}.get(role, "👤")
    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━╮",
        "   🆔 *YOUR ACCESS*",
        "╰━━━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"{icon}  *Role:*  `{role}`",
        f"🆔  *User ID:*  `{uid}`",
    ]
    if expires:
        lines.append(f"⏱  *Expires:*  `{access.fmt_expires(expires)}`")
        lines.append(f"⏳  *Time left:*  `{access.fmt_remaining(expires)}`")
    elif role != access.ROLE_GUEST:
        lines.append("⏱  *Expires:*  `permanent`")
    if code_used:
        lines.append(f"🎟  *Code used:*  `{code_used}`")
    if role == access.ROLE_GUEST:
        lines += [
            "",
            "_Aapko abhi access nahi hai._",
            "Code redeem karein:  `/redeem YOUR-CODE`",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /redeem <code> ──────────────────────────────────────────────────────────
async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: `/redeem YOUR-CODE`\nExample: `/redeem UAV-A7K2-9XB4-LM3Q`",
            parse_mode="Markdown",
        )
        return
    code = context.args[0]
    ok, role, expires, err = access.redeem_code(code, uid, OWNER_ID)
    if not ok:
        msgs = {
            "invalid":          "❌ Yeh code valid nahi hai. Check karke dobara try karein.",
            "expired":          "⌛ Yeh code expire ho chuka hai (revoked).",
            "exhausted":        "🔒 Yeh code apni usage limit reach kar chuka hai.",
            "already_redeemed": "⚠️ Aap pehle hi yeh code redeem kar chuke hain.",
            "already_premium":  "💎 Aap pehle se SUDO/Owner hain — redeem ki zaroorat nahi.",
            "banned":           "🚫 Aap ban kiye gaye hain — redeem allowed nahi.",
        }
        await update.message.reply_text(msgs.get(err or "", f"❌ Redeem fail: {err}"))
        logger.info(f"redeem fail: uid={uid} code={code[:20]} err={err}")
        return
    expires_txt = access.fmt_expires(expires) if expires else "permanent (lifetime)"
    remain_txt  = access.fmt_remaining(expires) if expires else "permanent"
    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   🎉 *CODE REDEEMED*\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "✅  *Welcome to Premium!*\n"
        f"💎  *Access:*  `{role}`\n"
        f"⏱  *Valid till:*  `{expires_txt}`\n"
        f"⏳  *Time:*  `{remain_txt}`\n\n"
        "_Ab full access available hai._\n"
        "_Use_ `/help` _ya_ `/menu` _to explore._",
        parse_mode="Markdown",
    )


# ── /code <days> [max_uses] [notes...] ──────────────────────────────────────
async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "*Usage:* `/code <days> [max_uses] [notes]`\n\n"
            "*Examples:*\n"
            "• `/code 30` → 30-day, 1 use\n"
            "• `/code 7 5` → 7-day, 5 uses\n"
            "• `/code 0 1 Lifetime VIP` → permanent, 1 use\n"
            "• `/code 90 10 Promo Campaign`",
            parse_mode="Markdown",
        )
        return
    try:
        days = int(args[0])
        if days < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ `days` valid integer (>=0) hona chahiye.", parse_mode="Markdown")
        return
    max_uses = 1
    if len(args) >= 2:
        try:
            max_uses = int(args[1])
            if max_uses < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ `max_uses` >=1 hona chahiye.", parse_mode="Markdown")
            return
    notes = " ".join(args[2:]) if len(args) >= 3 else ""
    # SUDO users limit: max 50 uses per code
    role = access.get_role(uid, OWNER_ID)
    if role == access.ROLE_SUDO and max_uses > 50:
        await update.message.reply_text("❌ SUDO users max 50 uses tak code bana sakte hain.")
        return
    try:
        row = access.create_code(uid, days, max_uses, notes)
    except Exception as ex:
        await update.message.reply_text(f"❌ Code generation fail: {ex}")
        logger.error(f"code gen fail by {uid}: {ex}")
        return
    duration_txt = "♾ permanent" if days == 0 else f"{days} days"
    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✅ *CODE GENERATED*\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🎟  *Code:*  `{row['code']}`\n"
        f"⏱  *Duration:*  {duration_txt}\n"
        f"🔢  *Max uses:*  `{max_uses}`\n"
        + (f"📝  *Notes:*  _{notes[:80]}_\n" if notes else "")
        + f"📅  *Created:*  `{access.fmt_expires(row['created_at'])}`\n\n"
        "_Tap code to copy. User redeems via:_\n"
        f"`/redeem {row['code']}`",
        parse_mode="Markdown",
    )


# ── /codes [active|used|expired] ────────────────────────────────────────────
async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flt = (context.args[0].lower() if context.args else "all")
    if flt not in ("all", "active", "used", "expired"):
        await update.message.reply_text("Usage: `/codes [all|active|used|expired]`", parse_mode="Markdown")
        return
    rows = access.list_codes(flt)
    if not rows:
        await update.message.reply_text(f"📭 Koi code nahi mila (filter: `{flt}`).", parse_mode="Markdown")
        return
    lines = [f"📋 *Codes ({flt}, {len(rows)})*\n"]
    for r in rows[:30]:  # cap to 30 to fit Telegram limit
        status = "🟢" if (r["is_active"] and r["current_uses"] < r["max_uses"]) else "🔴"
        notes = f" — _{r['notes'][:30]}_" if r.get("notes") else ""
        dur = "♾" if r["duration_days"] == 0 else f"{r['duration_days']}d"
        lines.append(
            f"{status} `{r['code']}`\n"
            f"   ⏱ {dur}  •  uses: {r['current_uses']}/{r['max_uses']}{notes}"
        )
    if len(rows) > 30:
        lines.append(f"\n_…aur {len(rows) - 30} aur (showing latest 30)_")
    body = "\n\n".join(lines)
    if len(body) > 4000:
        body = body[:3990] + "\n\n…"
    await update.message.reply_text(body, parse_mode="Markdown")


# ── /revokecode <code> ──────────────────────────────────────────────────────
async def cmd_revokecode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: `/revokecode UAV-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return
    code = context.args[0]
    if access.revoke_code(code, uid):
        await update.message.reply_text(f"✅ Code revoked: `{access.normalize_code(code)}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Code nahi mila ya pehle se revoked hai.")


# ── /addsudo <uid> | reply ──────────────────────────────────────────────────
async def cmd_addsudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_uid = update.effective_user.id
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: `/addsudo <user_id>` ya reply.", parse_mode="Markdown")
        return
    if access.is_owner(target, OWNER_ID):
        await update.message.reply_text("⛔ Owner already supreme — promote nahi ho sakta.")
        return
    access.set_role(target, access.ROLE_SUDO, added_by=by_uid, expires_at=None)
    access.audit(by_uid, "addsudo", f"target={target}")
    await update.message.reply_text(f"🛡 Promoted `{target}` to *SUDO*.", parse_mode="Markdown")


# ── /removesudo <uid> | reply ───────────────────────────────────────────────
async def cmd_removesudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_uid = update.effective_user.id
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: `/removesudo <user_id>` ya reply.", parse_mode="Markdown")
        return
    if access.is_owner(target, OWNER_ID):
        await update.message.reply_text("⛔ Owner ko demote nahi kiya ja sakta.")
        return
    row = access.get_user(target)
    if not row or row.get("role") != access.ROLE_SUDO:
        await update.message.reply_text("ℹ️ Yeh user SUDO nahi hai.")
        return
    access.set_role(target, access.ROLE_GUEST, added_by=by_uid, expires_at=None)
    access.audit(by_uid, "removesudo", f"target={target}")
    await update.message.reply_text(f"⬇️ Demoted `{target}` to *GUEST*.", parse_mode="Markdown")


# ── /sudolist ───────────────────────────────────────────────────────────────
async def cmd_sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = access.list_role(access.ROLE_SUDO)
    if not rows:
        await update.message.reply_text("📭 Koi SUDO user nahi hai.")
        return
    lines = [f"🛡 *SUDO Users ({len(rows)})*\n"]
    for r in rows:
        lines.append("• " + _fmt_user_line(r))
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /addpremium <uid|reply> [days] ──────────────────────────────────────────
async def cmd_addpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_uid = update.effective_user.id
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(
            f"❌ {err}\nUsage: `/addpremium <user_id> [days]` ya reply.",
            parse_mode="Markdown",
        )
        return
    if access.is_owner(target, OWNER_ID):
        await update.message.reply_text("⛔ Owner already supreme.")
        return
    # Optional [days] — second arg if first arg is an ID, or first arg if reply
    days_arg_idx = 1 if context.args and context.args[0].lstrip("-").isdigit() and not (
        update.message and update.message.reply_to_message) else 0
    if update.message and update.message.reply_to_message:
        days_arg_idx = 0
    days = 0
    if context.args and len(context.args) > days_arg_idx:
        try:
            days = int(context.args[days_arg_idx])
            if days < 0:
                raise ValueError
        except ValueError:
            days = 0
    expires_at = access._now_ts() + days * 86400 if days > 0 else None
    # SUDO cannot promote SUDO/Owner
    by_role = access.get_role(by_uid, OWNER_ID)
    target_row = access.get_user(target)
    if by_role == access.ROLE_SUDO and target_row and target_row.get("role") in (access.ROLE_SUDO, access.ROLE_OWNER):
        await update.message.reply_text("⛔ SUDO doosre SUDO/Owner ko modify nahi kar sakta.")
        return
    access.set_role(target, access.ROLE_PREMIUM, added_by=by_uid, expires_at=expires_at)
    access.audit(by_uid, "addpremium", f"target={target} days={days}")
    dur = "permanent" if days == 0 else f"{days} days"
    await update.message.reply_text(
        f"💎 `{target}` ab *PREMIUM* hai — duration: `{dur}`.",
        parse_mode="Markdown",
    )


# ── /removepremium <uid|reply> ──────────────────────────────────────────────
async def cmd_removepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_uid = update.effective_user.id
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: `/removepremium <user_id>` ya reply.", parse_mode="Markdown")
        return
    if access.is_owner(target, OWNER_ID):
        await update.message.reply_text("⛔ Owner cannot be demoted.")
        return
    by_role = access.get_role(by_uid, OWNER_ID)
    target_row = access.get_user(target)
    if by_role == access.ROLE_SUDO and target_row and target_row.get("role") in (access.ROLE_SUDO, access.ROLE_OWNER):
        await update.message.reply_text("⛔ SUDO doosre SUDO/Owner ko modify nahi kar sakta.")
        return
    if not target_row or target_row.get("role") != access.ROLE_PREMIUM:
        await update.message.reply_text("ℹ️ Yeh user PREMIUM nahi hai.")
        return
    access.set_role(target, access.ROLE_GUEST, added_by=by_uid, expires_at=None)
    access.audit(by_uid, "removepremium", f"target={target}")
    await update.message.reply_text(f"⬇️ `{target}` se PREMIUM access hata diya.", parse_mode="Markdown")


# ── /premiumlist ────────────────────────────────────────────────────────────
async def cmd_premiumlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = access.list_role(access.ROLE_PREMIUM)
    if not rows:
        await update.message.reply_text("📭 Koi PREMIUM user nahi hai.")
        return
    lines = [f"💎 *Premium Users ({len(rows)})*\n"]
    for r in rows[:50]:
        exp = access.fmt_remaining(r.get("expires_at"))
        lines.append(f"• {_fmt_user_line(r)}  ⏱ `{exp}`")
    if len(rows) > 50:
        lines.append(f"\n_…aur {len(rows) - 50} aur_")
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3990] + "\n…"
    await update.message.reply_text(body, parse_mode="Markdown")


# ── /ban <uid|reply> ────────────────────────────────────────────────────────
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_uid = update.effective_user.id
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: `/ban <user_id>` ya reply.", parse_mode="Markdown")
        return
    if access.is_owner(target, OWNER_ID):
        await update.message.reply_text("⛔ Owner ko ban nahi kiya ja sakta.")
        return
    access.ban_user(target, by_uid)
    # Also strip any role
    access.set_role(target, access.ROLE_GUEST, added_by=by_uid, expires_at=None)
    await update.message.reply_text(f"🚫 `{target}` ban kar diya gaya.", parse_mode="Markdown")


# ── /unban <uid|reply> ──────────────────────────────────────────────────────
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_uid = update.effective_user.id
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: `/unban <user_id>` ya reply.", parse_mode="Markdown")
        return
    access.unban_user(target, by_uid)
    await update.message.reply_text(f"✅ `{target}` ka ban hata diya gaya.", parse_mode="Markdown")


# ── /banned ─────────────────────────────────────────────────────────────────
async def cmd_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = access.list_banned()
    if not rows:
        await update.message.reply_text("✅ Koi banned user nahi hai.")
        return
    lines = [f"🚫 *Banned Users ({len(rows)})*\n"]
    for r in rows[:50]:
        lines.append("• " + _fmt_user_line(r))
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /userinfo <uid|reply> ───────────────────────────────────────────────────
async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target, err = _resolve_target_uid(update, context)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: `/userinfo <user_id>` ya reply.", parse_mode="Markdown")
        return
    role = access.get_role(target, OWNER_ID)
    row = access.get_user(target) or {}
    expires = row.get("expires_at")
    icon = {"OWNER": "👑", "SUDO": "🛡", "PREMIUM": "💎", "GUEST": "🔒"}.get(role, "👤")
    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━╮",
        "   ℹ️ *USER INFO*",
        "╰━━━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"🆔  *ID:*  `{target}`",
        f"👤  *Name:*  _{row.get('first_name') or '-'}_",
        f"🏷  *Username:*  @{row.get('username') or '-'}",
        f"{icon}  *Role:*  `{role}`",
        f"🚫  *Banned:*  `{'yes' if row.get('is_banned') else 'no'}`",
    ]
    if expires:
        lines.append(f"⏱  *Expires:*  `{access.fmt_expires(expires)}` ({access.fmt_remaining(expires)})")
    if row.get("redeem_code_used"):
        lines.append(f"🎟  *Code used:*  `{row['redeem_code_used']}`")
    if row.get("added_by"):
        lines.append(f"➕  *Added by:*  `{row['added_by']}`")
    if row.get("last_active"):
        lines.append(f"📡  *Last active:*  `{access.fmt_expires(row['last_active'])}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /accstats — access control stats ────────────────────────────────────────
async def cmd_accstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = access.stats()
    by_role = s["by_role"]
    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━╮",
        "   📊 *ACCESS STATS*",
        "╰━━━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f"👥  *Total users:*  `{s['users_total']}`",
        f"👑  *Owner:*       `{by_role.get('OWNER', 0)}`",
        f"🛡  *SUDO:*        `{by_role.get('SUDO', 0)}`",
        f"💎  *Premium:*     `{by_role.get('PREMIUM', 0)}`",
        f"🔒  *Guest:*       `{by_role.get('GUEST', 0)}`",
        f"🚫  *Banned:*      `{s['banned']}`",
        "",
        f"🎟  *Codes total:*    `{s['codes_total']}`",
        f"🟢  *Codes active:*   `{s['codes_active']}`",
        f"✅  *Codes redeemed:* `{s['codes_redeemed']}`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Periodic expiry sweep (called via job_queue) ────────────────────────────
async def _periodic_expire_sweep(context: ContextTypes.DEFAULT_TYPE):
    try:
        n = access.expire_check_all()
        if n:
            logger.info(f"Periodic expiry sweep: downgraded {n} users")
    except Exception as ex:
        logger.warning(f"expire sweep failed: {ex}")


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        print("ERROR: BOT_TOKEN not set.")
        return

    # ENH-006: register health provider before starting HTTP server
    try:
        from keep_alive import set_health_provider
        set_health_provider(_health_payload)
    except Exception as ex:
        logger.warning(f"Health provider registration failed: {ex}")

    keep_alive(8099)
    start_self_pinger()
    db.init_db()
    # Access control schema (bot_users + redeem_codes tables)
    try:
        access.init()
    except Exception as ex:
        logger.error(f"access.init failed: {ex}")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_on_startup)
        .post_stop(_on_shutdown)
        .build()
    )

    # Auth middleware — runs before every command (group=-1 = highest priority)
    from telegram.ext import TypeHandler
    app.add_handler(TypeHandler(Update, _auth_middleware), group=-1)

    _COMMANDS = [
        # Core
        ("start",          cmd_start),
        ("help",           cmd_help),
        # Setup — long + short
        ("set_url",        cmd_set_url),
        ("url",            cmd_set_url),
        ("addpxy",         cmd_add_proxy),
        ("ap",             cmd_add_proxy),
        ("list_proxies",   cmd_list_proxies),
        ("lp",             cmd_list_proxies),
        ("clear_proxies",  cmd_clear_proxies),
        ("clrp",           cmd_clear_proxies),
        ("chkpxy",         cmd_check_proxy),
        ("chk",            cmd_check_proxy),
        # Tor — long + short
        ("tor_on",         cmd_tor_on),
        ("ton",            cmd_tor_on),
        ("tor_off",        cmd_tor_off),
        ("toff",           cmd_tor_off),
        ("identity_on",    cmd_identity_on),
        ("idon",           cmd_identity_on),
        ("identity_off",   cmd_identity_off),
        ("idoff",          cmd_identity_off),
        ("circuit_on",     cmd_circuit_on),
        ("con",            cmd_circuit_on),
        ("circuit_off",    cmd_circuit_off),
        ("coff",           cmd_circuit_off),
        # Click — long + short
        ("set_click_text",  cmd_set_click_text),
        ("ct1",             cmd_set_click_text),
        ("set_click_text2", cmd_set_click_text2),
        ("ct2",             cmd_set_click_text2),
        ("set_selector",    cmd_set_selector),
        ("sel",             cmd_set_selector),
        ("set_selector2",   cmd_set_selector2),
        ("sel2",            cmd_set_selector2),
        # Timing — long + short
        ("set_delay",      cmd_set_delay),
        ("delay",          cmd_set_delay),
        ("set_wait",       cmd_set_wait),
        ("wait",           cmd_set_wait),
        ("set_loops",      cmd_set_loops),
        ("loops",          cmd_set_loops),
        ("set_timeout",    cmd_set_timeout),
        ("tout",           cmd_set_timeout),
        ("bint",           cmd_bint),
        ("bwait",          cmd_bwait),
        # Control
        ("run",            cmd_run),
        ("stop",           cmd_stop),
        ("restart",        cmd_restart),
        ("rs",             cmd_restart),
        ("status",         cmd_status),
        ("s",              cmd_status),
        ("ping",           cmd_ping),
        ("stats",          cmd_stats),
        ("diag",           cmd_diag),
        ("logs",           cmd_logs),
        # ── ENH-002/003/004/005: new admin + UX commands ──
        ("menu",           cmd_menu),
        ("backup",         cmd_backup),
        ("maint",          cmd_maint),
        ("audit",          cmd_audit),
        # ── Access control: all-users ──
        ("redeem",         cmd_redeem),
        ("myaccess",       cmd_myaccess),
        ("claimowner",     cmd_claimowner),
        # ── SUDO + Owner ──
        ("addpremium",     cmd_addpremium),
        ("removepremium",  cmd_removepremium),
        ("premiumlist",    cmd_premiumlist),
        ("code",           cmd_code),
        ("codes",          cmd_codes),
        ("revokecode",     cmd_revokecode),
        ("userinfo",       cmd_userinfo),
        ("accstats",       cmd_accstats),
        # ── Owner-only ──
        ("addsudo",        cmd_addsudo),
        ("removesudo",     cmd_removesudo),
        ("sudolist",       cmd_sudolist),
        ("ban",            cmd_ban),
        ("unban",          cmd_unban),
        ("banned",         cmd_banned),
    ]

    # Register all "/cmd" handlers
    for cmd, fn in _COMMANDS:
        app.add_handler(CommandHandler(cmd, fn))

    # ── Dot-prefix dispatcher: ".cmd args" works exactly like "/cmd args" ──
    # Builds a name→fn lookup, then a single MessageHandler intercepts any
    # text that begins with "." followed by a known command name.
    _DOT_HANDLERS = {name.lower(): fn for name, fn in _COMMANDS}

    async def _dot_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        if not text.startswith("."):
            return
        # Strip leading dot, parse "cmd rest..."
        body = text[1:].lstrip()
        if not body:
            return
        parts = body.split(maxsplit=1)
        cmd_name = parts[0].lower()
        # Strip @botname suffix if present (e.g. ".s@MyBot")
        if "@" in cmd_name:
            cmd_name = cmd_name.split("@", 1)[0]
        fn = _DOT_HANDLERS.get(cmd_name)
        if not fn:
            return  # Unknown ".xxx" — ignore silently
        # Mimic CommandHandler: populate context.args from the rest of text
        rest = parts[1] if len(parts) > 1 else ""
        context.args = rest.split() if rest else []
        await fn(update, context)

    # Catch any text starting with "." — dispatcher does the rest
    app.add_handler(MessageHandler(filters.Regex(r"^\.\S+"), _dot_dispatcher))

    # ENH-002: inline keyboard callbacks for /menu
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))

    # A-to-Z user guide — separate callback namespace (guide:*)
    try:
        import guide as _guide
        _guide.register(app)
    except Exception as ex:
        logger.warning(f"Guide module registration failed: {ex}")

    # Schedule periodic expiry sweep (every hour) — auto-downgrades expired users.
    # NOTE: requires python-telegram-bot[job-queue] extra. If unavailable,
    # access.get_role() still auto-expires users lazily on their next interaction,
    # so this is a nice-to-have, not critical.
    try:
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            jq = app.job_queue
        if jq is not None:
            jq.run_repeating(
                _periodic_expire_sweep,
                interval=3600,    # 1 hour
                first=60,          # first run after 60s
                name="access_expire_sweep",
            )
            logger.info("Scheduled hourly access expiry sweep")
        else:
            logger.info("JobQueue not available — expiry handled lazily on user interaction")
    except Exception as ex:
        logger.warning(f"Could not schedule expire sweep: {ex}")

    logger.info(f"Chromium  path : {CHROMIUM_PATH}")
    logger.info(f"ChromeDriver path: {CHROMEDRIVER_PATH}")
    logger.info("Bot start ho gaya...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
