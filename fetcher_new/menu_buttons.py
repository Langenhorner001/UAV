"""
Persistent reply keyboard (bottom menu) for the Fetcher bot.
Layout mirrors the reference 2+2+2+1 grid; labels adapted to the
post-fetcher domain. Role-aware variants:
    GUEST  -> minimal (Redeem / Contact Owner / Help)
    PREMIUM-> full grid + Help
    SUDO   -> full grid + [SUDO Panel]
    OWNER  -> full grid + [Owner Panel]

Pressing a button sends its label as plain text to the bot. The
F.text == "..." router below maps each label back to the right action.
The middleware whitelists every label string so guest taps don't get
blocked by the access gate.
"""
from __future__ import annotations

import logging
import time

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from bot.config import OWNER_ID, OWNER_USERNAME
from bot import access, stats, user_client as uc

logger = logging.getLogger(__name__)
router = Router()


# ── Button labels (single source of truth) ──────────────────────────────────
BTN_FETCH         = "🔗 Fetch Link"
BTN_STATS         = "📊 Stats"
BTN_MYACCESS      = "🪪 My Access"
BTN_REDEEM        = "🎟 Redeem"
BTN_STATUS        = "📡 Status"
BTN_CONTACT       = "📞 Contact Owner"
BTN_HELP          = "📖 Help"

# Guest-only variants
BTN_REDEEM_GUEST  = "🎟 Redeem Code"
BTN_CONTACT_GUEST = "💬 Contact Owner"

# Role panels
BTN_OWNER_PANEL   = "👑 Owner Panel"
BTN_SUDO_PANEL    = "🛡 SUDO Panel"


# Whitelist used by middleware to allow guest taps without auth gate
ALL_BUTTON_LABELS: frozenset[str] = frozenset({
    BTN_FETCH, BTN_STATS, BTN_MYACCESS, BTN_REDEEM, BTN_STATUS,
    BTN_CONTACT, BTN_HELP,
    BTN_REDEEM_GUEST, BTN_CONTACT_GUEST,
    BTN_OWNER_PANEL, BTN_SUDO_PANEL,
})


# ── Keyboard builders ───────────────────────────────────────────────────────
def kb_for_role(role: str) -> ReplyKeyboardMarkup:
    """Pick the right reply keyboard for a given role string."""
    role = (role or "GUEST").upper()
    if role == access.ROLE_GUEST:
        return _kb_guest()
    if role == access.ROLE_OWNER:
        return _kb_owner()
    if role == access.ROLE_SUDO:
        return _kb_sudo()
    return _kb_premium()


def _kb_premium() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FETCH),    KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_MYACCESS), KeyboardButton(text=BTN_REDEEM)],
            [KeyboardButton(text=BTN_STATUS),   KeyboardButton(text=BTN_CONTACT)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap an option or paste a t.me/... link",
    )


def _kb_sudo() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FETCH),    KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_MYACCESS), KeyboardButton(text=BTN_REDEEM)],
            [KeyboardButton(text=BTN_STATUS),   KeyboardButton(text=BTN_CONTACT)],
            [KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_SUDO_PANEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap an option or paste a t.me/... link",
    )


def _kb_owner() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FETCH),    KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_MYACCESS), KeyboardButton(text=BTN_REDEEM)],
            [KeyboardButton(text=BTN_STATUS),   KeyboardButton(text=BTN_CONTACT)],
            [KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_OWNER_PANEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap an option or paste a t.me/... link",
    )


def _kb_guest() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REDEEM_GUEST)],
            [KeyboardButton(text=BTN_CONTACT_GUEST), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="🔒 Redeem a code or contact owner",
    )


def kb_for_message(message: Message) -> ReplyKeyboardMarkup:
    """Convenience: pick keyboard for the user who sent `message`."""
    if not message.from_user:
        return _kb_guest()
    role = access.get_role(message.from_user.id, OWNER_ID)
    return kb_for_role(role)


# ── /menu command (re-open keyboard anytime) ────────────────────────────────
@router.message(Command("menu", "m"))
async def cmd_menu(message: Message):
    role = access.get_role(message.from_user.id, OWNER_ID) if message.from_user else access.ROLE_GUEST
    badge = {
        access.ROLE_OWNER:   "👑 OWNER",
        access.ROLE_SUDO:    "⚡ SUDO",
        access.ROLE_PREMIUM: "💎 PREMIUM",
        access.ROLE_GUEST:   "👤 GUEST",
    }.get(role, role)
    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  📋  MAIN MENU\n"
        f"╚══════════════════════════╝\n\n"
        f"<b>Your role:</b>  {badge}\n\n"
        f"<i>Niche button tap karein ya command bhejein.</i>",
        parse_mode="HTML",
        reply_markup=kb_for_role(role),
    )


# ── Button handlers ─────────────────────────────────────────────────────────
@router.message(F.text == BTN_FETCH)
async def btn_fetch(message: Message):
    await message.answer(
        "🔗 <b>Send a Telegram post link</b>\n\n"
        "Format:\n"
        "  • <code>https://t.me/channelname/123</code>\n"
        "  • <code>https://t.me/c/123456789/45</code>\n\n"
        "Ya kisi post ko <b>forward</b> kar dein.\n"
        "Bulk: ek hi message mein 20 links tak.",
        parse_mode="HTML",
    )


@router.message(F.text == BTN_STATS)
async def btn_stats(message: Message):
    # Reuse the existing /stats handler logic
    from bot.handlers.commands import cmd_stats
    await cmd_stats(message)


@router.message(F.text == BTN_MYACCESS)
async def btn_myaccess(message: Message):
    from bot.handlers.access import cmd_myaccess
    await cmd_myaccess(message)


@router.message(F.text.in_({BTN_REDEEM, BTN_REDEEM_GUEST}))
async def btn_redeem(message: Message):
    await message.answer(
        "🎟 <b>Redeem a code</b>\n\n"
        "Bhejein:\n"
        "<code>/redeem FCH-XXXX-XXXX-XXXX</code>\n\n"
        "<i>Owner ya SUDO se code mangwa lijiye.</i>",
        parse_mode="HTML",
    )


@router.message(F.text == BTN_STATUS)
async def btn_status(message: Message):
    from bot.handlers.commands import cmd_status
    await cmd_status(message)


@router.message(F.text.in_({BTN_CONTACT, BTN_CONTACT_GUEST}))
async def btn_contact(message: Message):
    owner_link = access.get_owner_display(OWNER_ID, OWNER_USERNAME)
    btn_kwargs = {"text": "👤 Open Owner Chat"}
    if OWNER_USERNAME:
        btn_kwargs["url"] = f"https://t.me/{OWNER_USERNAME.lstrip('@')}"
    elif OWNER_ID:
        btn_kwargs["url"] = f"tg://user?id={OWNER_ID}"
    contact_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**btn_kwargs)]]) \
        if btn_kwargs.get("url") else None
    await message.answer(
        "📞 <b>Contact Owner</b>\n\n"
        f"Owner:  {owner_link}\n\n"
        f"<i>Aapki ID:</i> <code>{message.from_user.id if message.from_user else '?'}</code>\n"
        "<i>Access ya code request karne ke liye message bhejein.</i>",
        parse_mode="HTML",
        reply_markup=contact_kb,
    )


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    from bot.handlers.commands import cmd_help
    await cmd_help(message)


# ── Owner Panel (inline keyboard with admin shortcuts) ──────────────────────
@router.message(F.text == BTN_OWNER_PANEL)
async def btn_owner_panel(message: Message):
    if not message.from_user or not access.is_owner(message.from_user.id, OWNER_ID):
        return  # silent — middleware should have blocked already
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Access Stats", callback_data="op:accstats"),
         InlineKeyboardButton(text="📜 Audit Log",   callback_data="op:audit")],
        [InlineKeyboardButton(text="⚡ SUDO List",    callback_data="op:sudolist"),
         InlineKeyboardButton(text="💎 Premium List", callback_data="op:premlist")],
        [InlineKeyboardButton(text="🎟 Active Codes", callback_data="op:codes"),
         InlineKeyboardButton(text="🚫 Banned",       callback_data="op:banned")],
        [InlineKeyboardButton(text="🖥 Sysinfo",      callback_data="op:sysinfo"),
         InlineKeyboardButton(text="📣 Broadcast",    callback_data="op:bcinfo")],
    ])
    await message.answer(
        "╔══════════════════════════╗\n"
        "  👑  OWNER PANEL\n"
        "╚══════════════════════════╝\n\n"
        "<i>Kisi bhi tile par tap karein, ya nicche likhe commands use karein:</i>\n\n"
        "<code>/code DAYS [USES]</code> — naya redeem code\n"
        "<code>/addsudo UID</code> · <code>/removesudo UID</code>\n"
        "<code>/addpremium UID DAYS</code> · <code>/removepremium UID</code>\n"
        "<code>/ban UID</code> · <code>/unban UID</code>\n"
        "<code>/userinfo UID</code> · <code>/broadcast TEXT</code>\n"
        "<code>/restart</code> · <code>/shutdown</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.message(F.text == BTN_SUDO_PANEL)
async def btn_sudo_panel(message: Message):
    if not message.from_user:
        return
    role = access.get_role(message.from_user.id, OWNER_ID)
    if access.role_level(role) < access.LEVELS[access.ROLE_SUDO]:
        return  # silent
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Access Stats", callback_data="op:accstats"),
         InlineKeyboardButton(text="💎 Premium List", callback_data="op:premlist")],
        [InlineKeyboardButton(text="🎟 Active Codes", callback_data="op:codes")],
    ])
    await message.answer(
        "╔══════════════════════════╗\n"
        "  🛡  SUDO PANEL\n"
        "╚══════════════════════════╝\n\n"
        "<i>SUDO commands:</i>\n\n"
        "<code>/code DAYS [USES]</code> — naya redeem code\n"
        "<code>/codes [active|all]</code> — list codes\n"
        "<code>/addpremium UID DAYS</code> · <code>/removepremium UID</code>\n"
        "<code>/userinfo UID</code> · <code>/accstats</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Owner-panel callback dispatch ───────────────────────────────────────────
from aiogram.types import CallbackQuery


@router.callback_query(F.data.startswith("op:"))
async def cb_owner_panel(cb: CallbackQuery):
    if not cb.from_user or not cb.message:
        return await cb.answer()
    uid = cb.from_user.id
    role = access.get_role(uid, OWNER_ID)
    is_sudo_plus = access.role_level(role) >= access.LEVELS[access.ROLE_SUDO]
    is_owner = access.is_owner(uid, OWNER_ID)

    action = (cb.data or "")[3:]
    # OWNER-only actions
    owner_only = {"audit", "sudolist", "banned", "sysinfo", "bcinfo"}
    if action in owner_only and not is_owner:
        return await cb.answer("Owner only.", show_alert=True)
    if not is_sudo_plus:
        return await cb.answer("Sudo+ only.", show_alert=True)

    # Build a fake Message so we can reuse existing handlers' logic
    msg = cb.message
    try:
        if action == "accstats":
            from bot.handlers.access import cmd_accstats
            # cmd_accstats reads from access.stats() — call & reply
            s = access.stats()
            await msg.answer(
                f"📊 <b>ACCESS STATS</b>\n\n"
                f"Users: <code>{s['users_total']}</code>\n"
                f"  👑 Owner   : <code>{s['by_role'].get('OWNER', 0)}</code>\n"
                f"  ⚡ Sudo    : <code>{s['by_role'].get('SUDO', 0)}</code>\n"
                f"  💎 Premium : <code>{s['by_role'].get('PREMIUM', 0)}</code>\n"
                f"  👤 Guest   : <code>{s['by_role'].get('GUEST', 0)}</code>\n"
                f"  🚫 Banned  : <code>{s['banned']}</code>\n\n"
                f"Codes: <code>{s['codes_total']}</code> (active <code>{s['codes_active']}</code>, "
                f"redeemed <code>{s['codes_redeemed']}</code>)",
                parse_mode="HTML",
            )
        elif action == "sudolist":
            rows = access.list_role(access.ROLE_SUDO)
            body = "\n".join(f"  • <code>{r['user_id']}</code> @{r.get('username') or '—'}"
                             for r in rows[:50]) or "<i>—</i>"
            await msg.answer(f"⚡ <b>SUDO ({len(rows)})</b>\n\n{body}", parse_mode="HTML")
        elif action == "premlist":
            rows = access.list_role(access.ROLE_PREMIUM)
            body = "\n".join(
                f"  • <code>{r['user_id']}</code> @{r.get('username') or '—'} — "
                f"<code>{access.fmt_remaining(r.get('expires_at'))}</code>"
                for r in rows[:50]) or "<i>—</i>"
            await msg.answer(f"💎 <b>PREMIUM ({len(rows)})</b>\n\n{body}", parse_mode="HTML")
        elif action == "codes":
            rows = access.list_codes("active")
            lines = []
            for r in rows[:25]:
                days = r.get("duration_days", 0)
                d_str = "perm" if days == 0 else f"{days}d"
                used = f"{r.get('current_uses', 0)}/{r.get('max_uses', 1)}"
                lines.append(f"  • <code>{r['code']}</code>  {d_str}  used={used}")
            body = "\n".join(lines) or "<i>—</i>"
            await msg.answer(f"🎟 <b>ACTIVE CODES ({len(rows)})</b>\n\n{body}", parse_mode="HTML")
        elif action == "banned":
            rows = access.list_banned()
            body = "\n".join(f"  • <code>{r['user_id']}</code> @{r.get('username') or '—'}"
                             for r in rows[:50]) or "<i>—</i>"
            await msg.answer(f"🚫 <b>BANNED ({len(rows)})</b>\n\n{body}", parse_mode="HTML")
        elif action == "audit":
            from bot.handlers.access import cmd_audit
            await cmd_audit(msg)
        elif action == "sysinfo":
            from bot.handlers.commands import cmd_sysinfo
            await cmd_sysinfo(msg)
        elif action == "bcinfo":
            await msg.answer(
                "📣 <b>Broadcast usage</b>\n\n"
                "<code>/broadcast Aapka message yahan</code>\n"
                f"\n👥 Recipients: <b>{len(stats.known_users)}</b>",
                parse_mode="HTML",
            )
        else:
            await cb.answer("Unknown action.", show_alert=True)
            return
    except Exception as ex:
        logger.exception("owner panel callback failed")
        await cb.answer(f"Error: {ex}", show_alert=True)
        return
    await cb.answer()
