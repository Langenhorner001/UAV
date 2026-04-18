import asyncio
import logging
import os
import resource
import sys
import time

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.config import OWNER_ID
from bot import user_client as uc
from bot import stats

logger = logging.getLogger(__name__)
router = Router()


def _is_owner(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == OWNER_ID


# ──────────────────────────────────────────────────────────────────────────────
#  /start
# ──────────────────────────────────────────────────────────────────────────────

WELCOME_TEXT = """
╔══════════════════════════════╗
  ⚡  AUTO FETCHER BOT  ⚡
╚══════════════════════════════╝

👋 <b>Salam! Main hoon tera smart</b>
    <b>Telegram Post Fetcher!</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>Main kya karta hoon?</b>

  🔗  Kisi bhi channel ka post fetch
  🔒  Private channels bhi — no problem
  📸  Photos · Videos · Docs · Audio
  📦  Poore albums ek saath
  ↩️  Forward karo — main khud fetch karunga
  🙈  Forward cover — "Forwarded from" tag hatao
  👥  Sudo users — multiple users ko access do

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🚀 <b>Shuru karo — link paste karo:</b>

<code>https://t.me/channelname/123</code>

  Ya koi bhi post <b>forward</b> karo! 📨

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖 /help  |  ⚡ /ping  |  📊 /stats
"""


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(WELCOME_TEXT, parse_mode="HTML")


# ──────────────────────────────────────────────────────────────────────────────
#  /help
# ──────────────────────────────────────────────────────────────────────────────

HELP_TEXT = """
╔══════════════════════════════╗
  📖  HOW TO USE — FULL GUIDE
╚══════════════════════════════╝

━━━━━━  📌 TARIKE  ━━━━━━

<b>① Link Paste Karo:</b>
  Post link copy karo, yahan paste karo.

<b>② Post Forward Karo:</b>
  Bot ko forward karo — original fetch
  hoga, ya as-is copy ho jaayega ✅

<b>③ Text / Media Copy:</b>
  Koi bhi text ya media bhejo —
  bot "Forwarded from" tag hataa kar
  apna message ki tarah send karega 🙈

━━━━━━  🔗 LINK FORMATS  ━━━━━━

✦ <code>https://t.me/channel/123</code>
✦ <code>https://t.me/c/channelid/123</code>
✦ <code>https://telegram.me/channel/123</code>

━━━━━━  📦 BULK FETCH  ━━━━━━

Ek saath <b>20 links</b> tak!
  <code>https://t.me/ch/1
https://t.me/ch/2
https://t.me/ch/3</code>

━━━━  💬 GENERAL COMMANDS  ━━━━

/start              ➜  Welcome screen
/help    (or /h)    ➜  Yeh guide
/ping    (or /p)    ➜  Bot latency (ms)
/status  (or /st)   ➜  Live bot status
/stats   (or /sa)   ➜  Fetch statistics

━━━━━  👑 OWNER COMMANDS  ━━━━━

/login              ➜  Telegram account connect
/logout             ➜  Account disconnect
/restart  (or /rs)  ➜  Bot restart karo
/shutdown (or /sd)  ➜  Bot band karo
/sysinfo  (or /sys) ➜  Server health check
/broadcast (or /bc) ➜  All users ko message bhejo

━━━━━  👥 SUDO COMMANDS  ━━━━━

/addsudo  (or /ads)    ➜  User(s) ko access do
  <code>/ads 111</code>  or  <code>/ads 111,222,333</code>
/delsudo  (or /rms)    ➜  User(s) ka access hatao
  <code>/rms 111</code>  or  <code>/rms 111,222,333</code>
/sudousers (or /slist) ➜  Allowed users ki list

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ <i>Private channels ke liye account
wahan member hona chahiye.</i>
"""


@router.message(Command("help", "h"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


# ──────────────────────────────────────────────────────────────────────────────
#  /ping
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("ping", "p"))
async def cmd_ping(message: Message):
    t = time.monotonic()
    sent = await message.answer("🏓 Ping...")
    ms = (time.monotonic() - t) * 1000

    bars    = min(int(ms / 60), 10)
    bar_str = "▓" * bars + "░" * (10 - bars)

    if ms < 100:
        quality = "🟢 Excellent"
    elif ms < 300:
        quality = "🟡 Good"
    elif ms < 600:
        quality = "🟠 Average"
    else:
        quality = "🔴 Poor"

    await sent.edit_text(
        f"╔══════════════════════════╗\n"
        f"  🏓  PONG!\n"
        f"╚══════════════════════════╝\n\n"
        f"⚡ <b>Latency:</b>  <code>{ms:.1f} ms</code>\n"
        f"📶 <b>Quality:</b>  {quality}\n\n"
        f"<code>[{bar_str}]</code>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  /status
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("status", "st"))
async def cmd_status(message: Message):
    logged_in   = uc.is_logged_in()
    client_icon = "🟢" if logged_in else "🔴"
    client_text = "Active & Connected" if logged_in else "Offline — use /login"

    try:
        mem_mb   = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        mem_text = f"{mem_mb:.1f} MB"
    except Exception:
        mem_text = "N/A"

    uptime = stats.get_uptime()

    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  📊  BOT STATUS\n"
        f"╚══════════════════════════╝\n\n"
        f"🤖 <b>Bot:</b>\n"
        f"   🟢 Online &amp; Running\n\n"
        f"👤 <b>User Client:</b>\n"
        f"   {client_icon} {client_text}\n\n"
        f"⏱ <b>Uptime:</b>\n"
        f"   <code>{uptime}</code>\n\n"
        f"🧠 <b>Memory:</b>\n"
        f"   <code>{mem_text}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 /stats — fetch details dekho",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  /stats
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("stats", "sa"))
async def cmd_stats(message: Message):
    fetched = stats.messages_fetched
    failed  = stats.messages_failed
    total   = fetched + failed
    rate    = f"{fetched / total * 100:.1f}%" if total > 0 else "—"
    uptime  = stats.get_uptime()

    life_f  = stats.lifetime_fetched
    life_x  = stats.lifetime_failed
    life_t  = life_f + life_x
    life_rate = f"{life_f / life_t * 100:.1f}%" if life_t > 0 else "—"
    lifetime  = stats.get_lifetime()

    if total == 0:
        trend = "🆕 Is session mein abhi koi request nahi aayi"
    elif fetched / total >= 0.9:
        trend = "🔥 Zabardast performance!"
    elif fetched / total >= 0.7:
        trend = "✅ Acha chal raha hai"
    else:
        trend = "⚠️ Kuch errors aa rahe hain"

    # Top channels
    top_ch_lines = "\n".join(
        f"  {i+1}. <code>{c}</code> — {n}"
        for i, (c, n) in enumerate(stats.top_channels(5))
    ) or "  <i>—</i>"

    # Top users
    top_u_lines = "\n".join(
        f"  {i+1}. <code>{u}</code> — {n}"
        for i, (u, n) in enumerate(stats.top_users(5))
    ) or "  <i>—</i>"

    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  📈  BOT STATISTICS\n"
        f"╚══════════════════════════╝\n\n"
        f"⏱ <b>Session Uptime:</b>\n   <code>{uptime}</code>\n\n"
        f"⏳ <b>Lifetime:</b>\n   <code>{lifetime}</code>\n\n"
        f"━━━━━━  THIS SESSION  ━━━━━━\n"
        f"📨 Total:    <code>{total}</code>\n"
        f"✅ OK:       <code>{fetched}</code>\n"
        f"❌ Failed:   <code>{failed}</code>\n"
        f"📊 Success:  <code>{rate}</code>\n\n"
        f"━━━━━━  LIFETIME TOTAL  ━━━━━━\n"
        f"📨 Total:    <code>{life_t}</code>\n"
        f"✅ OK:       <code>{life_f}</code>\n"
        f"❌ Failed:   <code>{life_x}</code>\n"
        f"📊 Success:  <code>{life_rate}</code>\n"
        f"👥 Users:    <code>{len(stats.known_users)}</code>\n\n"
        f"━━━━━━  TOP CHANNELS  ━━━━━━\n"
        f"{top_ch_lines}\n\n"
        f"━━━━━━  TOP USERS  ━━━━━━\n"
        f"{top_u_lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{trend}",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  /sysinfo  (owner only) — server health
# ──────────────────────────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


@router.message(Command("sysinfo", "sys"))
async def cmd_sysinfo(message: Message):
    if not _is_owner(message):
        await message.answer(
            "⛔ <b>Access Denied!</b>\n\nYeh command sirf owner use kar sakta hai.",
            parse_mode="HTML",
        )
        return

    import platform, socket, shutil as _sh
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=0.4)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu_str  = f"{cpu:5.1f}%  [{_bar(cpu)}]"
        mem_str  = f"{mem.percent:5.1f}%  [{_bar(mem.percent)}]  ({mem.used // (1024**2)}/{mem.total // (1024**2)} MB)"
        disk_str = f"{disk.percent:5.1f}%  [{_bar(disk.percent)}]  ({disk.used // (1024**3)}/{disk.total // (1024**3)} GB)"
    except Exception:
        cpu_str = mem_str = disk_str = "<i>psutil unavailable</i>"

    try:
        host = socket.gethostname()
    except Exception:
        host = "?"
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = "?"

    # Boot uptime
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        d, r = divmod(secs, 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
        boot = f"{d}d {h}h {m}m"
    except Exception:
        boot = "?"

    # Telegram ping (own /getMe via raw aiohttp for speed) — fallback to bot.get_me
    t = time.monotonic()
    try:
        await message.bot.get_me()
        ping_ms = (time.monotonic() - t) * 1000
        if ping_ms < 200:    ping_color = "🟢"
        elif ping_ms < 500:  ping_color = "🟡"
        else:                ping_color = "🔴"
        ping_str = f"{ping_color} {ping_ms:.0f} ms"
    except Exception:
        ping_str = "🔴 N/A"

    uc_status = "🟢 Active" if uc.is_logged_in() else "🔴 Offline"

    await message.answer(
        "╔══════════════════════════╗\n"
        "  🖥  SERVER  HEALTH\n"
        "╚══════════════════════════╝\n\n"
        f"<b>Host  :</b> <code>{host}</code>\n"
        f"<b>IP    :</b> <code>{ip}</code>\n"
        f"<b>OS    :</b> <code>{platform.system()} {platform.release()}</code>\n"
        f"<b>Python:</b> <code>{platform.python_version()}</code>\n"
        f"<b>Boot  :</b> <code>{boot}</code>\n\n"
        "━━━━━━  RESOURCES  ━━━━━━\n"
        f"<b>CPU   :</b> <code>{cpu_str}</code>\n"
        f"<b>RAM   :</b> <code>{mem_str}</code>\n"
        f"<b>Disk  :</b> <code>{disk_str}</code>\n\n"
        "━━━━━━  TELEGRAM  ━━━━━━\n"
        f"<b>Ping       :</b> {ping_str}\n"
        f"<b>User client:</b> {uc_status}\n",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  /broadcast  (owner only) — message to all known users
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("broadcast", "bc"))
async def cmd_broadcast(message: Message):
    if not _is_owner(message):
        await message.answer(
            "⛔ <b>Access Denied!</b>\n\nYeh command sirf owner use kar sakta hai.",
            parse_mode="HTML",
        )
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Usage:</b>\n<code>/broadcast Aapka message yahan</code>\n"
            f"\n👥 Recipients: <b>{len(stats.known_users)}</b>",
            parse_mode="HTML",
        )
        return

    text = parts[1].strip()
    targets = list(stats.known_users)
    if not targets:
        await message.answer("📭 Abhi koi known user nahi hai.")
        return

    status = await message.answer(
        f"📣 Broadcasting to <b>{len(targets)}</b> users...",
        parse_mode="HTML",
    )

    sent = 0
    failed = 0
    for uid in targets:
        try:
            await message.bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            logger.warning("broadcast → %s failed: %s", uid, e)
            failed += 1
        # Telegram-friendly throttle (~25 msg/s ceiling)
        await asyncio.sleep(0.05)

    try:
        await status.edit_text(
            f"✅ <b>Broadcast complete</b>\n\n"
            f"📨 Sent: <code>{sent}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            f"👥 Total: <code>{len(targets)}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
#  /restart  (owner only)
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("restart", "rs"))
async def cmd_restart(message: Message):
    if not _is_owner(message):
        await message.answer(
            "⛔ <b>Access Denied!</b>\n\n"
            "Yeh command sirf <b>owner</b> use kar sakta hai.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "╔══════════════════════════╗\n"
        "  🔄  RESTARTING BOT...\n"
        "╚══════════════════════════╝\n\n"
        "⏳ Kuch seconds mein wapas\n"
        "    online aa jaunga! 🚀",
        parse_mode="HTML",
    )
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ──────────────────────────────────────────────────────────────────────────────
#  /shutdown  (owner only)
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("shutdown", "sd"))
async def cmd_shutdown(message: Message):
    if not _is_owner(message):
        await message.answer(
            "⛔ <b>Access Denied!</b>\n\n"
            "Yeh command sirf <b>owner</b> use kar sakta hai.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "╔══════════════════════════╗\n"
        "  🛑  SHUTTING DOWN...\n"
        "╚══════════════════════════╝\n\n"
        "💤 Bot band ho raha hai.\n"
        "    Dubara chalane ke liye\n"
        "    server pe jao.",
        parse_mode="HTML",
    )
    await asyncio.sleep(1)
    sys.exit(0)


