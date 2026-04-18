"""Patch UAV bot.py to wire reply_keyboards module."""
import re

p = "bot.py"
s = open(p).read()

# ── 1. Add module import (after existing `import access` line) ──────────────
if "import reply_keyboards" not in s:
    # Find the line `import access` (top of file imports section)
    s = re.sub(
        r"(^import access\b)",
        r"\1\nimport reply_keyboards as _rkb",
        s,
        count=1,
        flags=re.M,
    )

# ── 2. Patch cmd_start: append persistent kb intro after welcome ────────────
old_start = (
    'async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):\n'
    '    uid = update.effective_user.id if update.effective_user else None\n'
    '    # Attach premium inline menu so 📖 guide + categories are one tap away\n'
    '    try:\n'
    '        kb = _menu_keyboard(uid)\n'
    '    except Exception:\n'
    '        kb = None\n'
    '    await update.message.reply_text(_help_for(uid), parse_mode="Markdown", reply_markup=kb)'
)
new_start = (
    'async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):\n'
    '    uid = update.effective_user.id if update.effective_user else None\n'
    '    # Attach premium inline menu so 📖 guide + categories are one tap away\n'
    '    try:\n'
    '        kb = _menu_keyboard(uid)\n'
    '    except Exception:\n'
    '        kb = None\n'
    '    await update.message.reply_text(_help_for(uid), parse_mode="Markdown", reply_markup=kb)\n'
    '    # Persistent bottom keyboard (role-aware)\n'
    '    try:\n'
    '        await update.message.reply_text(\n'
    '            "📋 *Menu loaded* — niche tap karein ya commands use karein.",\n'
    '            parse_mode="Markdown",\n'
    '            reply_markup=_rkb.kb_for_user(uid, OWNER_ID),\n'
    '        )\n'
    '    except Exception:\n'
    '        pass'
)
if old_start in s and "Persistent bottom keyboard" not in s:
    s = s.replace(old_start, new_start, 1)

# ── 3. Add cmd_hide + button_dispatcher just before the registration block ──
# Sentinel: the line `# Register all "/cmd" handlers`
hide_block = '''

# ── Persistent reply keyboard helpers (bottom menu) ─────────────────────────
async def cmd_hide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hide the persistent reply keyboard."""
    from telegram import ReplyKeyboardRemove
    await update.message.reply_text(
        "✖️ Menu hidden. Wapas lane ke liye /menu ya /start bhejein.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cmd_menukb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-open the persistent reply keyboard (without inline tiles)."""
    uid = update.effective_user.id if update.effective_user else None
    role = access.get_role(uid, OWNER_ID) if uid else access.ROLE_GUEST
    badge = {
        access.ROLE_OWNER: "👑 OWNER", access.ROLE_SUDO: "⚡ SUDO",
        access.ROLE_PREMIUM: "💎 PREMIUM", access.ROLE_GUEST: "👤 GUEST",
    }.get(role, role)
    await update.message.reply_text(
        f"📋 *MAIN MENU*\\n\\n*Your role:* {badge}\\n\\n_Tap any button below._",
        parse_mode="Markdown",
        reply_markup=_rkb.kb_for_user(uid, OWNER_ID),
    )


async def _btn_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route persistent-keyboard button taps to existing handlers."""
    if not update.message or not update.message.text:
        return
    text = update.message.text
    uid = update.effective_user.id if update.effective_user else None

    # Hide
    if text == _rkb.BTN_HIDE:
        return await cmd_hide(update, context)

    # Premium-tier actions
    if text == _rkb.BTN_START:
        return await cmd_run(update, context)
    if text == _rkb.BTN_STOP:
        return await cmd_stop(update, context)
    if text == _rkb.BTN_STATUS:
        return await cmd_status(update, context)
    if text == _rkb.BTN_STATS:
        return await cmd_stats(update, context)
    if text == _rkb.BTN_MYACCESS:
        return await cmd_myaccess(update, context)
    if text in (_rkb.BTN_REDEEM, _rkb.BTN_REDEEM_GUEST):
        return await update.message.reply_text(
            "🎟 *Redeem a code*\\n\\nBhejein:\\n`/redeem UAV-XXXX-XXXX-XXXX`\\n\\n"
            "_Owner ya SUDO se code mangwa lijiye._",
            parse_mode="Markdown",
        )
    if text == _rkb.BTN_HELP:
        return await cmd_help(update, context)

    # Contact owner (guest button)
    if text == _rkb.BTN_CONTACT_GUEST:
        username = (OWNER_USERNAME or "").lstrip("@")
        if not username and OWNER_ID:
            try:
                chat = await context.bot.get_chat(OWNER_ID)
                if chat.username:
                    username = chat.username
            except Exception:
                pass
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        if username:
            url = f"https://t.me/{username}"
        elif OWNER_ID:
            url = f"tg://user?id={OWNER_ID}"
        else:
            url = None
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open Owner Chat", url=url)]]) if url else None
        return await update.message.reply_text(
            "📞 *Contact Owner*\\n\\n_Access ya code request karne ke liye neeche button par tap karein._",
            parse_mode="Markdown",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    # Owner / Sudo panels — open the existing inline menu
    if text in (_rkb.BTN_OWNER_PANEL, _rkb.BTN_SUDO_PANEL):
        return await cmd_menu(update, context)


'''

if "_btn_dispatcher" not in s:
    marker = '    # Register all "/cmd" handlers'
    s = s.replace(marker, hide_block + marker, 1)

# ── 4. Register cmd_hide + button text MessageHandler in setup ──────────────
# Add ("hide", cmd_hide) into the _COMMANDS list (just before the ENH-002 group)
if '("hide",' not in s:
    s = s.replace(
        '        # ── ENH-002/003/004/005: new admin + UX commands ──',
        '        ("hide",           cmd_hide),\n'
        '        # ── ENH-002/003/004/005: new admin + UX commands ──',
        1,
    )

# Register the dispatcher MessageHandler. Insert AFTER the "/cmd handlers" loop
# but BEFORE the dot-prefix handler so button text wins.
disp_register = '''
    # ── Persistent reply-keyboard button dispatcher ──
    # Build a regex of escaped button labels and route taps to _btn_dispatcher.
    import re as _re_pk
    _btn_pattern = "^(" + "|".join(_re_pk.escape(b) for b in _rkb.ALL_BUTTON_LABELS) + ")$"
    app.add_handler(MessageHandler(filters.Regex(_btn_pattern), _btn_dispatcher))

'''
if "_btn_pattern" not in s:
    s = s.replace(
        '    # ── Dot-prefix dispatcher: ".cmd args" works exactly like "/cmd args" ──',
        disp_register + '    # ── Dot-prefix dispatcher: ".cmd args" works exactly like "/cmd args" ──',
        1,
    )

open(p, "w").write(s)
print("OK patched")
