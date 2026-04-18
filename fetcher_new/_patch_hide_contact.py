"""Patch: (1) add ✖️ Hide button + /hide handler, (2) strip Owner: line from contact."""
import re

p = "bot/handlers/menu_buttons.py"
s = open(p).read()

# ── 1. Add BTN_HIDE label + put it in ALL_BUTTON_LABELS ─────────────────
if "BTN_HIDE " not in s:
    s = s.replace(
        'BTN_HELP          = "\U0001f4d6 Help"',
        'BTN_HELP          = "\U0001f4d6 Help"\nBTN_HIDE          = "\u2716\ufe0f Hide Menu"',
        1,
    )
    s = s.replace(
        "BTN_CONTACT, BTN_HELP,\n    BTN_REDEEM_GUEST,",
        "BTN_CONTACT, BTN_HELP, BTN_HIDE,\n    BTN_REDEEM_GUEST,",
        1,
    )

# ── 2. Inject Hide row into every keyboard builder (premium/sudo/owner/guest) ─
# Premium: append [BTN_HIDE] row after Help
s = s.replace(
    "[KeyboardButton(text=BTN_HELP)],\n        ],\n        resize_keyboard=True,\n        is_persistent=True,\n        input_field_placeholder=\"Tap an option or paste a t.me/... link\",\n    )\n\n\ndef _kb_sudo",
    "[KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_HIDE)],\n        ],\n        resize_keyboard=True,\n        is_persistent=True,\n        input_field_placeholder=\"Tap an option or paste a t.me/... link\",\n    )\n\n\ndef _kb_sudo",
    1,
)
# Sudo: turn Help row into Help+Hide
s = s.replace(
    "[KeyboardButton(text=BTN_HELP)],\n            [KeyboardButton(text=BTN_SUDO_PANEL)],",
    "[KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_HIDE)],\n            [KeyboardButton(text=BTN_SUDO_PANEL)],",
    1,
)
# Owner: same
s = s.replace(
    "[KeyboardButton(text=BTN_HELP)],\n            [KeyboardButton(text=BTN_OWNER_PANEL)],",
    "[KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_HIDE)],\n            [KeyboardButton(text=BTN_OWNER_PANEL)],",
    1,
)
# Guest: append a small hide row
s = s.replace(
    "[KeyboardButton(text=BTN_CONTACT_GUEST), KeyboardButton(text=BTN_HELP)],\n        ],\n        resize_keyboard=True,\n        is_persistent=True,\n        input_field_placeholder=\"\U0001f512 Redeem a code or contact owner\",",
    "[KeyboardButton(text=BTN_CONTACT_GUEST), KeyboardButton(text=BTN_HELP)],\n            [KeyboardButton(text=BTN_HIDE)],\n        ],\n        resize_keyboard=True,\n        is_persistent=True,\n        input_field_placeholder=\"\U0001f512 Redeem a code or contact owner\",",
    1,
)

# ── 3. Add /hide command + button handler ───────────────────────────────
if "btn_hide" not in s:
    hide_block = '''

@router.message(Command("hide"))
async def cmd_hide(message: Message):
    await message.answer(
        "\u2716\ufe0f Menu hidden. Wapas lane ke liye /menu ya /start bhejein.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == BTN_HIDE)
async def btn_hide(message: Message):
    await message.answer(
        "\u2716\ufe0f Menu hidden. Wapas lane ke liye /menu ya /start bhejein.",
        reply_markup=ReplyKeyboardRemove(),
    )
'''
    # Insert before the Owner Panel handler section
    s = s.replace(
        "# \u2500\u2500 Owner Panel (inline keyboard with admin shortcuts) \u2500\u2500",
        hide_block + "\n# \u2500\u2500 Owner Panel (inline keyboard with admin shortcuts) \u2500\u2500",
        1,
    )

# ── 4. Strip "Owner: @..." line from btn_contact ────────────────────────
old_contact_body = (
    'await message.answer(\n'
    '        "\U0001f4de <b>Contact Owner</b>\\n\\n"\n'
    '        f"Owner: {owner_link}\\n\\n"\n'
    '        "<i>Access ya code request karne ke liye neeche button par tap karein.</i>",'
)
new_contact_body = (
    'await message.answer(\n'
    '        "\U0001f4de <b>Contact Owner</b>\\n\\n"\n'
    '        "<i>Access ya code request karne ke liye neeche button par tap karein.</i>",'
)
if old_contact_body in s:
    s = s.replace(old_contact_body, new_contact_body, 1)

open(p, "w").write(s)
print("OK patched")
