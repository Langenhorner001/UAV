"""One-shot patch: wire menu_buttons into commands.py, middleware, access.py, main.py."""
import re

# 1) commands.py /start ── attach role-aware keyboard
p = "bot/handlers/commands.py"
s = open(p).read()
old = '@router.message(CommandStart())\nasync def cmd_start(message: Message):\n    await message.answer(WELCOME_TEXT, parse_mode="HTML")'
new = (
    '@router.message(CommandStart())\n'
    'async def cmd_start(message: Message):\n'
    '    from bot.handlers.menu_buttons import kb_for_message\n'
    '    await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_for_message(message))'
)
if old in s:
    open(p, "w").write(s.replace(old, new, 1))
    print("✅ commands.py /start patched")
else:
    print("⚠️  commands.py /start: pattern not found (already patched?)")

# 2) middleware_access.py ── whitelist menu button labels
p = "bot/middleware_access.py"
s = open(p).read()
marker = "if role == access.ROLE_GUEST and cmd is None:"
inject = (
    "# Allow persistent reply-keyboard button labels through to the menu router\n"
    "        try:\n"
    "            from bot.handlers.menu_buttons import ALL_BUTTON_LABELS as _MENU_BTNS\n"
    "        except Exception:\n"
    "            _MENU_BTNS = frozenset()\n"
    "        if cmd is None and event.text in _MENU_BTNS:\n"
    "            return await handler(event, data)\n\n"
    "        "
)
if "_MENU_BTNS" not in s and marker in s:
    open(p, "w").write(s.replace(marker, inject + marker, 1))
    print("✅ middleware whitelist injected")
else:
    print("⚠️  middleware: already patched or marker missing")

# 3) access.py /redeem ── refresh keyboard on success
p = "bot/handlers/access.py"
s = open(p).read()
if "kb_for_message" not in s:
    # add import
    if "from bot.handlers.menu_buttons" not in s:
        s = s.replace(
            "from bot.config import OWNER_ID",
            "from bot.config import OWNER_ID\nfrom bot.handlers.menu_buttons import kb_for_message as _kb_for_message",
            1,
        )
    # append kb refresh at end of cmd_redeem body (before next @router decorator)
    pat = re.compile(r"(async def cmd_redeem\(message: Message\):.*?)(\n@router\.message)", re.S)
    def _inject(mo):
        body = mo.group(1)
        tail = (
            "\n    try:\n"
            "        await message.answer(\"⌨️ Menu updated.\", reply_markup=_kb_for_message(message))\n"
            "    except Exception:\n"
            "        pass\n"
        )
        return body + tail + mo.group(2)
    new_s = pat.sub(_inject, s, count=1)
    if new_s != s:
        open(p, "w").write(new_s)
        print("✅ /redeem keyboard refresh injected")
    else:
        print("⚠️  /redeem: regex did not match")
else:
    print("⚠️  /redeem: already patched")

# 4) main.py ── register menu router
p = "main.py"
s = open(p).read()
if "menu_buttons" not in s:
    s = s.replace(
        "from bot.handlers import access as access_handlers",
        "from bot.handlers import access as access_handlers\nfrom bot.handlers import menu_buttons as menu_handlers",
        1,
    )
    # insert include_router(menu_handlers.router) right after commands router include
    new_s, n = re.subn(
        r"(dp\.include_router\(commands\.router\)\s*\n)",
        r"\1    dp.include_router(menu_handlers.router)\n",
        s,
        count=1,
    )
    if n == 0:
        # fallback: try without indent
        new_s, n = re.subn(
            r"(dp\.include_router\(commands\.router\)\s*\n)",
            r"\1dp.include_router(menu_handlers.router)\n",
            s,
            count=1,
        )
    if n:
        open(p, "w").write(new_s)
        print("✅ main.py menu router registered")
    else:
        print("⚠️  main.py: commands.router include not found")
else:
    print("⚠️  main.py: already has menu_buttons")

print("=== DONE ===")
