"""
Crypto Alpha Hunter Bot
- Feature 1: Auto-discover new crypto accounts + pin to track posts/follows
- Feature 2: Search new accounts by keyword (under 7 or 30 days)
- Feature 3: Free mint tracker — finds NFT free mint announcements
- Feature 4: Established account tracker — any age, manual review
- Platform: Twitter/X via twscrape (no API key needed)
- Alerts: Instant via Telegram
"""

import os
import sqlite3
import logging
import asyncio
import re
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
import twscrape
from twscrape import API, gather
from twscrape.logger import set_log_level

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
set_log_level("ERROR")

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
X_USERNAME = os.environ.get("X_USERNAME")
X_PASSWORD = os.environ.get("X_PASSWORD")
X_EMAIL = os.environ.get("X_EMAIL")
DB_PATH = "cryptoalpha.db"

# Age thresholds
NEW_ACCOUNT_DAYS = 30
VERY_NEW_ACCOUNT_DAYS = 7
FREE_MINT_DAYS = 30

# Free mint keywords
FREE_MINT_KEYWORDS = [
    "free mint", "freemint", "free nft", "free drop", "0 eth mint",
    "free claim", "whitelist free", "public free", "mint free",
    "0 cost mint", "no cost mint", "free wl"
]

# Crypto discovery keywords
CRYPTO_KEYWORDS = [
    "blockchain", "defi", "nft", "web3", "crypto project",
    "token launch", "mainnet", "testnet", "airdrop", "protocol"
]

# ── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Search terms
    c.execute("""
        CREATE TABLE IF NOT EXISTS search_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT UNIQUE NOT NULL,
            feature INTEGER NOT NULL,
            age_filter TEXT DEFAULT '30days',
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Pinned accounts to monitor
    c.execute("""
        CREATE TABLE IF NOT EXISTS pinned_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            user_id TEXT,
            last_tweet_id TEXT,
            last_following_count INTEGER DEFAULT 0,
            pinned_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Seen accounts (to avoid duplicate alerts)
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            feature INTEGER NOT NULL,
            seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, feature)
        )
    """)

    # Seen tweets from pinned accounts
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_tweets (
            tweet_id TEXT PRIMARY KEY,
            username TEXT,
            seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

def add_search_term(term, feature, age_filter="30days"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO search_terms (term, feature, age_filter) VALUES (?, ?, ?)",
                  (term, feature, age_filter))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def remove_search_term(term):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM search_terms WHERE LOWER(term) = LOWER(?)", (term,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_search_terms(feature=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if feature:
        c.execute("SELECT term, age_filter FROM search_terms WHERE feature=?", (feature,))
    else:
        c.execute("SELECT term, feature, age_filter FROM search_terms")
    rows = c.fetchall()
    conn.close()
    return rows

def add_pinned(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO pinned_accounts (username) VALUES (?)", (username.lower(),))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def remove_pinned(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM pinned_accounts WHERE LOWER(username) = LOWER(?)", (username,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_pinned():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, user_id, last_tweet_id, last_following_count FROM pinned_accounts")
    rows = c.fetchall()
    conn.close()
    return rows

def update_pinned(username, last_tweet_id=None, following_count=None, user_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if last_tweet_id:
        c.execute("UPDATE pinned_accounts SET last_tweet_id=? WHERE LOWER(username)=LOWER(?)",
                  (last_tweet_id, username))
    if following_count is not None:
        c.execute("UPDATE pinned_accounts SET last_following_count=? WHERE LOWER(username)=LOWER(?)",
                  (following_count, username))
    if user_id:
        c.execute("UPDATE pinned_accounts SET user_id=? WHERE LOWER(username)=LOWER(?)",
                  (user_id, username))
    conn.commit()
    conn.close()

def is_seen(username, feature):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen_accounts WHERE LOWER(username)=LOWER(?) AND feature=?",
              (username, feature))
    found = c.fetchone() is not None
    conn.close()
    return found

def mark_seen(username, feature):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO seen_accounts (username, feature) VALUES (?, ?)",
                  (username.lower(), feature))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

def is_tweet_seen(tweet_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen_tweets WHERE tweet_id=?", (tweet_id,))
    found = c.fetchone() is not None
    conn.close()
    return found

def mark_tweet_seen(tweet_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO seen_tweets (tweet_id, username) VALUES (?, ?)",
                  (tweet_id, username))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

# ── TWSCRAPE SETUP ────────────────────────────────────────────────────────────
api = API()

async def setup_account():
    await api.pool.add_account(X_USERNAME, X_PASSWORD, X_EMAIL, X_PASSWORD)
    await api.pool.login_all()

def account_age_days(created_at):
    now = datetime.now(timezone.utc)
    return (now - created_at).days

def format_account_alert(user, feature_label, extra=""):
    age = account_age_days(user.created)
    url = f"https://x.com/{user.username}"
    return (
        f"🚨 *{feature_label}*\n\n"
        f"👤 [@{user.username}]({url})\n"
        f"📝 {user.rawDescription[:100] if user.rawDescription else 'No bio'}...\n"
        f"📅 Account age: *{age} days old*\n"
        f"👥 Followers: {user.followersCount:,}\n"
        f"🐦 Following: {user.friendsCount:,}\n"
        f"📨 Tweets: {user.statusesCount:,}"
        + (f"\n{extra}" if extra else "")
    )

# ── FEATURE 1: AUTO DISCOVER NEW CRYPTO ACCOUNTS ─────────────────────────────
async def run_feature1(bot: Bot):
    terms = get_search_terms(feature=1)
    if not terms:
        return

    for term, age_filter in terms:
        try:
            tweets = await gather(api.search(f"{term} -is:retweet", limit=20))
            seen_users = set()

            for tweet in tweets:
                user = tweet.user
                if not user or user.username in seen_users:
                    continue
                seen_users.add(user.username)

                age = account_age_days(user.created)
                if age > NEW_ACCOUNT_DAYS:
                    continue
                if is_seen(user.username, 1):
                    continue

                mark_seen(user.username, 1)
                msg = format_account_alert(
                    user,
                    "🆕 NEW CRYPTO ACCOUNT FOUND",
                    f"🔍 Found via: `{term}`\n"
                    f"💬 Tweet: {tweet.rawContent[:120]}...\n\n"
                    f"👉 To track: /pin {user.username}"
                )
                await bot.send_message(
                    chat_id=CHAT_ID, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Feature 1 error ({term}): {e}")

# ── FEATURE 2: SEARCH NEW ACCOUNTS BY KEYWORD ────────────────────────────────
async def run_feature2(bot: Bot):
    terms = get_search_terms(feature=2)
    if not terms:
        return

    for term, age_filter in terms:
        days_limit = VERY_NEW_ACCOUNT_DAYS if age_filter == "7days" else NEW_ACCOUNT_DAYS
        label = f"{'⚡ UNDER 7 DAYS' if days_limit == 7 else '🆕 UNDER 30 DAYS'}"

        try:
            tweets = await gather(api.search(f"{term} -is:retweet", limit=30))
            seen_users = set()

            for tweet in tweets:
                user = tweet.user
                if not user or user.username in seen_users:
                    continue
                seen_users.add(user.username)

                age = account_age_days(user.created)
                if age > days_limit:
                    continue
                if is_seen(user.username, 2):
                    continue

                mark_seen(user.username, 2)
                msg = format_account_alert(
                    user,
                    f"{label} ACCOUNT — KEYWORD MATCH",
                    f"🔍 Search term: `{term}`\n"
                    f"💬 Tweet: {tweet.rawContent[:120]}..."
                )
                await bot.send_message(
                    chat_id=CHAT_ID, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Feature 2 error ({term}): {e}")

# ── FEATURE 3: FREE MINT TRACKER ─────────────────────────────────────────────
async def run_feature3(bot: Bot):
    for keyword in FREE_MINT_KEYWORDS:
        try:
            tweets = await gather(api.search(f'"{keyword}" -is:retweet', limit=15))
            seen_users = set()

            for tweet in tweets:
                user = tweet.user
                if not user or user.username in seen_users:
                    continue
                seen_users.add(user.username)

                age = account_age_days(user.created)
                if age > FREE_MINT_DAYS:
                    continue
                if is_seen(user.username, 3):
                    continue

                mark_seen(user.username, 3)
                msg = format_account_alert(
                    user,
                    "🎨 FREE MINT ALERT",
                    f"🔑 Keyword: `{keyword}`\n"
                    f"💬 Tweet: {tweet.rawContent[:150]}...\n"
                    f"🔗 Tweet link: https://x.com/{user.username}/status/{tweet.id}"
                )
                await bot.send_message(
                    chat_id=CHAT_ID, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Feature 3 error ({keyword}): {e}")

# ── FEATURE 4: ESTABLISHED ACCOUNT TRACKER ───────────────────────────────────
async def run_feature4(bot: Bot):
    terms = get_search_terms(feature=4)
    if not terms:
        return

    for term, age_filter in terms:
        try:
            tweets = await gather(api.search(f"{term} -is:retweet", limit=25))
            seen_users = set()

            for tweet in tweets:
                user = tweet.user
                if not user or user.username in seen_users:
                    continue
                seen_users.add(user.username)

                if is_seen(user.username, 4):
                    continue

                mark_seen(user.username, 4)
                age = account_age_days(user.created)
                msg = format_account_alert(
                    user,
                    "🔎 ESTABLISHED ACCOUNT — REVIEW",
                    f"🔍 Search term: `{term}`\n"
                    f"💬 Tweet: {tweet.rawContent[:120]}...\n\n"
                    f"👉 To track: /pin {user.username}"
                )
                await bot.send_message(
                    chat_id=CHAT_ID, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Feature 4 error ({term}): {e}")

# ── PINNED ACCOUNT MONITOR ────────────────────────────────────────────────────
async def monitor_pinned(bot: Bot):
    pinned = get_pinned()
    if not pinned:
        return

    for username, user_id, last_tweet_id, last_following in pinned:
        try:
            # Check new tweets
            user_obj = await api.user_by_login(username)
            if not user_obj:
                continue

            update_pinned(username, user_id=str(user_obj.id))

            tweets = await gather(api.user_tweets(user_obj.id, limit=5))
            for tweet in tweets:
                if is_tweet_seen(str(tweet.id)):
                    continue
                mark_tweet_seen(str(tweet.id), username)

                msg = (
                    f"📌 *PINNED ACCOUNT POSTED*\n\n"
                    f"👤 [@{username}](https://x.com/{username})\n"
                    f"💬 {tweet.rawContent[:200]}\n"
                    f"🔗 https://x.com/{username}/status/{tweet.id}"
                )
                await bot.send_message(
                    chat_id=CHAT_ID, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )

            # Check following count change
            current_following = user_obj.friendsCount
            if last_following and current_following > last_following:
                new_follows = current_following - last_following
                msg = (
                    f"👀 *PINNED ACCOUNT FOLLOWED {new_follows} new account(s)*\n\n"
                    f"👤 [@{username}](https://x.com/{username})\n"
                    f"🐦 Now following: {current_following:,}"
                )
                await bot.send_message(
                    chat_id=CHAT_ID, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )

            update_pinned(username, following_count=current_following)
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Pinned monitor error ({username}): {e}")

# ── MASTER SCAN ───────────────────────────────────────────────────────────────
async def run_all_scans(bot: Bot):
    logger.info("Running all scans...")
    await monitor_pinned(bot)
    await run_feature1(bot)
    await run_feature2(bot)
    await run_feature3(bot)
    await run_feature4(bot)

# ── TELEGRAM COMMANDS ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🔍 *Crypto Alpha Hunter Bot*\n\n"
        "Tracks new crypto projects, free mints, and pinned accounts on X.\n\n"
        "*Feature 1 — New Crypto Account Discovery:*\n"
        "/addterm1 `<keyword>` — Add search term\n\n"
        "*Feature 2 — Keyword Account Search:*\n"
        "/addterm2 `<keyword>` `<7days or 30days>` — Add search term\n\n"
        "*Feature 3 — Free Mint Tracker:*\n"
        "Auto-runs. No setup needed.\n\n"
        "*Feature 4 — Established Account Tracker:*\n"
        "/addterm4 `<keyword>` — Add search term\n\n"
        "*Pinned Accounts:*\n"
        "/pin `<@handle>` — Pin account to monitor posts + follows\n"
        "/unpin `<@handle>` — Unpin account\n"
        "/listpinned — See all pinned accounts\n\n"
        "*General:*\n"
        "/listterms — See all search terms\n"
        "/removeterm `<term>` — Remove a search term\n"
        "/scan — Run all scans right now\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_addterm1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addterm1 solana defi")
        return
    term = " ".join(context.args)
    added = add_search_term(term, feature=1, age_filter="30days")
    if added:
        await update.message.reply_text(f"✅ Added '{term}' to Feature 1 (new accounts, under 30 days).")
    else:
        await update.message.reply_text(f"'{term}' already exists.")

async def cmd_addterm2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addterm2 base chain 7days\nor /addterm2 base chain 30days")
        return
    args = context.args
    age_filter = "30days"
    if args[-1] in ["7days", "30days"]:
        age_filter = args[-1]
        term = " ".join(args[:-1])
    else:
        term = " ".join(args)

    added = add_search_term(term, feature=2, age_filter=age_filter)
    if added:
        await update.message.reply_text(f"✅ Added '{term}' to Feature 2 (filter: {age_filter}).")
    else:
        await update.message.reply_text(f"'{term}' already exists.")

async def cmd_addterm4(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addterm4 ethereum nft project")
        return
    term = " ".join(context.args)
    added = add_search_term(term, feature=4, age_filter="any")
    if added:
        await update.message.reply_text(f"✅ Added '{term}' to Feature 4 (established accounts).")
    else:
        await update.message.reply_text(f"'{term}' already exists.")

async def cmd_removeterm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeterm solana defi")
        return
    term = " ".join(context.args)
    removed = remove_search_term(term)
    if removed:
        await update.message.reply_text(f"🗑️ Removed '{term}'.")
    else:
        await update.message.reply_text(f"'{term}' not found.")

async def cmd_listterms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    terms = get_search_terms()
    if not terms:
        await update.message.reply_text("No search terms yet.")
        return
    feature_labels = {1: "Feature 1 (New)", 2: "Feature 2 (Keyword)", 4: "Feature 4 (Established)"}
    msg = "*Search Terms:*\n\n"
    for term, feature, age_filter in terms:
        label = feature_labels.get(feature, f"Feature {feature}")
        msg += f"• `{term}` — {label} [{age_filter}]\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pin elonmusk")
        return
    username = context.args[0].replace("@", "")
    added = add_pinned(username)
    if added:
        await update.message.reply_text(
            f"📌 Pinned @{username}\n"
            f"I'll alert you when they post or follow someone."
        )
    else:
        await update.message.reply_text(f"@{username} is already pinned.")

async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unpin elonmusk")
        return
    username = context.args[0].replace("@", "")
    removed = remove_pinned(username)
    if removed:
        await update.message.reply_text(f"📌 Unpinned @{username}.")
    else:
        await update.message.reply_text(f"@{username} not found in pinned list.")

async def cmd_listpinned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pinned = get_pinned()
    if not pinned:
        await update.message.reply_text("No pinned accounts yet. Use /pin to add one.")
        return
    msg = "*📌 Pinned Accounts:*\n\n"
    for username, _, _, _ in pinned:
        msg += f"• [@{username}](https://x.com/{username})\n"
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Running all scans now...")
    await run_all_scans(context.bot)
    await update.message.reply_text("✅ Scan complete.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    init_db()

    async def post_init(app):
        await setup_account()
        logger.info("X account logged in.")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("addterm1", cmd_addterm1))
    app.add_handler(CommandHandler("addterm2", cmd_addterm2))
    app.add_handler(CommandHandler("addterm4", cmd_addterm4))
    app.add_handler(CommandHandler("removeterm", cmd_removeterm))
    app.add_handler(CommandHandler("listterms", cmd_listterms))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("unpin", cmd_unpin))
    app.add_handler(CommandHandler("listpinned", cmd_listpinned))
    app.add_handler(CommandHandler("scan", cmd_scan))

    # Run scans every 15 minutes
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_all_scans, "interval", minutes=15, args=[app.bot])
    scheduler.start()

    logger.info("Crypto Alpha Hunter Bot running.")
    app.run_polling()

if __name__ == "__main__":
    main()
