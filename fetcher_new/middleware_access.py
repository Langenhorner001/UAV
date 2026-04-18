"""Aiogram middleware: role gating + ban check for all incoming updates."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from bot.config import OWNER_ID, OWNER_USERNAME
from bot import access

logger = logging.getLogger(__name__)

# Owner-only commands (full set incl aliases)
_OWNER_ONLY_CMDS = frozenset({
    "addsudo", "ads",
    "removesudo", "delsudo", "rms",
    "sudolist", "sudousers", "slist",
    "ban", "unban", "banned",
    "audit", "maint",
    "restart", "rs", "shutdown", "sd",
    "broadcast", "bc",
    "sysinfo", "sys",
})

# SUDO + OWNER (incl aliases)
_SUDO_PLUS_CMDS = frozenset({
    "addpremium", "removepremium", "premiumlist",
    "code", "codes", "revokecode",
    "userinfo", "accstats",
})


def _extract_command(message: Message) -> str | None:
    """Pull /command (no args, no @bot suffix) from a message in lowercase."""
    if not message.text:
        return None
    t = message.text.strip()
    if not t.startswith("/"):
        return None
    head = t.split(maxsplit=1)[0][1:]
    head = head.split("@", 1)[0]
    return head.lower() or None


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Only gate Message events with a from_user
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        uid   = event.from_user.id
        uname = event.from_user.username or ""
        fname = event.from_user.first_name or ""
        cmd   = _extract_command(event)

        # Touch + persist identity (best-effort)
        try:
            access.touch_user(uid, uname, fname)
        except Exception as ex:
            logger.debug(f"touch_user failed: {ex}")

        # 1. Banned -> hard block (silent for unauth events; reply if we can)
        try:
            if access.is_banned(uid):
                if cmd is not None and event.bot:
                    await event.answer("🚫 <b>Aap banned hain.</b>", parse_mode="HTML")
                return  # stop pipeline

            role = access.get_role(uid, OWNER_ID)
        except Exception as ex:
            logger.warning(f"access middleware role lookup failed: {ex}")
            role = access.ROLE_GUEST

        # 2. Owner-only command guard
        if cmd in _OWNER_ONLY_CMDS and role != access.ROLE_OWNER:
            await event.answer(
                "⛔ <b>Owner-only command.</b>",
                parse_mode="HTML",
            )
            return

        # 3. SUDO+ command guard
        if cmd in _SUDO_PLUS_CMDS and access.role_level(role) < access.LEVELS[access.ROLE_SUDO]:
            await event.answer(
                "⛔ <b>Sirf SUDO ya Owner is command ko use kar sakta hai.</b>",
                parse_mode="HTML",
            )
            return

        # 4. GUEST -> only whitelisted commands allowed
        if role == access.ROLE_GUEST and cmd is not None and cmd not in access.GUEST_ALLOWED_CMDS:
            owner_link = access.get_owner_display(OWNER_ID, OWNER_USERNAME)
            no_owner_hint = ""
            if OWNER_ID is None and access.count_owners_db() == 0:
                no_owner_hint = (
                    "\n\n⚙️ <b>Setup pending:</b>\n"
                    "<i>Agar aap is bot ke owner hain to</i> <code>/claimowner</code> <i>bhejein.</i>"
                )
            await event.answer(
                "╔══════════════════════════╗\n"
                "  🔒  <b>ACCESS RESTRICTED</b>\n"
                "╚══════════════════════════╝\n\n"
                "<i>Yeh bot invite-only hai.</i>\n\n"
                "🎟  <b>Code hai?</b>\n"
                "   <code>/redeem YOUR-CODE</code>\n\n"
                "💬  <b>Access chahiye?</b>\n"
                f"   Owner se contact karein:  {owner_link}\n\n"
                f"<i>Aapki ID:</i> <code>{uid}</code>"
                f"{no_owner_hint}",
                parse_mode="HTML",
            )
            return

        # 5. GUEST sending non-command (e.g., a link/forward) -> also block fetch
        if role == access.ROLE_GUEST and cmd is None:
            owner_link = access.get_owner_display(OWNER_ID, OWNER_USERNAME)
            no_owner_hint = ""
            if OWNER_ID is None and access.count_owners_db() == 0:
                no_owner_hint = (
                    "\n\n⚙️ <b>Setup pending:</b>\n"
                    "<i>Agar aap is bot ke owner hain to</i> <code>/claimowner</code> <i>bhejein.</i>"
                )
            await event.answer(
                "🔒 <b>Access required to fetch posts.</b>\n\n"
                f"Owner: {owner_link}\n"
                f"Aapki ID: <code>{uid}</code>\n\n"
                "<i>Code redeem:</i> <code>/redeem CODE</code>"
                f"{no_owner_hint}",
                parse_mode="HTML",
            )
            return

        # All checks passed -> continue
        return await handler(event, data)
