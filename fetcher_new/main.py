import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from bot.config import BOT_TOKEN, OWNER_ID
from bot.handlers import commands, login, link_handler, access as access_handlers
from bot.middleware_access import AccessMiddleware
from bot import user_client as uc
from bot import stats
from bot import access
from keep_alive import start_keep_alive

# ── Pyrofork peer-id range patch ─────────────────────────────────────────────
# Telegram has expanded channel IDs beyond pyrofork's hardcoded range, causing
# `ValueError: Peer id invalid: -100XXXXXXXXXX` spam in update handlers.
# Replace `get_peer_type` with a permissive structural check.
def _patch_pyrofork_peer_range() -> None:
    try:
        import pyrogram.utils as _pu

        def _safe_get_peer_type(peer_id: int) -> str:
            s = str(peer_id)
            if not s.startswith("-"):
                return "user"
            if s.startswith("-100"):
                return "channel"
            return "chat"

        _pu.get_peer_type = _safe_get_peer_type
        # Bump the hardcoded constants too in case anything else reads them.
        for name in ("MAX_CHANNEL_ID", "MIN_CHANNEL_ID"):
            if hasattr(_pu, name):
                # Allow the full int64 negative range for channels.
                if name == "MIN_CHANNEL_ID":
                    setattr(_pu, name, -1009999999999999)
                else:
                    setattr(_pu, name, -1000000000000)
    except Exception as e:
        # Don't crash startup — patch is best-effort.
        print(f"[startup] pyrofork peer-id patch skipped: {e}", file=sys.stderr)


_patch_pyrofork_peer_range()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Polling auto-restart backoff: starts at 5s, caps at 60s
_RESTART_DELAY_MIN = 5
_RESTART_DELAY_MAX = 60
# Heartbeat: log memory + alive status every 10 minutes
_HEARTBEAT_INTERVAL = 10 * 60


# ── Global asyncio exception handler ─────────────────────────────────────────

def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict):
    """Catch uncaught asyncio exceptions — log them without crashing the process."""
    exc = context.get("exception")
    msg = context.get("message", "Unknown asyncio error")
    if exc:
        logger.error("Uncaught asyncio exception: %s — %s", msg, exc, exc_info=exc)
    else:
        logger.error("Asyncio error (no exception object): %s", msg)


# ── Heartbeat background task ─────────────────────────────────────────────────

async def _heartbeat_loop():
    """Log memory usage and alive status every 10 minutes."""
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            import resource
            mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            mem_mb = mem_kb / 1024
        except Exception:
            mem_mb = 0.0
        logger.info(
            "[HEARTBEAT] Bot alive — memory: %.1f MB — user_client: %s",
            mem_mb,
            "active" if uc.is_logged_in() else "OFFLINE",
        )


# ── Owner notification helpers ────────────────────────────────────────────────

async def _notify(bot: Bot, text: str):
    """Send a silent notification to the owner. Never raises."""
    try:
        await bot.send_message(OWNER_ID, text, parse_mode="HTML",
                               disable_notification=True)
    except Exception as e:
        logger.warning("Could not notify owner: %s", e)


# ── Eye-catchy startup banner ────────────────────────────────────────────────

def _bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


async def _startup_banner(bot: Bot) -> str:
    """Build a rich HTML banner showing bot/server health for the owner."""
    import platform, socket, time as _time

    # Telegram ping
    t = _time.monotonic()
    try:
        me = await bot.get_me()
        ping_ms = (_time.monotonic() - t) * 1000
        bot_user = f"@{me.username}" if me.username else me.first_name
    except Exception:
        ping_ms = 0
        bot_user = "?"

    if ping_ms < 200:    ping_color = "🟢"
    elif ping_ms < 500:  ping_color = "🟡"
    else:                ping_color = "🔴"

    # Server
    try:
        host = socket.gethostname()
    except Exception:
        host = "?"
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = "?"
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        d, r = divmod(secs, 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
        boot = f"{d}d {h}h {m}m"
    except Exception:
        boot = "?"

    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=0.4)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu_str  = f"{cpu:5.1f}%  [{_bar(cpu)}]"
        mem_str  = f"{mem.percent:5.1f}%  [{_bar(mem.percent)}]"
        disk_str = f"{disk.percent:5.1f}%  [{_bar(disk.percent)}]"
    except Exception:
        cpu_str = mem_str = disk_str = "<i>n/a</i>"

    uc_status = "🟢 Active" if uc.is_logged_in() else "🔴 Offline"

    # Versions
    try:
        import aiogram, pyrogram
        ver = f"aiogram {aiogram.__version__} · pyrogram {pyrogram.__version__}"
    except Exception:
        ver = "?"

    return (
        "╔═══════════════════════════════╗\n"
        "  🚀  <b>TG-POST-FETCHER ONLINE</b>\n"
        "╚═══════════════════════════════╝\n\n"
        f"<b>Bot     :</b> {bot_user}\n"
        f"<b>Ping    :</b> {ping_color} <code>{ping_ms:.0f} ms</code>\n"
        f"<b>Stack   :</b> <code>{ver}</code>\n\n"
        "━━━━━━  SERVER  ━━━━━━\n"
        f"<b>Host    :</b> <code>{host}</code>\n"
        f"<b>IP      :</b> <code>{ip}</code>\n"
        f"<b>OS      :</b> <code>{platform.system()} {platform.release()}</code>\n"
        f"<b>Python  :</b> <code>{platform.python_version()}</code>\n"
        f"<b>Boot    :</b> <code>{boot}</code>\n\n"
        "━━━━━━  RESOURCES  ━━━━━━\n"
        f"<b>CPU     :</b> <code>{cpu_str}</code>\n"
        f"<b>RAM     :</b> <code>{mem_str}</code>\n"
        f"<b>Disk    :</b> <code>{disk_str}</code>\n\n"
        "━━━━━━  STATUS  ━━━━━━\n"
        f"<b>User cli:</b> {uc_status}\n"
        f"<b>Lifetime:</b> <code>{stats.lifetime_fetched}</code> fetched · "
        f"<code>{stats.lifetime_failed}</code> failed\n"
        f"<b>Users   :</b> <code>{len(stats.known_users)}</code> known\n\n"
        "<i>Type /help for commands.</i>"
    )


# ── Aiogram global error handler ─────────────────────────────────────────────

def _register_error_handler(dp: Dispatcher, bot: Bot):
    """Register a catch-all error handler so handler exceptions never crash polling."""
    @dp.errors()
    async def _on_error(event: ErrorEvent):
        logger.error(
            "Unhandled handler exception for update %s: %s",
            event.update.update_id if event.update else "?",
            event.exception,
            exc_info=event.exception,
        )
        # Silently swallow — polling continues


# ── Polling with auto-restart ─────────────────────────────────────────────────

async def _polling_loop(bot: Bot, dp: Dispatcher):
    """
    Run polling forever. On crash, wait with exponential backoff and restart.
    Notifies owner on each crash and recovery.
    """
    delay = _RESTART_DELAY_MIN
    attempt = 0

    while True:
        attempt += 1
        logger.info("Starting polling (attempt #%d)…", attempt)
        try:
            await dp.start_polling(bot, drop_pending_updates=(attempt == 1))
            # start_polling returned normally (graceful stop) — exit loop
            logger.info("Polling stopped gracefully.")
            break
        except Exception as e:
            logger.error(
                "Polling crashed (attempt #%d): %s — restarting in %ds…",
                attempt, e, delay, exc_info=True,
            )
            await _notify(
                bot,
                f"⚠️ <b>Bot polling crashed</b> (attempt #{attempt})\n"
                f"<code>{e}</code>\n"
                f"Restarting in {delay}s…",
            )
            await asyncio.sleep(delay)
            # Re-clear webhook in case Telegram locked the session
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
            # Exponential backoff, capped at max
            delay = min(delay * 2, _RESTART_DELAY_MAX)
            await _notify(bot, "🔄 <b>Bot polling restarted.</b>")


# ── Main entry ────────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Cleared previous webhook/polling session.")

    # Initialize access control schema + migrate legacy sudo_users.txt -> DB
    try:
        access.init()
        # Refresh in-memory SUDO cache so link_handler.py sees DB-backed sudos
        from bot import config as _cfg
        _cfg.refresh_sudo_users()
        logger.info(
            "Access control: %d sudo, %d premium, %d banned, %d codes active",
            len(access.list_role(access.ROLE_SUDO)),
            len(access.list_role(access.ROLE_PREMIUM)),
            len(access.list_banned()),
            len(access.list_codes("active")),
        )
    except Exception as ex:
        logger.error("access.init failed: %s", ex, exc_info=True)

    dp = Dispatcher(storage=MemoryStorage())
    # Global access middleware (ban/role gate) — must be first
    dp.message.middleware(AccessMiddleware())
    # Access router takes precedence so /addsudo etc. use the DB-backed handlers
    dp.include_router(access_handlers.router)
    dp.include_router(commands.router)
    dp.include_router(login.router)
    dp.include_router(link_handler.router)
    _register_error_handler(dp, bot)

    logged_in = await uc.start_user_client()
    if logged_in:
        logger.info("✅ User client is active.")
    else:
        logger.warning("⚠️  No session found — owner must set SESSION_STRING or use /login.")

    await start_keep_alive()

    # Background heartbeat
    asyncio.create_task(_heartbeat_loop())

    # Notify owner with eye-catchy startup banner
    try:
        banner = await _startup_banner(bot)
        await _notify(bot, banner)
    except Exception as e:
        logger.warning("Startup banner failed: %s", e)
        await _notify(bot, "✅ <b>Bot started successfully.</b>")

    logger.info("Bot entering polling loop…")
    try:
        await _polling_loop(bot, dp)
    finally:
        await _notify(bot, "🛑 <b>Bot process is shutting down.</b>")
        await uc.stop_user_client()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard.")
    except Exception as e:
        logger.critical("Fatal startup error: %s", e, exc_info=True)
        sys.exit(1)
