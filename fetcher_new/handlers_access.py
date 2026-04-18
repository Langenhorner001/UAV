"""Aiogram handlers: access control + redeem code commands."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import OWNER_ID, OWNER_USERNAME
from bot import access

logger = logging.getLogger(__name__)
router = Router()


# ── Helpers ─────────────────────────────────────────────────────────────────
def _user_label(row: dict) -> str:
    uid = row.get("user_id")
    uname = row.get("username") or ""
    fn = row.get("first_name") or ""
    bits = [f"<code>{uid}</code>"]
    if uname:
        bits.append(f"@{uname}")
    if fn:
        bits.append(escape(fn))
    return " · ".join(bits)


def _parse_target_uid(text: str) -> int | None:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip().split()[0]
    if raw.isdigit():
        return int(raw)
    return None


def _parse_args(text: str) -> list[str]:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return []
    return parts[1].strip().split()


def _badge(role: str) -> str:
    return {
        access.ROLE_OWNER:   "👑 OWNER",
        access.ROLE_SUDO:    "⚡ SUDO",
        access.ROLE_PREMIUM: "💎 PREMIUM",
        access.ROLE_GUEST:   "👤 GUEST",
    }.get(role, role)


# ── /myaccess ───────────────────────────────────────────────────────────────
@router.message(Command("myaccess"))
async def cmd_myaccess(message: Message):
    uid = message.from_user.id
    role = access.get_role(uid, OWNER_ID)
    row = access.get_user(uid) or {}
    expires_str = access.fmt_expires(row.get("expires_at"))
    remaining   = access.fmt_remaining(row.get("expires_at"))
    code_used   = row.get("redeem_code_used") or "—"
    banned_str  = "🚫 YES" if row.get("is_banned") else "✅ No"
    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  🪪  YOUR ACCESS\n"
        f"╚══════════════════════════╝\n\n"
        f"<b>User ID :</b>  <code>{uid}</code>\n"
        f"<b>Role    :</b>  {_badge(role)}\n"
        f"<b>Expires :</b>  <code>{expires_str}</code>\n"
        f"<b>Remains :</b>  <code>{remaining}</code>\n"
        f"<b>Code    :</b>  <code>{escape(code_used)}</code>\n"
        f"<b>Banned  :</b>  {banned_str}",
        parse_mode="HTML",
    )


# ── /redeem CODE ────────────────────────────────────────────────────────────
@router.message(Command("redeem"))
async def cmd_redeem(message: Message):
    args = _parse_args(message.text or "")
    if not args:
        await message.answer(
            "❌ <b>Usage:</b>  <code>/redeem FCH-XXXX-XXXX-XXXX</code>",
            parse_mode="HTML",
        )
        return
    code = access.normalize_code(args[0])
    uid = message.from_user.id
    ok, role, expires_at, err = access.redeem_code(code, uid, OWNER_ID)
    if not ok:
        emap = {
            "invalid":           "❌ Code invalid hai.",
            "expired":           "❌ Code revoked / inactive hai.",
            "exhausted":         "❌ Code already fully used.",
            "already_redeemed":  "ℹ️ Aap is code ko pehle redeem kar chuke hain.",
            "already_premium":   "ℹ️ Aap pehle se SUDO/Owner hain.",
            "banned":            "🚫 Aap banned hain.",
        }
        await message.answer(emap.get(err or "invalid", "❌ Redeem fail."), parse_mode="HTML")
        return
    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  ✅  REDEEM SUCCESS\n"
        f"╚══════════════════════════╝\n\n"
        f"💎 <b>Role:</b> {_badge(role)}\n"
        f"⏳ <b>Expires:</b> <code>{access.fmt_expires(expires_at)}</code>\n"
        f"🎟  <b>Code:</b> <code>{escape(code)}</code>\n\n"
        f"<i>Ab aap bot ke saare features use kar sakte hain.</i>",
        parse_mode="HTML",
    )


# ── /claimowner (only works if no owner exists) ────────────────────────────
@router.message(Command("claimowner"))
async def cmd_claimowner(message: Message):
    uid = message.from_user.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""
    # If env OWNER_ID is set and matches -> tell user they're already owner
    if OWNER_ID and uid == OWNER_ID:
        # Ensure DB row exists with OWNER role
        access.touch_user(uid, uname, fname)
        access.set_role(uid, access.ROLE_OWNER, added_by=uid)
        await message.answer(
            "👑 <b>Aap pehle se Owner hain</b> (env-based).",
            parse_mode="HTML",
        )
        return
    if access.count_owners_db() > 0 or (OWNER_ID and uid != OWNER_ID):
        await message.answer(
            "❌ <b>Owner pehle se assigned hai.</b>",
            parse_mode="HTML",
        )
        return
    if access.bootstrap_owner(uid, uname, fname):
        await message.answer(
            f"╔══════════════════════════╗\n"
            f"  👑  OWNER CLAIMED\n"
            f"╚══════════════════════════╝\n\n"
            f"<b>UID:</b> <code>{uid}</code>\n"
            f"<b>Tag:</b> @{escape(uname or '—')}",
            parse_mode="HTML",
        )
    else:
        await message.answer("❌ Bootstrap fail.", parse_mode="HTML")


# ── /addpremium UID DAYS [notes] ────────────────────────────────────────────
@router.message(Command("addpremium"))
async def cmd_addpremium(message: Message):
    args = _parse_args(message.text or "")
    if len(args) < 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await message.answer(
            "❌ <b>Usage:</b>  <code>/addpremium UID DAYS [notes]</code>\n"
            "<i>DAYS=0 means permanent.</i>",
            parse_mode="HTML",
        )
        return
    target = int(args[0])
    days = int(args[1])
    if days < 0:
        await message.answer("❌ DAYS must be >= 0.", parse_mode="HTML")
        return
    notes = " ".join(args[2:]) if len(args) > 2 else ""
    expires = None if days == 0 else access._now_ts() + days * 86400
    access.set_role(target, access.ROLE_PREMIUM, added_by=message.from_user.id, expires_at=expires)
    access.audit(message.from_user.id, "addpremium", f"target={target} days={days} notes={notes}")
    await message.answer(
        f"✅ <b>PREMIUM granted</b>\n\n"
        f"👤 <code>{target}</code>\n"
        f"⏳ <code>{access.fmt_expires(expires)}</code>",
        parse_mode="HTML",
    )


# ── /removepremium UID ─────────────────────────────────────────────────────
@router.message(Command("removepremium"))
async def cmd_removepremium(message: Message):
    target = _parse_target_uid(message.text or "")
    if not target:
        await message.answer("❌ <b>Usage:</b>  <code>/removepremium UID</code>", parse_mode="HTML")
        return
    row = access.get_user(target)
    if not row or (row.get("role") or "").upper() != access.ROLE_PREMIUM:
        await message.answer("ℹ️ Yeh user PREMIUM nahi hai.", parse_mode="HTML")
        return
    access.set_role(target, access.ROLE_GUEST, added_by=message.from_user.id)
    access.audit(message.from_user.id, "removepremium", f"target={target}")
    await message.answer(f"✅ <code>{target}</code> ka PREMIUM revoke ho gaya.", parse_mode="HTML")


# ── /premiumlist ────────────────────────────────────────────────────────────
@router.message(Command("premiumlist"))
async def cmd_premiumlist(message: Message):
    rows = access.list_role(access.ROLE_PREMIUM)
    if not rows:
        await message.answer("📭 Koi PREMIUM user nahi.", parse_mode="HTML")
        return
    lines = []
    for r in rows[:50]:
        rem = access.fmt_remaining(r.get("expires_at"))
        lines.append(f"  • {_user_label(r)} — <code>{rem}</code>")
    await message.answer(
        f"💎 <b>PREMIUM USERS ({len(rows)})</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── /addsudo UID ───────────────────────────────────────────────────────────
@router.message(Command("addsudo", "ads"))
async def cmd_addsudo(message: Message):
    args = _parse_args(message.text or "")
    if not args:
        await message.answer(
            "❌ <b>Usage:</b>  <code>/addsudo UID</code>  ya  <code>/addsudo 111,222,333</code>",
            parse_mode="HTML",
        )
        return
    raw_ids = [x.strip() for x in args[0].replace(" ", ",").split(",") if x.strip()]
    if any(not x.isdigit() for x in raw_ids):
        await message.answer("❌ Sirf numeric IDs.", parse_mode="HTML")
        return
    added, skipped = [], []
    for raw in raw_ids:
        uid = int(raw)
        if uid == OWNER_ID:
            skipped.append(f"<code>{uid}</code> (owner)")
            continue
        cur = access.get_role(uid, OWNER_ID)
        if cur == access.ROLE_SUDO:
            skipped.append(f"<code>{uid}</code> (already)")
            continue
        access.set_role(uid, access.ROLE_SUDO, added_by=message.from_user.id)
        added.append(f"<code>{uid}</code>")
    access.audit(message.from_user.id, "addsudo",
                 f"added={','.join(added)} skipped={','.join(skipped)}")
    # Refresh legacy in-memory set so link_handler picks up immediately
    try:
        from bot import config as _cfg
        _cfg.refresh_sudo_users()
    except Exception:
        pass
    parts = []
    if added:   parts.append(f"✅ <b>Added ({len(added)}):</b> " + ", ".join(added))
    if skipped: parts.append(f"⏭️ <b>Skipped ({len(skipped)}):</b> " + ", ".join(skipped))
    await message.answer(
        "╔══════════════════════════╗\n"
        "  👥  SUDO UPDATE\n"
        "╚══════════════════════════╝\n\n" + "\n\n".join(parts),
        parse_mode="HTML",
    )


# ── /removesudo (alias /delsudo /rms) ───────────────────────────────────────
@router.message(Command("removesudo", "delsudo", "rms"))
async def cmd_removesudo(message: Message):
    args = _parse_args(message.text or "")
    if not args:
        await message.answer("❌ <b>Usage:</b>  <code>/removesudo UID</code>", parse_mode="HTML")
        return
    raw_ids = [x.strip() for x in args[0].replace(" ", ",").split(",") if x.strip()]
    if any(not x.isdigit() for x in raw_ids):
        await message.answer("❌ Sirf numeric IDs.", parse_mode="HTML")
        return
    removed, skipped = [], []
    for raw in raw_ids:
        uid = int(raw)
        cur_role = access.get_role(uid, OWNER_ID)
        if cur_role != access.ROLE_SUDO:
            skipped.append(f"<code>{uid}</code> (not sudo)")
            continue
        access.set_role(uid, access.ROLE_GUEST, added_by=message.from_user.id)
        removed.append(f"<code>{uid}</code>")
    access.audit(message.from_user.id, "removesudo",
                 f"removed={','.join(removed)} skipped={','.join(skipped)}")
    try:
        from bot import config as _cfg
        _cfg.refresh_sudo_users()
    except Exception:
        pass
    parts = []
    if removed: parts.append(f"🗑 <b>Removed ({len(removed)}):</b> " + ", ".join(removed))
    if skipped: parts.append(f"⏭️ <b>Skipped ({len(skipped)}):</b> " + ", ".join(skipped))
    await message.answer(
        "╔══════════════════════════╗\n"
        "  🗑  SUDO REMOVE\n"
        "╚══════════════════════════╝\n\n" + "\n\n".join(parts),
        parse_mode="HTML",
    )


# ── /sudolist (alias /sudousers /slist) ─────────────────────────────────────
@router.message(Command("sudolist", "sudousers", "slist"))
async def cmd_sudolist(message: Message):
    rows = access.list_role(access.ROLE_SUDO)
    if not rows:
        await message.answer(
            "📭 Koi SUDO user nahi.\n\nAdd:  <code>/addsudo UID</code>",
            parse_mode="HTML",
        )
        return
    lines = [f"  • {_user_label(r)}" for r in rows[:50]]
    await message.answer(
        f"⚡ <b>SUDO USERS ({len(rows)})</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── /code DAYS [MAX_USES] [notes] ───────────────────────────────────────────
@router.message(Command("code"))
async def cmd_code(message: Message):
    args = _parse_args(message.text or "")
    if not args or not args[0].lstrip("-").isdigit():
        await message.answer(
            "❌ <b>Usage:</b>  <code>/code DAYS [MAX_USES] [notes]</code>\n"
            "<i>DAYS=0 means permanent.</i>",
            parse_mode="HTML",
        )
        return
    days = int(args[0])
    max_uses = 1
    notes_start = 1
    if len(args) >= 2 and args[1].isdigit():
        max_uses = int(args[1])
        notes_start = 2
    notes = " ".join(args[notes_start:]) if len(args) > notes_start else ""
    if days < 0 or max_uses < 1:
        await message.answer("❌ DAYS>=0 aur MAX_USES>=1.", parse_mode="HTML")
        return
    code = access.create_code(message.from_user.id, days, max_uses, notes)
    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  🎟  NEW REDEEM CODE\n"
        f"╚══════════════════════════╝\n\n"
        f"<code>{code['code']}</code>\n\n"
        f"⏳ <b>Days:</b> <code>{days}</code>  ({'permanent' if days == 0 else f'{days}d'})\n"
        f"🎯 <b>Max uses:</b> <code>{max_uses}</code>\n"
        f"📝 <b>Notes:</b> {escape(notes) if notes else '—'}\n\n"
        f"<i>Share kar do — user </i><code>/redeem {code['code']}</code><i> bhejega.</i>",
        parse_mode="HTML",
    )


# ── /codes [all|active|used|expired] ────────────────────────────────────────
@router.message(Command("codes"))
async def cmd_codes(message: Message):
    args = _parse_args(message.text or "")
    f = (args[0].lower() if args else "active")
    if f not in {"all", "active", "used", "expired"}:
        f = "active"
    rows = access.list_codes(f)
    if not rows:
        await message.answer(f"📭 Koi <b>{f}</b> code nahi.", parse_mode="HTML")
        return
    lines = []
    for r in rows[:25]:
        used = f"{r.get('current_uses', 0)}/{r.get('max_uses', 1)}"
        days = r.get("duration_days", 0)
        d_str = "perm" if days == 0 else f"{days}d"
        active = "✅" if r.get("is_active") else "❌"
        lines.append(f"{active} <code>{r['code']}</code>  {d_str}  used={used}")
    await message.answer(
        f"🎟 <b>CODES — filter: {f} ({len(rows)})</b>\n\n" + "\n".join(lines)
        + ("\n\n<i>...truncated</i>" if len(rows) > 25 else ""),
        parse_mode="HTML",
    )


# ── /revokecode CODE ───────────────────────────────────────────────────────
@router.message(Command("revokecode"))
async def cmd_revokecode(message: Message):
    args = _parse_args(message.text or "")
    if not args:
        await message.answer("❌ <b>Usage:</b>  <code>/revokecode FCH-XXXX-XXXX-XXXX</code>", parse_mode="HTML")
        return
    code = access.normalize_code(args[0])
    if access.revoke_code(code, message.from_user.id):
        await message.answer(f"✅ Revoked: <code>{code}</code>", parse_mode="HTML")
    else:
        await message.answer(f"❌ Not found / already inactive: <code>{code}</code>", parse_mode="HTML")


# ── /ban UID  /unban UID  /banned ──────────────────────────────────────────
@router.message(Command("ban"))
async def cmd_ban(message: Message):
    target = _parse_target_uid(message.text or "")
    if not target:
        await message.answer("❌ <b>Usage:</b>  <code>/ban UID</code>", parse_mode="HTML")
        return
    if target == OWNER_ID or access.get_role(target, OWNER_ID) == access.ROLE_OWNER:
        await message.answer("❌ Owner ban nahi ho sakta.", parse_mode="HTML")
        return
    access.ban_user(target, message.from_user.id)
    await message.answer(f"🚫 Banned: <code>{target}</code>", parse_mode="HTML")


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    target = _parse_target_uid(message.text or "")
    if not target:
        await message.answer("❌ <b>Usage:</b>  <code>/unban UID</code>", parse_mode="HTML")
        return
    access.unban_user(target, message.from_user.id)
    await message.answer(f"✅ Unbanned: <code>{target}</code>", parse_mode="HTML")


@router.message(Command("banned"))
async def cmd_banned(message: Message):
    rows = access.list_banned()
    if not rows:
        await message.answer("📭 Koi banned user nahi.", parse_mode="HTML")
        return
    lines = [f"  • {_user_label(r)}" for r in rows[:50]]
    await message.answer(
        f"🚫 <b>BANNED ({len(rows)})</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── /userinfo UID ──────────────────────────────────────────────────────────
@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message):
    target = _parse_target_uid(message.text or "")
    if not target:
        await message.answer("❌ <b>Usage:</b>  <code>/userinfo UID</code>", parse_mode="HTML")
        return
    row = access.get_user(target)
    if not row:
        await message.answer(f"❌ Unknown user: <code>{target}</code>", parse_mode="HTML")
        return
    role = access.get_role(target, OWNER_ID)
    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  🪪  USER INFO\n"
        f"╚══════════════════════════╝\n\n"
        f"<b>UID    :</b> <code>{target}</code>\n"
        f"<b>Name   :</b> {escape(row.get('first_name') or '—')}\n"
        f"<b>User   :</b> @{escape(row.get('username') or '—')}\n"
        f"<b>Role   :</b> {_badge(role)}\n"
        f"<b>Expires:</b> <code>{access.fmt_expires(row.get('expires_at'))}</code>\n"
        f"<b>Remain :</b> <code>{access.fmt_remaining(row.get('expires_at'))}</code>\n"
        f"<b>Code   :</b> <code>{escape(row.get('redeem_code_used') or '—')}</code>\n"
        f"<b>Banned :</b> {'🚫 YES' if row.get('is_banned') else '✅ No'}",
        parse_mode="HTML",
    )


# ── /accstats ──────────────────────────────────────────────────────────────
@router.message(Command("accstats"))
async def cmd_accstats(message: Message):
    s = access.stats()
    await message.answer(
        f"╔══════════════════════════╗\n"
        f"  📊  ACCESS STATS\n"
        f"╚══════════════════════════╝\n\n"
        f"<b>Users   :</b>  <code>{s['users_total']}</code>\n"
        f"  👑 Owner   : <code>{s['by_role'].get('OWNER', 0)}</code>\n"
        f"  ⚡ Sudo    : <code>{s['by_role'].get('SUDO', 0)}</code>\n"
        f"  💎 Premium : <code>{s['by_role'].get('PREMIUM', 0)}</code>\n"
        f"  👤 Guest   : <code>{s['by_role'].get('GUEST', 0)}</code>\n"
        f"  🚫 Banned  : <code>{s['banned']}</code>\n\n"
        f"<b>Codes   :</b>  <code>{s['codes_total']}</code>\n"
        f"  ✅ Active   : <code>{s['codes_active']}</code>\n"
        f"  🎟 Redeemed : <code>{s['codes_redeemed']}</code>",
        parse_mode="HTML",
    )


# ── /audit (owner only) ─────────────────────────────────────────────────────
@router.message(Command("audit"))
async def cmd_audit(message: Message):
    import os as _os
    path = "/tmp/fetcher-audit.log"
    if not _os.path.exists(path):
        await message.answer("📭 Audit log abhi empty hai.", parse_mode="HTML")
        return
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4000))
            tail = f.read().decode("utf-8", errors="replace")
        # Last ~30 lines
        lines = tail.strip().splitlines()[-30:]
        body = "\n".join(escape(ln) for ln in lines) or "<i>—</i>"
    except Exception as ex:
        body = f"<i>read error: {escape(str(ex))}</i>"
    await message.answer(
        f"📜 <b>AUDIT LOG (last 30)</b>\n\n<pre>{body}</pre>",
        parse_mode="HTML",
    )
