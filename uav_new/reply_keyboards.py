"""
Persistent reply keyboard (bottom menu) for the UAV bot.
PTB (python-telegram-bot) version with role-aware variants:
    GUEST  -> Redeem / Contact Owner / Help / Hide
    PREMIUM-> Start/Stop/Status/Stats/MyAccess/Redeem/Help/Hide
    SUDO   -> ... + [SUDO Panel]
    OWNER  -> ... + [Owner Panel]
"""
from __future__ import annotations
from typing import Optional

from telegram import ReplyKeyboardMarkup, KeyboardButton

import access


# ── Button label constants ──────────────────────────────────────────────────
BTN_START      = "▶️ Start"
BTN_STOP       = "⏹ Stop"
BTN_STATUS     = "📊 Status"
BTN_STATS      = "📜 Stats"
BTN_MYACCESS   = "🪪 My Access"
BTN_REDEEM     = "🎟 Redeem"
BTN_HELP       = "📖 Help"
BTN_HIDE       = "✖️ Hide Menu"

BTN_REDEEM_GUEST  = "🎟 Redeem Code"
BTN_CONTACT_GUEST = "💬 Contact Owner"

BTN_OWNER_PANEL = "👑 Owner Panel"
BTN_SUDO_PANEL  = "🛡 SUDO Panel"


# Whitelist used by dispatcher to match exact button taps
ALL_BUTTON_LABELS = frozenset({
    BTN_START, BTN_STOP, BTN_STATUS, BTN_STATS,
    BTN_MYACCESS, BTN_REDEEM, BTN_HELP, BTN_HIDE,
    BTN_REDEEM_GUEST, BTN_CONTACT_GUEST,
    BTN_OWNER_PANEL, BTN_SUDO_PANEL,
})


# ── Keyboard builders ───────────────────────────────────────────────────────
def kb_for_role(role: str) -> ReplyKeyboardMarkup:
    role = (role or "GUEST").upper()
    if role == access.ROLE_GUEST:
        return _kb_guest()
    if role == access.ROLE_OWNER:
        return _kb_owner()
    if role == access.ROLE_SUDO:
        return _kb_sudo()
    return _kb_premium()


def _common_premium_rows():
    return [
        [KeyboardButton(BTN_START),    KeyboardButton(BTN_STOP)],
        [KeyboardButton(BTN_STATUS),   KeyboardButton(BTN_STATS)],
        [KeyboardButton(BTN_MYACCESS), KeyboardButton(BTN_REDEEM)],
        [KeyboardButton(BTN_HELP),     KeyboardButton(BTN_HIDE)],
    ]


def _kb_premium() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        _common_premium_rows(),
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap an option or send a command...",
    )


def _kb_sudo() -> ReplyKeyboardMarkup:
    rows = _common_premium_rows() + [[KeyboardButton(BTN_SUDO_PANEL)]]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap an option or send a command...",
    )


def _kb_owner() -> ReplyKeyboardMarkup:
    rows = _common_premium_rows() + [[KeyboardButton(BTN_OWNER_PANEL)]]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap an option or send a command...",
    )


def _kb_guest() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_REDEEM_GUEST)],
            [KeyboardButton(BTN_CONTACT_GUEST), KeyboardButton(BTN_HELP)],
            [KeyboardButton(BTN_HIDE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="🔒 Redeem a code or contact owner",
    )


def kb_for_user(uid: Optional[int], owner_id: Optional[int]) -> ReplyKeyboardMarkup:
    if uid is None:
        return _kb_guest()
    role = access.get_role(uid, owner_id)
    return kb_for_role(role)
