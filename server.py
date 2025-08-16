import os, re, sqlite3, asyncio, httpx, time
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, AIORateLimiter,
)

# ---------- Config ----------
load_dotenv(".env", override=True)

LIST_URL = "https://mostaql.com/projects?category=development"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
}
POLL = int(os.getenv("POLL_INTERVAL_SECONDS", "45"))
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.path.join(os.path.dirname(__file__), "state.sqlite3")

# ---------- Helpers ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY)")
    return conn

def mark_seen(conn, url):
    conn.execute("INSERT OR IGNORE INTO seen(url) VALUES (?)", (url,))
    conn.commit()

def seen(conn, url) -> bool:
    cur = conn.execute("SELECT 1 FROM seen WHERE url=?", (url,))
    return cur.fetchone() is not None

def sel_text(soup, selectors):
    for css in selectors:
        el = soup.select_one(css)
        if el:
            return " ".join(el.get_text(" ", strip=True).split())
    return ""

def budget_strictly_over_500(text: str) -> bool:
    # e.g. "$250 - $750", "500 - 1000$", "USD 700"
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return bool(nums and max(nums) > 500)

def fetch_detail(url: str, client: httpx.Client):
    r = client.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title = sel_text(soup, ["div.page-title h1 span", "h1 span", "h1"]) or "(بدون عنوان)"
    budget = sel_text(soup, [
        "#project-meta-panel .meta-value span",
        "#project-meta-panel .meta-value",
        ".project-card .text-center span",
    ])
    owner = sel_text(soup, [
        "#project-users\\  h5 bdi",  # id with trailing space
        "#project-users h5 bdi",
        ".user-card h5 bdi",
        ".user-card .user-name",
        ".username bdi",
        ".username a",
    ]) or "(غير مذكور)"

    details_container = soup.select_one("#projectDetailsTab > div > div") \
                        or soup.select_one("#projectDetailsTab") \
                        or soup.select_one("div.project-card")
    if details_container:
        ps = [p.get_text(" ", strip=True) for p in details_container.select("p")]
        description = " ".join(ps[:8]) if ps else details_container.get_text(" ", strip=True)
    else:
        description = ""

    return {"title": title, "budget": budget, "owner": owner, "description": description, "url": url}

def fetch_links():
    resp = httpx.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Works from your tests: h2 a inside table rows
    links = [a.get("href") for a in soup.select("table tbody tr h2 a[href]")]
    links = [u if u.startswith("http") else f"https://mostaql.com{u}" for u in links]
    return links

# ---------- Telegram Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ البوت يعمل. سأرسل المشاريع الجديدة تلقائيًا.")

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, url = q.data.split("|", 1)
    # Here you can queue an auto-prefill worker later if you want
    if action == "approve":
        await q.edit_message_text(q.message.text + "\n\n✅ تم اعتماد العرض.")
    else:
        await q.edit_message_text(q.message.text + "\n\n❌ تم رفض المشروع.")

# ---------- Periodic Watcher (JobQueue) ----------
async def watcher_job(context: ContextTypes.DEFAULT_TYPE):
    conn = context.application.bot_data["db"]
    bot = context.bot

    try:
        links = fetch_links()
        # optional: newest first
        for url in links[:25]:
            if seen(conn, url):
                continue

            with httpx.Client(headers=HEADERS, timeout=30) as client:
                d = fetch_detail(url, client)

            # Filter: only budget > $500
            if not budget_strictly_over_500(d["budget"]):
                mark_seen(conn, url)
                continue

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ موافقة", callback_data=f"approve|{d['url']}"),
                InlineKeyboardButton("❌ رفض",   callback_data=f"reject|{d['url']}"),
            ],[
                InlineKeyboardButton("🔗 فتح المشروع", url=d["url"])
            ]])

            text = (
                f"📢 {d['title']}\n"
                f"💰 الميزانية: {d['budget']}\n"
                f"👤 العميل: {d['owner']}\n"
                f"🔗 {d['url']}\n\n"
                f"{(d['description'] or '')[:600]}..."
            )
            await bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=kb)
            mark_seen(conn, url)

    except Exception as e:
        # Soft log to Telegram so you know it's alive
        try:
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ watcher error: {e}")
        except:
            pass

# ---------- Main ----------
def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    app = Application.builder().token(TOKEN).rate_limiter(AIORateLimiter()).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))

    # shared db connection
    app.bot_data["db"] = db()

    # run watcher every POLL seconds
    app.job_queue.run_repeating(watcher_job, interval=POLL, first=3)

    # blocking call; handles long-polling
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
