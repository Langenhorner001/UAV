import re

p = "bot/handlers/menu_buttons.py"
s = open(p).read()

# Find and replace the entire btn_contact handler
pat = re.compile(
    r'@router\.message\(F\.text\.in_\(\{BTN_CONTACT, BTN_CONTACT_GUEST\}\)\)\s*'
    r'async def btn_contact\(message: Message\):.*?(?=\n@router|\nfrom aiogram\.types import CallbackQuery)',
    re.S,
)

new_block = '''@router.message(F.text.in_({BTN_CONTACT, BTN_CONTACT_GUEST}))
async def btn_contact(message: Message):
    # Resolve owner username: env override > Telegram API lookup
    username = (OWNER_USERNAME or "").lstrip("@")
    if not username and OWNER_ID:
        try:
            chat = await message.bot.get_chat(OWNER_ID)
            if chat.username:
                username = chat.username
        except Exception as ex:
            logger.warning(f"contact: get_chat({OWNER_ID}) failed: {ex}")

    if username:
        owner_link = f'<a href="https://t.me/{username}">@{username}</a>'
        btn_url = f"https://t.me/{username}"
    elif OWNER_ID:
        owner_link = f'<a href="tg://user?id={OWNER_ID}">Open Owner Profile</a>'
        btn_url = f"tg://user?id={OWNER_ID}"
    else:
        owner_link = "<i>not configured</i>"
        btn_url = None

    contact_kb = None
    if btn_url:
        contact_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="\\U0001f4ac Open Owner Chat", url=btn_url)
        ]])

    await message.answer(
        "\\U0001f4de <b>Contact Owner</b>\\n\\n"
        f"Owner: {owner_link}\\n\\n"
        "<i>Access ya code request karne ke liye neeche button par tap karein.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=contact_kb,
    )


'''

new_s, n = pat.subn(lambda m: new_block, s, count=1)
if n:
    open(p, "w").write(new_s)
    print("OK patched")
else:
    print("FAIL: pattern not found")
