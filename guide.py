"""
UAV User Guide — multi-page A-to-Z documentation.

Provides:
- PAGES dict: structured content (icon, title, body) for each section
- index_text() / index_keyboard(): the table of contents view
- render_page(pid) / page_keyboard(pid): per-section views with prev/next
- guide_callback(): single CallbackQueryHandler for "guide:*" patterns
- register(app): wires the handler into the Telegram Application

Callback data convention:
    guide:open       → entry point (same as index)
    guide:idx        → show index page
    guide:p:<page_id> → show specific section
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, CallbackQueryHandler


# ── Section ordering (used for prev/next nav + page numbering) ──────────────
PAGES_ORDER = [
    "intro", "what", "benefits", "features", "setup",
    "howto", "tips", "faq", "troubleshoot", "support",
]


# ── Content ─────────────────────────────────────────────────────────────────
PAGES: dict[str, dict[str, str]] = {
    "intro": {
        "icon": "👋",
        "title": "WELCOME",
        "body": (
            "*UAV — URL Auto Visitor*\n"
            "_Real-browser visitor bot with Tor & proxy rotation_\n\n"
            "Salaam! Yeh bot aapke liye automated browser visits karta hai — "
            "bilkul real Chrome jaisa, full JavaScript support ke saath.\n\n"
            "🎯 *Kis ke liye?*\n"
            "• Web app testing aur QA loops\n"
            "• Site analytics dummy traffic se test karna\n"
            "• Anonymous browsing via Tor circuits\n"
            "• Click-flow automation (signup, link clicks, etc.)\n\n"
            "✨ *Khaas baat:*\n"
            "Aap chair pe baith ke phone se commands bhejein, EC2 server pe "
            "browser real visits karta rahega — chahe aap offline ho."
        ),
    },
    "what": {
        "icon": "💡",
        "title": "WHAT IS THIS BOT?",
        "body": (
            "*UAV ek Telegram-controlled visitor bot hai* jo aapki di hui URL pe "
            "Selenium-driven Chromium browser launch karta hai aur loops mein "
            "visit karta hai.\n\n"
            "🛠 *Yeh kya solve karta hai?*\n"
            "Aksar log dummy traffic, automated signups, ya click-testing ke liye "
            "scripts likhte hain. UAV yeh sab ek Telegram chat se control karta "
            "hai — koi local setup nahi.\n\n"
            "🎬 *Use cases:*\n"
            "1️⃣  Website warmup — naya site launch karke organic-jaisa traffic\n"
            "2️⃣  Form / signup automation — text ya CSS selector se click\n"
            "3️⃣  Anonymous testing — Tor circuit har visit pe naya IP\n"
            "4️⃣  Proxy verification — bulk proxies ka live check\n"
            "5️⃣  Long-running monitor — site availability ke liye background loops\n\n"
            "🔐 *Privacy:*\n"
            "Saara setup aapke EC2 box pe — koi 3rd-party cloud nahi."
        ),
    },
    "benefits": {
        "icon": "✨",
        "title": "KEY BENEFITS",
        "body": (
            "_Kyun yeh bot pasand kiya jata hai 👇_\n\n"
            "⚡ *Real Browser, Real Visits*\n"
            "_Selenium + Chromium — JS execute hota hai, analytics count karte hain_\n\n"
            "🧅 *Tor Anonymity Built-in*\n"
            "_New Identity ya New Circuit — har loop mein naya IP_\n\n"
            "🌐 *Smart Proxy Support*\n"
            "_HTTP, SOCKS4, SOCKS5 — auto-detect by port_\n\n"
            "🤖 *Auto Proxy Scraper*\n"
            "_Live proxies internet se khud dhundhta hai_\n\n"
            "🖱 *Two-Stage Click Flow*\n"
            "_Pehle button text se, phir CSS selector se click_\n\n"
            "📊 *Live Status Panel*\n"
            "_Progress bar, IP, error count — sab real-time_\n\n"
            "🛡 *Safe by Design*\n"
            "_Rate limits, audit log, maintenance mode — production-ready_\n\n"
            "📱 *100% Telegram Controlled*\n"
            "_Phone se sab kuch — koi SSH nahi chahiye_"
        ),
    },
    "features": {
        "icon": "🚀",
        "title": "FEATURES OVERVIEW",
        "body": (
            "*Sab features ek nazar mein:*\n\n"
            "『 🌍 *Visiting Engine* 』\n"
            "• Real Chromium browser (headless / headed)\n"
            "• JavaScript & cookies fully working\n"
            "• Page-load + element-wait timeouts\n\n"
            "『 🧅 *Tor Integration* 』\n"
            "• Auto-start Tor service if not running\n"
            "• New Identity (~10s, full IP change)\n"
            "• New Circuit (~3s, faster)\n\n"
            "『 🌐 *Proxy System* 』\n"
            "• Manual add (HTTP/SOCKS4/SOCKS5)\n"
            "• Bulk paste support\n"
            "• Live health check (single ya all)\n"
            "• Auto-scraper public sources se\n\n"
            "『 🖱 *Click Automation* 』\n"
            "• Click by visible text\n"
            "• Click by CSS selector\n"
            "• Two-stage flow (e.g. Open → Confirm)\n\n"
            "『 ⏱ *Loop Control* 』\n"
            "• Configurable loops, delays, timeouts\n"
            "• Coffee breaks every N loops\n\n"
            "『 📊 *Observability* 』\n"
            "• /s live panel  • /stats system\n"
            "• /logs file  • /diag browser pair\n"
            "• /health JSON endpoint\n\n"
            "『 🛡 *Admin (Owner-only)* 』\n"
            "• /maint — maintenance mode\n"
            "• /audit — sensitive actions log\n"
            "• /backup — SQLite snapshot"
        ),
    },
    "setup": {
        "icon": "🎯",
        "title": "GETTING STARTED",
        "body": (
            "*Bot pehli baar use karne ka 60-second guide:*\n\n"
            "*Step 1 — Bot ko start karein*\n"
            "`/start` bhejein — premium menu khulega.\n\n"
            "*Step 2 — Target URL set karein*\n"
            "`/url https://yoursite.com`\n"
            "✅ Confirmation milegi: \"URL set: https://...\"\n\n"
            "*Step 3 — Mode chunein*\n"
            "*Option A:* Tor mode (anonymous)\n"
            "   `/ton` → `/idon` (full IP change)\n"
            "*Option B:* Proxy mode\n"
            "   `/ap host:port` ya bulk paste\n"
            "   `/chk` se live verify karein\n\n"
            "*Step 4 — (Optional) Click flow*\n"
            "`/ct1 Sign Up` → button text se click\n"
            "`/ct2 Confirm` → second button\n\n"
            "*Step 5 — Timing tune*\n"
            "`/loops 100`  •  `/delay 5`  •  `/wait 5`\n\n"
            "*Step 6 — START!*\n"
            "`/run` → browser launch hota hai\n"
            "`/s` se live progress dekhein\n"
            "`/stop` se rok dein\n\n"
            "🎉 *Bas! Aap ready hain.*"
        ),
    },
    "howto": {
        "icon": "📖",
        "title": "HOW TO USE",
        "body": (
            "*Har command ka detail walkthrough:*\n\n"
            "『 🔗 */url <link>* 』\n"
            "_Purpose:_ Target page set karna\n"
            "_Example:_ `/url https://example.com/page`\n\n"
            "『 ➕ */ap <proxies>* 』\n"
            "_Purpose:_ Proxies add (bulk OK)\n"
            "_Format:_ host:port  •  host:port:user:pass  •  scheme://...\n\n"
            "『 🩺 */chk* 』\n"
            "_Purpose:_ Sab proxies live/dead check\n"
            "_Use:_ `/chk` (sab) ya `/chk 1.2.3.4:8080` (single)\n\n"
            "『 🟢 */ton* — */toff* 』\n"
            "_Purpose:_ Tor mode toggle\n"
            "_Note:_ /ton ke baad agar Tor band hai, auto-start hoga\n\n"
            "『 🆔 */idon* — ⛔ */idoff* 』\n"
            "_Purpose:_ Har loop mein NEW Identity\n"
            "_Speed:_ ~10s extra, full IP change\n\n"
            "『 🔀 */con* — ⛔ */coff* 』\n"
            "_Purpose:_ Har loop mein NEW Circuit\n"
            "_Speed:_ ~3s extra, faster than identity\n\n"
            "『 👆 */ct1* — */ct2 <text>* 』\n"
            "_Purpose:_ Visible button text se click\n"
            "_Example:_ `/ct1 Subscribe`\n\n"
            "『 🎯 */sel* — */sel2 <css>* 』\n"
            "_Purpose:_ Advanced CSS selector\n"
            "_Example:_ `/sel button.primary`\n\n"
            "『 🚀 */run*  •  🛑 */stop*  •  ♻️ */restart* 』\n"
            "_Purpose:_ Visiting loop control\n"
            "_Tip:_ /restart = stop + dobara start with same settings\n\n"
            "『 📡 */s* 』\n"
            "_Purpose:_ Live status (URL, mode, loops, IP, errors)"
        ),
    },
    "tips": {
        "icon": "💎",
        "title": "PRO TIPS & TRICKS",
        "body": (
            "_Power-user tricks 👇_\n\n"
            "💡 *Tip 1 — Dot prefix shortcut*\n"
            "`/run` ke jagah `.run` bhi kaam karta hai. Phone se faster typing.\n\n"
            "💡 *Tip 2 — Inline menu se navigate*\n"
            "`/menu` se categories tap karein — har section ki commands ek view mein.\n\n"
            "💡 *Tip 3 — Proxy + Tor hybrid nahi*\n"
            "Aap dono ek saath nahi use karte — aik chunein. Tor zyada anonymous, "
            "proxies zyada fast.\n\n"
            "💡 *Tip 4 — Coffee break pattern*\n"
            "Real users continuous nahi hote. `/bint 50` `/bwait 60` set karein — "
            "har 50 loops baad 1 min pause.\n\n"
            "💡 *Tip 5 — Two-stage click for paywalls*\n"
            "`/ct1 Open Article` → `/ct2 Continue Reading` → real flow simulate.\n\n"
            "💡 *Tip 6 — Settings auto-save*\n"
            "Sab settings SQLite mein save hoti hain. Bot restart ke baad bhi "
            "/run wahi pichli config use karega.\n\n"
            "💡 *Tip 7 — Owner weekly backup*\n"
            "`/backup` weekly chalayein — SQLite snapshot Telegram pe milta hai. "
            "Disaster recovery easy."
        ),
    },
    "faq": {
        "icon": "❓",
        "title": "FAQ",
        "body": (
            "*Q1: Bot kya record karta hai?*\n"
            "A: Sirf aapki settings (URL, proxies, click text) SQLite mein. "
            "Visit logs nahi.\n\n"
            "*Q2: Tor aur proxy ek saath?*\n"
            "A: Nahi — Tor mode ON hai to proxies ignore. Kisi ek ko chunein.\n\n"
            "*Q3: Kitne loops max?*\n"
            "A: `/loops 0` matlab infinite. Limit aapke EC2 RAM pe depend.\n\n"
            "*Q4: Kya browser visible hota hai?*\n"
            "A: Default headless (background). Real screen nahi dikhata.\n\n"
            "*Q5: Proxy fail ho jaaye to?*\n"
            "A: Auto-retry next proxy. `/chk` se pehle hi verify kar lein.\n\n"
            "*Q6: New Identity vs New Circuit kya farak?*\n"
            "A: Identity = full IP change (~10s). Circuit = same IP set se naya "
            "path (~3s).\n\n"
            "*Q7: Multi-user support?*\n"
            "A: Haan — har Telegram user ki settings alag SQLite mein save hoti.\n\n"
            "*Q8: Maintenance mode kya hota hai?*\n"
            "A: Owner /maint on karta hai → doosre users block, sirf /s /ping allowed.\n\n"
            "*Q9: Bot crash ho jaaye to?*\n"
            "A: systemd auto-restart karta hai. /health endpoint se uptime check.\n\n"
            "*Q10: Rate limit kyun hai?*\n"
            "A: /run, /restart, /chk per user 60s mein 3 baar — accidental spam se bachao."
        ),
    },
    "troubleshoot": {
        "icon": "🛠",
        "title": "TROUBLESHOOTING",
        "body": (
            "*Common issues + fixes:*\n\n"
            "⚠️ *\"Browser launch fail ho gaya\"*\n"
            "_Cause:_ Chromium/ChromeDriver mismatch\n"
            "_Fix:_ `/diag` chalao — pair status dekho. Snap+apt mismatch ho to "
            "ek ko match karo.\n\n"
            "⚠️ *\"Tor start nahi ho raha\"*\n"
            "_Cause:_ Service down ya port 9050 busy\n"
            "_Fix:_ SSH se `systemctl restart tor`. Phir /ton dobara.\n\n"
            "⚠️ *\"Sab proxies dead ho rahi\"*\n"
            "_Cause:_ Public proxies short-lived hote hain\n"
            "_Fix:_ `/clrp` → `/run` (auto-scraper fresh dhundh lega) ya quality "
            "private proxies use karein.\n\n"
            "⚠️ *\"403 Forbidden in logs\"*\n"
            "_Cause:_ Bot user ko message nahi bhej sakta\n"
            "_Fix:_ Normal hai — user ne /start nahi kiya hoga ya block kar diya.\n\n"
            "⚠️ *\"Click element not found\"*\n"
            "_Cause:_ Selector galat ya page abhi load nahi\n"
            "_Fix:_ `/wait 10` set karo. Ya /sel se exact CSS use karo.\n\n"
            "⚠️ *\"Rate limit message dikha\"*\n"
            "_Cause:_ 60s mein 3 baar /run ya /chk\n"
            "_Fix:_ Wait karein — message mein retry seconds bataya hota hai.\n\n"
            "⚠️ *\"Bot reply nahi de raha\"*\n"
            "_Cause:_ Maintenance mode ON ya service down\n"
            "_Fix:_ `/ping` try karo. Owner se confirm karo.\n\n"
            "🆘 *Last resort:* `/logs` bhejo developer ko."
        ),
    },
    "support": {
        "icon": "💬",
        "title": "SUPPORT & CONTACT",
        "body": (
            "*Madad chahiye?*\n\n"
            "📧 *Owner contact:*\n"
            "Apne bot owner se direct DM karein.\n\n"
            "🐛 *Bug report:*\n"
            "1. `/diag` ka output bhejo\n"
            "2. `/logs` se last lines attach karo\n"
            "3. Steps to reproduce mention karo\n\n"
            "⏱ *Response time:*\n"
            "Usually 24 hours ke andar. Critical bugs faster.\n\n"
            "📚 *Self-service resources:*\n"
            "• `/help` — full command list\n"
            "• `/menu` — categorized inline menu\n"
            "• Yeh guide — 10 sections cover karta hai\n\n"
            "🛡 *Security issue?*\n"
            "Audit log `/audit` (owner-only) check karo. Suspicious activity ho "
            "to bot off karo: `/maint on`.\n\n"
            "💎 *Feature request?*\n"
            "Owner ko suggest karein. Approved features next deploy mein add hoti hain.\n\n"
            "✨ *Thank you for using UAV!*\n"
            "_Aapka feedback bot ko behtar banata hai._"
        ),
    },
}


# ── Renderers ───────────────────────────────────────────────────────────────
_BORDER_TOP    = "╭━━━━━━━━━━━━━━━━━━━━━━╮"
_BORDER_BOTTOM = "╰━━━━━━━━━━━━━━━━━━━━━━╯"
_DIVIDER       = "━━━━━━━━━━━━━━━━━━━━━━"


def render_page(pid: str) -> str:
    """Render a single content page with header + body + page-number footer."""
    p = PAGES[pid]
    idx = PAGES_ORDER.index(pid) + 1
    total = len(PAGES_ORDER)
    return (
        f"{_BORDER_TOP}\n"
        f"   {p['icon']} *{p['title']}*\n"
        f"{_BORDER_BOTTOM}\n\n"
        f"{p['body']}\n\n"
        f"{_DIVIDER}\n"
        f"📄 _Page {idx} of {total}_\n"
        f"{_DIVIDER}"
    )


def index_text() -> str:
    """Render the table-of-contents page."""
    rows = []
    for i, pid in enumerate(PAGES_ORDER, 1):
        p = PAGES[pid]
        rows.append(f"{i:>2}.  {p['icon']}  *{p['title'].title()}*")
    return (
        f"{_BORDER_TOP}\n"
        f"   📖 *UAV — USER GUIDE* 📖\n"
        f"{_BORDER_BOTTOM}\n"
        f"_Complete A-to-Z documentation_\n\n"
        f"Niche kisi section pe tap karein:\n\n"
        + "\n".join(rows)
        + f"\n\n{_DIVIDER}\n"
        f"📑 _10 sections • multi-page guide_\n"
        f"{_DIVIDER}"
    )


# ── Keyboards ───────────────────────────────────────────────────────────────
def index_keyboard() -> InlineKeyboardMarkup:
    """Index page: 10 section buttons (2 per row) + back/close row."""
    rows = []
    items = [(pid, PAGES[pid]) for pid in PAGES_ORDER]
    for i in range(0, len(items), 2):
        row = []
        for pid, p in items[i:i + 2]:
            short = p["title"].split()[0].title()  # e.g. "FAQ" → "Faq"
            row.append(InlineKeyboardButton(
                f"{p['icon']} {short}",
                callback_data=f"guide:p:{pid}",
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🏠 Main Menu", callback_data="menu:back"),
        InlineKeyboardButton("❌ Close",     callback_data="menu:close"),
    ])
    return InlineKeyboardMarkup(rows)


def page_keyboard(pid: str) -> InlineKeyboardMarkup:
    """Content page: prev/index/next + main-menu/close."""
    idx = PAGES_ORDER.index(pid)
    n = len(PAGES_ORDER)
    prev_pid = PAGES_ORDER[(idx - 1) % n]
    next_pid = PAGES_ORDER[(idx + 1) % n]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Previous", callback_data=f"guide:p:{prev_pid}"),
            InlineKeyboardButton("📑 Index",    callback_data="guide:idx"),
            InlineKeyboardButton("➡️ Next",     callback_data=f"guide:p:{next_pid}"),
        ],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu:back"),
            InlineKeyboardButton("❌ Close",     callback_data="menu:close"),
        ],
    ])


# ── Callback handler ────────────────────────────────────────────────────────
async def guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatcher for guide:* callbacks."""
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()

    data = q.data
    if data in ("guide:open", "guide:idx"):
        text = index_text()
        kb = index_keyboard()
    elif data.startswith("guide:p:"):
        pid = data.split(":", 2)[2]
        if pid not in PAGES:
            return
        text = render_page(pid)
        kb = page_keyboard(pid)
    else:
        return

    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        # If edit fails (e.g. content unchanged or original is media), send new
        try:
            await q.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass


def register(app):
    """Register the guide callback handler with the Telegram Application."""
    app.add_handler(CallbackQueryHandler(guide_callback, pattern=r"^guide:"))
