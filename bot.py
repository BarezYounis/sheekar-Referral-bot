import os
import sqlite3
import logging
from contextlib import closing

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatInviteLink,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BotCommand,
    InputFile,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
    CallbackQueryHandler,
)
from telegram.error import BadRequest

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ref-contest")

# =========================
# Config
# =========================
BOT_TOKEN = "8236746541:AAGkaU9xrHKWJJGTxCRZnp2-gOK8a2MW8WQ"         # export BOT_TOKEN="123456:ABC..."
CHANNEL_ID = -1002517867246                      # your channel id (bot must be admin)
DB_PATH = "referrals.db"
START_IMAGE_URL = "https://ibb.co/4w7M14Cx"  # optional HTTPS image for /start
START_IMAGE_LOCAL = "start.jpg"  # fallback local file if URL not set
# =========================
# i18n (Central Kurdish / Sorani)
# =========================
I18N = {
    "ckb": {
        "start_title": "👋 بەخێربێیت بۆ پێشبڕکێی بانگهێشتکردنی شیکار!",
        "start_body": (
            "ئەم بۆتە یارمەتیدەدات لینکی تایبەت بە خۆت دروست بکەیت بەناو هاوڕێکانت بڵاوی بکەیتەوە دواتر ببیتە براوەی خەڵاتەکانی شیکار.\n\n"
            "🔘 دوگمەکان:\n"
            "• **/link** — لینکی تایبەت بە خۆت \n"
            "• **/mystats** — ئەو کەسانەی لە ڕێگای لینکەکەت هاتوونە ناو شیکار\n"
            "• **/leaderboard** — براوەکان \n\n"
            "تێبینی: ژماردن تەنها  بۆ یەک جارە—هەرکەس یەکجار هاتە ناو  کەناڵی شیکار هەر ئەو جارەی بۆ هەژمار دەکرێت ئەگەر بڕوات و دواتر بگەڕێتەوە هەژمار ناکرێت ."
	    "   .\n"
		"• دوا بەروار بۆ پێشبڕکێکە  12/10/2025  ڕۆژی یەکشەمە دەبێت، ڕۆژی دووشەمە براوەکان خەڵات دەکرێن\n"
        ),
        "your_link": "🔗 لینکی تایبەت بە خۆت:",
        "stats_title": "📊 ژمارەی ئەو کەسانەی بە لینکەکەت هاتونە کەناڵ: {count}",
        "stats_recent": "👥 ئەندامانی تازە:",
        "top_title": "🏆 لیستەی سەرەوەترین بانگهێشتەران:",
        "need_admin": "⚠️ دەبێت بۆتەکە ئەدمین بێت لە کەناڵ و مافی 'Invite users' هەبێت.",
        "join_btn": "➕ داخڵبوون بۆ کەناڵ",
        "btn_get_link": "🔗 لینکەکەم",
        "btn_my_stats": "📊 ئامارەکانم",
        "btn_top": "🏆 براوەکان",
    }
}
DEFAULT_LANG = "ckb"
def T(key, **kw):
    return I18N[DEFAULT_LANG].get(key, key).format(**kw) if kw else I18N[DEFAULT_LANG].get(key, key)

# =========================
# Reply Keyboard (3 commands)
# =========================
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("/link"), KeyboardButton("/mystats")],
        [KeyboardButton("/leaderboard")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    one_time_keyboard=False,
    selective=False,
)

# =========================
# DB
# =========================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        # Reliability
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        # Tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_links (
                invite_link TEXT PRIMARY KEY,
                chat_id INTEGER,
                owner_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(invite_link)
            )
        """)
        # Count only once per referred user (per channel)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_referrals (
                chat_id INTEGER,
                referrer_id INTEGER,
                referred_id INTEGER,
                invite_link TEXT,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, referred_id)
            )
        """)
        # Helpful indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON channel_referrals(chat_id, referrer_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_invite_links_owner  ON invite_links(chat_id, owner_id)")
        conn.commit()

def store_user(u):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_name) VALUES (?, ?, ?)",
            (u.id, u.username, u.first_name),
        )
        conn.commit()

# =========================
# Helpers
# =========================
async def ensure_personal_invite_link(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    # Return existing if already created
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT invite_link FROM invite_links WHERE chat_id=? AND owner_id=?",
            (CHANNEL_ID, user_id),
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]

    # Create NON-REQUEST (instant join) link
    try:
        cil: ChatInviteLink = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            name=f"ref-{user_id}",
            creates_join_request=False  # instant join
        )
    except Exception as e:
        raise RuntimeError(f"{T('need_admin')}\n\nDetails: {e}")

    inv = cil.invite_link
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "INSERT OR IGNORE INTO invite_links(invite_link, chat_id, owner_id) VALUES (?, ?, ?)",
            (inv, CHANNEL_ID, user_id),
        )
        conn.commit()
    return inv

# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome tutorial with image and quick-action buttons."""
    user = update.effective_user
    store_user(user)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(T("btn_get_link"), callback_data="tut_link"),
            InlineKeyboardButton(T("btn_my_stats"), callback_data="tut_stats"),
        ],
        [
            InlineKeyboardButton(T("btn_top"), callback_data="tut_top"),
        ]
    ])

    caption = f"{T('start_title')}\n\n{T('start_body')}"
    # Try URL image, else local file, else text only
    sent = False
    if START_IMAGE_URL:
        try:
            await update.message.reply_photo(photo=START_IMAGE_URL, caption=caption, reply_markup=kb)
            sent = True
        except Exception as e:
            logger.warning("Failed to send START_IMAGE_URL: %s", e)
    if not sent and os.path.isfile(START_IMAGE_LOCAL):
        try:
            with open(START_IMAGE_LOCAL, "rb") as f:
                await update.message.reply_photo(photo=InputFile(f), caption=caption, reply_markup=kb)
            sent = True
        except Exception as e:
            logger.warning("Failed to send local start.jpg: %s", e)
    if not sent:
        await update.message.reply_text(caption, reply_markup=MAIN_KB)

    # Always send the persistent reply keyboard once
    if update.message and sent:
        await update.message.reply_text("⬇️ فەرمانەکان لە خوارەوەن:", reply_markup=MAIN_KB)

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store_user(user)
    try:
        inv = await ensure_personal_invite_link(context, user.id)
    except RuntimeError as e:
        await update.message.reply_text(str(e), reply_markup=MAIN_KB)
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T("join_btn"), url=inv)]])
    await update.message.reply_text(
        f"{T('your_link')}\n{inv}\n\n"
        "⚠️ تکایە هەمان ئەم بەستەرە/دوگمەیە بەکاربێنە — "
        "ئەگەر بە لینکی گشتیی (@username) داخڵ بن، ژماردن ناکرێت.",
        reply_markup=kb
    )

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show ONLY this user's invitees, with names if known (counted once)."""
    uid = update.effective_user.id
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT COUNT(*) FROM channel_referrals WHERE chat_id=? AND referrer_id=?",
            (CHANNEL_ID, uid),
        )
        count = cur.fetchone()[0]

        cur.execute("""
            SELECT r.referred_id,
                   r.joined_at,
                   u.username,
                   u.first_name
            FROM channel_referrals r
            LEFT JOIN users u ON u.user_id = r.referred_id
            WHERE r.chat_id=? AND r.referrer_id=?
            ORDER BY r.joined_at DESC
            LIMIT 20
        """, (CHANNEL_ID, uid))
        rows = cur.fetchall()

    lines = [T("stats_title", count=count)]
    if rows:
        lines.append("\n" + T("stats_recent"))
        for referred_id, ts, uname, fname in rows:
            disp = f"@{uname}" if uname else (fname or str(referred_id))
            lines.append(f"• {disp} — {ts}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("""
            SELECT referrer_id, COUNT(*) AS c
            FROM channel_referrals
            WHERE chat_id=?
            GROUP BY referrer_id
            ORDER BY c DESC
            LIMIT 10
        """, (CHANNEL_ID,))
        rows = cur.fetchall()
    lines = [T("top_title")]
    if rows:
        for i, (ref, c) in enumerate(rows, start=1):
            with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
                cur.execute("SELECT username, first_name FROM users WHERE user_id=?", (ref,))
                u = cur.fetchone()
            name = f"@{u[0]}" if u and u[0] else (u[1] if u and u[1] else str(ref))
            lines.append(f"{i}. {name} — {c}")
    else:
        lines.append("—")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)
# =========================
# Tutorial inline buttons
# =========================
async def tut_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tutorial inline buttons by calling the same actions."""
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    # Route to the same logic as commands
    fake_message = update.effective_message  # we will reply in chat directly
    if data == "tut_link":
        await link_cmd(Update(update.update_id, message=fake_message), context)
    elif data == "tut_stats":
        await mystats_cmd(Update(update.update_id, message=fake_message), context)
    elif data == "tut_top":
        await leaderboard_cmd(Update(update.update_id, message=fake_message), context)

# =========================
# Channel updates (count once)
# =========================
async def on_channel_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu or cmu.chat.id != CHANNEL_ID:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    user = cmu.new_chat_member.user
    user_id = user.id

    store_user(user)

    NON_MEMBER_STATES = {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.KICKED,
    }
    joined_now = (old_status in NON_MEMBER_STATES) and (new_status == ChatMemberStatus.MEMBER)
    if not joined_now:
        return

    inv_obj = cmu.invite_link
    used_link = inv_obj.invite_link if isinstance(inv_obj, ChatInviteLink) else None
    if not used_link:
        return  # public username or unknown link → not attributable

    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT owner_id FROM invite_links WHERE invite_link=? AND chat_id=?",
            (used_link, CHANNEL_ID),
        )
        row = cur.fetchone()
        if not row:
            return
        referrer_id = row[0]
        if referrer_id == user_id:
            return  # ignore self-join

        # Extra guard: if already counted once, skip
        cur.execute(
            "SELECT 1 FROM channel_referrals WHERE chat_id=? AND referred_id=? LIMIT 1",
            (CHANNEL_ID, user_id),
        )
        if cur.fetchone():
            logger.info("Duplicate join ignored for referred_id=%s", user_id)
            return

        cur.execute("""
            INSERT OR IGNORE INTO channel_referrals(chat_id, referrer_id, referred_id, invite_link)
            VALUES (?, ?, ?, ?)
        """, (CHANNEL_ID, referrer_id, user_id, used_link))
        conn.commit()
        logger.info("Counted referral: referrer_id=%s referred_id=%s", referrer_id, user_id)

# =========================
# Post-init: register commands
# =========================
async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "ڕێنمایی دەستپێک + وێنە"),
            BotCommand("link", "بەستەری تایبەت + دوگمەی داخڵبوون"),
            BotCommand("mystats", "ئامارەکانت"),
            BotCommand("leaderboard", "سەرەوەترین بانگهێشتەران"),
        ])
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var (export BOT_TOKEN=...)")
    if not isinstance(CHANNEL_ID, int) or not str(CHANNEL_ID).startswith("-100"):
        raise RuntimeError("Set a valid CHANNEL_ID (int like -100xxxxxxxxxx)")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start",       start_cmd))
    app.add_handler(CommandHandler("link",        link_cmd))
    app.add_handler(CommandHandler("mystats",     mystats_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    # Tutorial inline buttons
    app.add_handler(CallbackQueryHandler(tut_router, pattern=r"^tut_(link|stats|top)$"))

    # Channel event: direct-join crediting (unique per referred user)
    app.add_handler(ChatMemberHandler(on_channel_member, ChatMemberHandler.CHAT_MEMBER))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

