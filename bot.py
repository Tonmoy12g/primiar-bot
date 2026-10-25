#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paid File Sell Bot — v3
========================
A Telegram shop bot (python-telegram-bot v22+):
  • Admin adds products (name, photo, description, price, file)
  • Users browse products, buy with wallet balance, get the file
  • Add Money via MANUAL bKash (user sends TrxID, admin approves/rejects)
  • Referral: referrer gets ৳2 bonus per new referred user (once each)
  • Balance is direct taka money only — NO points system
  • No payment gateway integration
  • Main menu (8 buttons, Banglish UI):
      Premium Files, Refer Earn, My Balance, My Referral,
      Add Money, My Account, How it works, Support
  • Reply-keyboard buttons are coloured (PTB 22+ styles, same pattern
    as the friend-colored bot's _mkb helper); inline buttons stay
    plain because Telegram cannot colour inline buttons.
  • v3 flow: Premium Files shows product NAMES as coloured
    reply-keyboard buttons; tapping a name shows photo + description
    + price with a coloured reply-keyboard Buy button. The last-viewed
    product per user is tracked in the DB so Buy knows what to sell.
    Product management (add/list/delete) stays in the admin panel only.

Setup:
  1. pip install -r requirements.txt
  2. Set environment variables:
       TELEGRAM_BOT_TOKEN  — bot token from @BotFather (required)
       ADMIN_ID            — Telegram user ID of the admin (required)
       BKASH_NUMBER        — admin's bKash number (optional; admin can
                             also set it later with /setbkash)
  3. python bot.py

Data is stored in SQLite (shop.db next to this file). Product photos and
files are stored as Telegram file_ids — no files are downloaded.
No token, key, phone number or username is hardcoded in this file.
"""

import logging
import os
import sqlite3
import threading

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- config
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
ENV_BKASH = os.environ.get("BKASH_NUMBER", "")

REFER_BONUS = 2  # taka, credited once per newly referred user

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")
_db_lock = threading.Lock()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------- conversation states
(ADD_NAME, ADD_PHOTO, ADD_DESC, ADD_PRICE, ADD_FILE,
 MONEY_AMOUNT, MONEY_TRXID,
 BAL_USER, BAL_AMOUNT,
 SUPPORT_MSG) = range(10)

# ------------------------------------------------------------- main menu
BTN_PREMIUM = "📁 Premium Files"
BTN_REFER_EARN = "👥 Refer Earn"
BTN_BALANCE = "💳 My Balance"
BTN_REFERRAL = "🔗 My Referral"
BTN_MONEY = "💰 Add Money"
BTN_ACCOUNT = "👤 My Account"
BTN_HOW = "❓ How it works"
BTN_SUPPORT = "🛟 Support"
BTN_BUY = "🛒 Buy"
BTN_BACK = "🔙 Back"


def _mkb(text, style="primary"):
    """Coloured reply-keyboard button (PTB 22+); plain button on older PTB — never crashes."""
    try:
        return KeyboardButton(text, style=style)
    except TypeError:
        return KeyboardButton(text)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [_mkb(BTN_PREMIUM, "primary"), _mkb(BTN_REFER_EARN, "success")],
            [_mkb(BTN_BALANCE, "success"), _mkb(BTN_REFERRAL, "primary")],
            [_mkb(BTN_MONEY, "primary"), _mkb(BTN_ACCOUNT, "success")],
            [_mkb(BTN_HOW, "success"), _mkb(BTN_SUPPORT, "danger")],
        ],
        resize_keyboard=True,
    )


# ------------------------------------------------------------------- db
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT DEFAULT '',
                balance    INTEGER NOT NULL DEFAULT 0,
                referrer_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                photo_id    TEXT NOT NULL,
                description TEXT NOT NULL,
                price       INTEGER NOT NULL,
                file_id     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                PRIMARY KEY (referrer_id, referred_id)
            );
            CREATE TABLE IF NOT EXISTS money_requests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                trxid      TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # Migration for databases created by v1 (no first_name column).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "first_name" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT ''")
        # Migration for v3: track last-viewed product per user (Buy button).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "last_product_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_product_id INTEGER")
        conn.commit()


def get_user(user_id: int, username: str | None = None,
             first_name: str | None = None) -> sqlite3.Row:
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name) "
                "VALUES (?, ?, ?)",
                (user_id, username or "", first_name or ""),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        else:
            updates = []
            params = []
            if username is not None and row["username"] != username:
                updates.append("username = ?")
                params.append(username)
            if first_name is not None and row["first_name"] != first_name:
                updates.append("first_name = ?")
                params.append(first_name)
            if updates:
                params.append(user_id)
                conn.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?",
                    params,
                )
                conn.commit()
        return row


def get_balance(user_id: int) -> int:
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["balance"] if row else 0


def change_balance(user_id: int, delta: int) -> int:
    """Add delta (may be negative) to balance; returns the new balance."""
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (delta, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["balance"] if row else 0


def referral_stats(user_id: int):
    """Returns (count, total_earnings, [(referred_id, first_name, username)])."""
    with _db_lock, db() as conn:
        ref_rows = conn.execute(
            "SELECT referred_id FROM referrals WHERE referrer_id = ? "
            "ORDER BY referred_id",
            (user_id,),
        ).fetchall()
        referred = []
        for r in ref_rows:
            u = conn.execute(
                "SELECT username, first_name FROM users WHERE user_id = ?",
                (r["referred_id"],),
            ).fetchone()
            if u is not None:
                referred.append((r["referred_id"], u["first_name"] or "",
                                 u["username"] or ""))
            else:
                referred.append((r["referred_id"], "", ""))
    count = len(referred)
    return count, count * REFER_BONUS, referred


def get_bkash_number() -> str:
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'bkash_number'"
        ).fetchone()
        if row and row["value"]:
            return row["value"]
    return ENV_BKASH


def set_bkash_number(number: str) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('bkash_number', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (number,),
        )
        conn.commit()


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ------------------------------------------------------------------ /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)

    # Referral: /start ref_<referrer_id>
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0][4:])
        except ValueError:
            referrer_id = 0
        if referrer_id and referrer_id != user.id:
            with _db_lock, db() as conn:
                already = conn.execute(
                    "SELECT 1 FROM referrals WHERE referrer_id = ? "
                    "AND referred_id = ?",
                    (referrer_id, user.id),
                ).fetchone()
                row = conn.execute(
                    "SELECT referrer_id FROM users WHERE user_id = ?",
                    (user.id,),
                ).fetchone()
                if not already and (row is None or row["referrer_id"] is None):
                    conn.execute(
                        "INSERT INTO referrals (referrer_id, referred_id) "
                        "VALUES (?, ?)",
                        (referrer_id, user.id),
                    )
                    conn.execute(
                        "UPDATE users SET referrer_id = ? WHERE user_id = ?",
                        (referrer_id, user.id),
                    )
                    conn.execute(
                        "UPDATE users SET balance = balance + ? "
                        "WHERE user_id = ?",
                        (REFER_BONUS, referrer_id),
                    )
                    conn.commit()
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 Apnar refer link diye ekjon notun user "
                            f"join koreche! Bonus ৳{REFER_BONUS} apnar "
                            f"balance-e jog hoyeche.",
                        )
                    except Exception as exc:  # referrer may block the bot
                        logger.warning("Could not notify referrer: %s", exc)

    await update.message.reply_text(
        "👋 Assalamu Alaikum! Welcome.\n"
        "Nicher menu theke beche nin — premium file kinun, "
        "balance-e taka add korun, ba bondhuder refer korun.",
        reply_markup=main_menu(),
    )


# ------------------------------------------------------------ premium files
def products_keyboard() -> ReplyKeyboardMarkup:
    """Coloured reply-keyboard buttons with product names + Back."""
    with _db_lock, db() as conn:
        products = conn.execute(
            "SELECT id, name, price FROM products ORDER BY id"
        ).fetchall()
    rows = []
    for i, p in enumerate(products):
        style = "primary" if i % 2 == 0 else "success"
        rows.append([_mkb(p["name"], style)])
    rows.append([_mkb(BTN_BACK, "primary")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def set_last_product(user_id: int, product_id: int) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE users SET last_product_id = ? WHERE user_id = ?",
            (product_id, user_id),
        )
        conn.commit()


def get_last_product(user_id: int):
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT last_product_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["last_product_id"] if row and row["last_product_id"] else None


async def premium_files_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with _db_lock, db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count == 0:
        await update.message.reply_text(
            "📁 Ekhono kono premium file add kora hoyni. Pore abar check korun.",
            reply_markup=main_menu(),
        )
        return
    context.user_data["nav"] = "products"
    await update.message.reply_text(
        "📁 Premium Files — jeta kinte chan setar nam-e click korun:",
        reply_markup=products_keyboard(),
    )


async def product_detail_by_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for product-name taps (and unknown text).

    Runs last, so menu buttons, Buy/Back and conversation states are
    all handled before this. Unknown text gets a short hint + main menu.
    """
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    text = update.message.text.strip()
    with _db_lock, db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE name = ? ORDER BY id LIMIT 1",
            (text,),
        ).fetchone()
    if product is None:
        await update.message.reply_text(
            "❌ Bujhte parini. Menu theke beche nin.",
            reply_markup=main_menu(),
        )
        return
    set_last_product(user.id, product["id"])
    context.user_data["last_product_id"] = product["id"]
    context.user_data["nav"] = "detail"
    keyboard = ReplyKeyboardMarkup(
        [
            [_mkb(BTN_BUY, "success")],
            [_mkb(BTN_BACK, "primary")],
        ],
        resize_keyboard=True,
    )
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=product["photo_id"],
        caption=f"📌 {product['name']}\n\n"
                f"{product['description']}\n\n"
                f"💰 Dam: ৳{product['price']}",
        reply_markup=keyboard,
    )


async def buy_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    product_id = (context.user_data.get("last_product_id")
                  or get_last_product(user_id))
    if not product_id:
        await update.message.reply_text(
            "❌ Prothome 📁 Premium Files theke ekta file beche nin.",
            reply_markup=main_menu(),
        )
        return
    with _db_lock, db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    if product is None:
        await update.message.reply_text("❌ Ei product-ti ar nei.",
                                        reply_markup=main_menu())
        return
    balance = get_balance(user_id)
    if balance < product["price"]:
        await update.message.reply_text(
            f"⚠️ Apnar balance kom! Bortoman balance: ৳{balance}, "
            f"dam: ৳{product['price']}.\n"
            f"Prothome 💰 Add Money theke balance-e taka add korun.",
            reply_markup=main_menu(),
        )
        return
    new_balance = change_balance(user_id, -product["price"])
    context.user_data["nav"] = "main"
    await context.bot.send_document(
        chat_id=user_id,
        document=product["file_id"],
        caption=f"✅ Kena somponno! {product['name']}\n"
                f"💳 Bortoman balance: ৳{new_balance}",
    )
    await update.message.reply_text(
        "👍 Dhonnobad! Menu theke abar beche nin.",
        reply_markup=main_menu(),
    )


async def back_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nav = context.user_data.get("nav", "main")
    if nav == "detail":
        context.user_data["nav"] = "products"
        await update.message.reply_text(
            "📁 Premium Files — jeta kinte chan setar nam-e click korun:",
            reply_markup=products_keyboard(),
        )
    else:
        context.user_data["nav"] = "main"
        await update.message.reply_text(
            "👋 Main menu:",
            reply_markup=main_menu(),
        )


# -------------------------------------------------------------- add money
async def add_money_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bkash = get_bkash_number()
    if not bkash:
        await update.message.reply_text(
            "⚠️ Admin ekhono bKash number set koren ni. "
            "Pore abar chesta korun.",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "💰 Koto taka add korte chan? Shudhu sonkhata likhun (jemon: 100).",
        reply_markup=main_menu(),
    )
    return MONEY_AMOUNT


async def add_money_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Sothik sonkha likhun (jemon: 100).")
        return MONEY_AMOUNT
    context.user_data["money_amount"] = amount
    bkash = get_bkash_number()
    await update.message.reply_text(
        f"💰 {amount} taka add korte:\n\n"
        f"1️⃣ bKash-e {amount} taka pathan: {bkash}\n"
        f"2️⃣ Payment-er TrxID-ti ekhane pathan.",
        reply_markup=main_menu(),
    )
    return MONEY_TRXID


async def add_money_trxid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    trxid = update.message.text.strip()
    amount = context.user_data.get("money_amount")
    user = update.effective_user
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO money_requests (user_id, amount, trxid) "
            "VALUES (?, ?, ?)",
            (user.id, amount, trxid),
        )
        req_id = cur.lastrowid
        conn.commit()
    keyboard = [[
        InlineKeyboardButton("✅ Approve",
                             callback_data=f"approve_{req_id}"),
        InlineKeyboardButton("❌ Reject",
                             callback_data=f"reject_{req_id}"),
    ]]
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💰 Notun Add Money Request\n\n"
            f"👤 User: {user.full_name} (@{user.username or '-'}, "
            f"ID: {user.id})\n"
            f"💵 Poriman: ৳{amount}\n"
            f"🔢 TrxID: {trxid}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as exc:
        logger.warning("Could not notify admin: %s", exc)
    await update.message.reply_text(
        "✅ Request pathano hoyeche. Admin verify korlei "
        "balance-e taka jog hobe.",
        reply_markup=main_menu(),
    )
    context.user_data.pop("money_amount", None)
    return ConversationHandler.END


async def money_request_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ Shudhu admin eta korte parben.")
        return
    action, req_id = query.data.split("_", 1)
    req_id = int(req_id)
    with _db_lock, db() as conn:
        req = conn.execute(
            "SELECT * FROM money_requests WHERE id = ?", (req_id,)
        ).fetchone()
        if req is None or req["status"] != "pending":
            await query.edit_message_text("⚠️ Ei request-ti ar pending nei.")
            return
        status = "approved" if action == "approve" else "rejected"
        conn.execute(
            "UPDATE money_requests SET status = ? WHERE id = ?",
            (status, req_id),
        )
        conn.commit()
    if action == "approve":
        new_balance = change_balance(req["user_id"], req["amount"])
        try:
            await context.bot.send_message(
                req["user_id"],
                f"✅ Apnar ৳{req['amount']} Add Money Approve hoyeche!\n"
                f"💳 Bortoman balance: ৳{new_balance}",
            )
        except Exception as exc:
            logger.warning("Could not notify user: %s", exc)
        await query.edit_message_text(
            f"✅ Approved — User {req['user_id']} er balance-e "
            f"৳{req['amount']} jog hoyeche.")
    else:
        try:
            await context.bot.send_message(
                req["user_id"],
                f"❌ Apnar ৳{req['amount']} Add Money request "
                f"Reject hoyeche. TrxID thik thakle abar request korun.",
            )
        except Exception as exc:
            logger.warning("Could not notify user: %s", exc)
        await query.edit_message_text(
            f"❌ Rejected — User {req['user_id']} er ৳{req['amount']} "
            f"request batil hoyeche.")


# ------------------------------------------------------------- refer earn
async def refer_earn_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    count, earnings, _ = referral_stats(user.id)
    username = context.bot.username or "YourBotUsername"
    link = f"https://t.me/{username}?start=ref_{user.id}"
    await update.message.reply_text(
        f"👥 Apnar Refer Link:\n{link}\n\n"
        f"Ei link diye keu notun join korle apni paben "
        f"proti refer-e ৳{REFER_BONUS} bonus.\n"
        f"💰 Mot referral earning: ৳{earnings}\n"
        f"✅ Mot refer: {count} jon",
        reply_markup=main_menu(),
    )


# ------------------------------------------------------------ my referral
async def my_referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    count, earnings, referred = referral_stats(user.id)
    if not referred:
        await update.message.reply_text(
            "🔗 Ekhono keu apnar link diye join koreni.\n"
            "Refer link pete 👥 Refer Earn-e jan.",
            reply_markup=main_menu(),
        )
        return
    lines = [
        f"🔗 Apnar Referral:\n",
        f"✅ Mot refer: {count} jon",
        f"💰 Mot earning: ৳{earnings}\n",
        "👥 Referred user-ra:",
    ]
    for rid, first_name, uname in referred:
        label = first_name or "-"
        if uname:
            label += f" (@{uname})"
        else:
            label += f" (ID: {rid})"
        lines.append(f"• {label}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu())


# ------------------------------------------------------------- my balance
async def balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    balance = get_balance(user.id)
    await update.message.reply_text(
        f"💳 Apnar bortoman balance: ৳{balance}",
        reply_markup=main_menu(),
    )


# ------------------------------------------------------------- my account
async def my_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    balance = get_balance(user.id)
    count, earnings, _ = referral_stats(user.id)
    name_line = user.first_name or "-"
    if user.username:
        name_line += f" (@{user.username})"
    await update.message.reply_text(
        "👤 My Account\n\n"
        f"📛 Nam: {name_line}\n"
        f"🆔 User ID: {user.id}\n"
        f"💳 Balance: ৳{balance}\n"
        f"👥 Mot refer: {count} jon\n"
        f"💰 Referral earning: ৳{earnings}",
        reply_markup=main_menu(),
    )


# ------------------------------------------------------------ how it works
HOW_IT_WORKS_TEXT = (
    "❓ How it works\n\n"
    "📁 File kinte:\n"
    "Premium Files-e jan, file-er nam-e click korun, tarpor Buy "
    "button-e click korun. Balance thakle file sathe sathe peye jaben.\n\n"
    "💰 Taka add korte:\n"
    "Add Money-e amount likhun, bKash-e taka pathiye TrxID din. "
    "Admin approve korlei balance-e taka jog hobe.\n\n"
    "👥 Refer bonus:\n"
    "Apnar refer link diye keu notun join korle apni paben "
    "proti refer-e ৳2 bonus. Bonus sorasori apnar balance-e jog hobe."
)


async def how_it_works_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HOW_IT_WORKS_TEXT,
        reply_markup=main_menu(),
    )


# ------------------------------------------------------------------ support
async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🛟 Apnar somossa-ti likhun, admin-ke pathiye dewa hobe.",
        reply_markup=main_menu(),
    )
    return SUPPORT_MSG


async def support_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    problem = update.message.text.strip()
    try:
        await context.bot.send_message(
            ADMIN_ID,
            "🛟 Notun Support Message\n\n"
            f"👤 User: {user.full_name} (@{user.username or '-'}, "
            f"ID: {user.id})\n\n"
            f"📝 Somossa:\n{problem}",
        )
    except Exception as exc:
        logger.warning("Could not forward support message: %s", exc)
    await update.message.reply_text(
        "✅ Apnar message admin-ke pathano hoyeche. "
        "Admin sorasori apnake reply diben.",
        reply_markup=main_menu(),
    )
    return ConversationHandler.END


# ------------------------------------------------------------------- admin
def admin_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Product Add",
                              callback_data="admin_add")],
        [InlineKeyboardButton("📋 Product List",
                              callback_data="admin_list")],
        [InlineKeyboardButton("🗑️ Product Delete",
                              callback_data="admin_del")],
        [InlineKeyboardButton("💰 Balance Add",
                              callback_data="admin_addbal")],
        [InlineKeyboardButton("⏳ Pending Request",
                              callback_data="admin_pending")],
        [InlineKeyboardButton("⚙️ bKash Number Set",
                              callback_data="admin_bkash")],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Shudhu admin eta bebohar korte parben.")
        return
    await update.message.reply_text(
        "⚙️ Admin Panel — beche nin:",
        reply_markup=admin_menu_markup(),
    )


async def set_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Shudhu admin eta bebohar korte parben.")
        return
    if not context.args:
        await update.message.reply_text(
            "Bebohar: /setbkash <bKash number>\nJemon: /setbkash 01XXXXXXXXX")
        return
    set_bkash_number(context.args[0].strip())
    await update.message.reply_text(
        f"✅ bKash number set hoyeche: {context.args[0].strip()}")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ Shudhu admin eta korte parben.")
        return None
    data = query.data

    if data == "admin_list":
        with _db_lock, db() as conn:
            products = conn.execute(
                "SELECT id, name, price FROM products ORDER BY id"
            ).fetchall()
        if not products:
            await query.edit_message_text("📋 Kono product nei.")
        else:
            lines = ["📋 Product List:\n"]
            for p in products:
                lines.append(f"{p['id']}. {p['name']} — ৳{p['price']}")
            await query.edit_message_text("\n".join(lines))
        return None

    if data == "admin_del":
        with _db_lock, db() as conn:
            products = conn.execute(
                "SELECT id, name FROM products ORDER BY id"
            ).fetchall()
        if not products:
            await query.edit_message_text("🗑️ Delete korar moto kono product nei.")
            return None
        keyboard = [[InlineKeyboardButton(
            f"🗑️ {p['name']}", callback_data=f"del_{p['id']}")]
            for p in products]
        await query.edit_message_text(
            "🗑️ Kon product delete korben?",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return None

    if data == "admin_pending":
        with _db_lock, db() as conn:
            reqs = conn.execute(
                "SELECT * FROM money_requests WHERE status = 'pending' "
                "ORDER BY id"
            ).fetchall()
        if not reqs:
            await query.edit_message_text("⏳ Kono pending request nei.")
            return None
        for r in reqs:
            keyboard = [[
                InlineKeyboardButton("✅ Approve",
                                     callback_data=f"approve_{r['id']}"),
                InlineKeyboardButton("❌ Reject",
                                     callback_data=f"reject_{r['id']}"),
            ]]
            await context.bot.send_message(
                query.message.chat_id,
                f"💰 Request #{r['id']}\n"
                f"👤 User ID: {r['user_id']}\n"
                f"💵 Poriman: ৳{r['amount']}\n"
                f"🔢 TrxID: {r['trxid']}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return None

    if data == "admin_bkash":
        current = get_bkash_number() or "set kora hoyni"
        await query.edit_message_text(
            f"⚙️ Bortoman bKash number: {current}\n\n"
            f"Bodlate likhun: /setbkash <notun number>")
        return None

    if data == "admin_add":
        await query.edit_message_text("➕ Product-er nam likhun:")
        return ADD_NAME

    if data == "admin_addbal":
        await query.edit_message_text(
            "💰 Kon user-er balance baraben? User ID likhun:")
        return BAL_USER

    return None


async def admin_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ Shudhu admin eta korte parben.")
        return
    product_id = int(query.data.split("_", 1)[1])
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
    await query.edit_message_text(f"🗑️ Product #{product_id} delete hoyeche.")


# -------------------------------------------- admin: add product (steps)
async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["prod_name"] = update.message.text.strip()
    await update.message.reply_text("🖼️ Ebar product-er chobi pathan:")
    return ADD_PHOTO


async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("❌ Chobi pathan (file noy, chobi hisebe).")
        return ADD_PHOTO
    context.user_data["prod_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("📝 Ebar product-er biboron likhun:")
    return ADD_DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["prod_desc"] = update.message.text.strip()
    await update.message.reply_text("💰 Ebar dam likhun (shudhu sonkha, jemon: 100):")
    return ADD_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = int(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Sothik dam likhun (jemon: 100).")
        return ADD_PRICE
    context.user_data["prod_price"] = price
    await update.message.reply_text("📎 Ebar file-ti document hisebe pathan:")
    return ADD_FILE


async def add_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.document:
        await update.message.reply_text("❌ File-ti document hisebe pathan.")
        return ADD_FILE
    data = context.user_data
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO products (name, photo_id, description, price, file_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (data["prod_name"], data["prod_photo"], data["prod_desc"],
             data["prod_price"], update.message.document.file_id),
        )
        conn.commit()
        prod_id = cur.lastrowid
    await update.message.reply_text(
        f"✅ Product jog hoyeche!\n"
        f"📌 {data['prod_name']} — ৳{data['prod_price']} (ID: {prod_id})",
        reply_markup=main_menu(),
    )
    for key in ("prod_name", "prod_photo", "prod_desc", "prod_price"):
        context.user_data.pop(key, None)
    return ConversationHandler.END


# ------------------------------------------- admin: manual balance add
async def bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Sothik User ID (sonkha) likhun.")
        return BAL_USER
    context.user_data["bal_user"] = user_id
    await update.message.reply_text("💰 Koto taka jog korben? Sonkha likhun:")
    return BAL_AMOUNT


async def bal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Sothik sonkha likhun (jemon: 100).")
        return BAL_AMOUNT
    user_id = context.user_data.pop("bal_user")
    get_user(user_id)
    new_balance = change_balance(user_id, amount)
    await update.message.reply_text(
        f"✅ User {user_id} er balance-e ৳{amount} jog hoyeche. "
        f"Bortoman balance: ৳{new_balance}",
        reply_markup=main_menu(),
    )
    try:
        await context.bot.send_message(
            user_id,
            f"💰 Admin apnar balance-e ৳{amount} jog korechen.\n"
            f"💳 Bortoman balance: ৳{new_balance}",
        )
    except Exception as exc:
        logger.warning("Could not notify user: %s", exc)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("money_amount", None)
    for key in ("prod_name", "prod_photo", "prod_desc", "prod_price", "bal_user"):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "❌ Batil kora hoyeche.", reply_markup=main_menu())
    return ConversationHandler.END


# --------------------------------------------------------------------- main
def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "ERROR: TELEGRAM_BOT_TOKEN environment variable is not set.")
    if ADMIN_ID == 0:
        raise SystemExit(
            "ERROR: ADMIN_ID environment variable is not set.")
    init_db()

    app = Application.builder().token(TOKEN).build()

    # Admin product-add conversation (entered from /admin inline menu)
    add_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback,
                                           pattern="^admin_(add|addbal)$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       add_name)],
            ADD_PHOTO: [MessageHandler(filters.PHOTO, add_photo)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       add_desc)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                        add_price)],
            ADD_FILE: [MessageHandler(filters.Document.ALL, add_file)],
            BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       bal_user)],
            BAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                         bal_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # User add-money conversation
    money_conv = ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(f"^{BTN_MONEY}$"), add_money_start)],
        states={
            MONEY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                           add_money_amount)],
            MONEY_TRXID: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                          add_money_trxid)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # User support conversation
    support_conv = ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(f"^{BTN_SUPPORT}$"), support_start)],
        states={
            SUPPORT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                          support_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("setbkash", set_bkash))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(add_product_conv)
    app.add_handler(money_conv)
    app.add_handler(support_conv)
    app.add_handler(CallbackQueryHandler(
        admin_callback,
        pattern="^(admin_list|admin_del|admin_pending|admin_bkash)$"))
    app.add_handler(CallbackQueryHandler(
        admin_delete_product, pattern="^del_[0-9]+$"))
    app.add_handler(CallbackQueryHandler(
        money_request_decision, pattern="^(approve|reject)_[0-9]+$"))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_PREMIUM}$"), premium_files_menu))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_REFER_EARN}$"), refer_earn_menu))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_BALANCE}$"), balance_menu))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_REFERRAL}$"), my_referral_menu))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_ACCOUNT}$"), my_account_menu))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_HOW}$"), how_it_works_menu))
    # Buy / Back reply-keyboard buttons (checked before the catch-all).
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_BUY}$"), buy_button))
    app.add_handler(MessageHandler(
        filters.Regex(f"^{BTN_BACK}$"), back_button))
    # Product-name taps (coloured reply-keyboard buttons) — last, so
    # menu buttons, Buy/Back and conversations win first.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, product_detail_by_name))

    logger.info("Paid File Sell Bot v3 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
