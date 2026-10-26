#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paid File Sell Bot — v5.3 (100% Stable & Fixed)
================================================
Fixed:
  • Fixed async coroutine bug in `_make_menu_router` (prevented TypeError: 'coroutine' object is not callable).
  • Unified into a single master `ConversationHandler` so switching between ANY menu or admin flow works seamlessly.
  • Cancel button (`❌ Cancel`) and Back button (`🔙 Back`) work reliably at every step (both user & admin flows).
  • Auto-Cancel: Tapping ANY menu button instantly cancels the current flow and starts the new action.
  • Admin Panel works without errors and retains admin keyboard navigation.
"""

import base64
import io
import logging
import os
import re
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

try:
    from flask import Flask
    _FLASK_AVAILABLE = True
except ImportError:
    Flask = None
    _FLASK_AVAILABLE = False

try:
    from banner_b64 import EMBEDDED_BANNER_B64 as _EMBEDDED_BANNER_B64
except Exception:
    _EMBEDDED_BANNER_B64 = ""

# ---------------------------------------------------------------- config
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# ADMIN_ID কে স্ট্রিং থেকে ১০০% সেফলি ইন্টিজারে কনভার্ট করা হচ্ছে
_raw_admin = str(os.environ.get("ADMIN_ID", "0")).strip().replace("'", "").replace('"', "")
try:
    ADMIN_ID = int(_raw_admin)
except ValueError:
    ADMIN_ID = 0

ENV_BKASH = os.environ.get("BKASH_NUMBER", "").strip()
DEFAULT_REFER_BONUS = 2

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")
_db_lock = threading.Lock()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------- conversation states
(
    ADD_NAME,
    ADD_PHOTO,
    ADD_DESC,
    ADD_PRICE,
    ADD_FILE,
    MONEY_AMOUNT,
    MONEY_TRXID,
    MONEY_METHOD,
    BAL_USER,
    BAL_AMOUNT,
    SUPPORT_MSG,
    SET_BKASH,
    SET_SUPPORT,
    SET_BONUS,
    SET_FORCE,
    SET_NAGAD,
    SET_ROCKET,
) = range(17)

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
BTN_CANCEL = "❌ Cancel"
BTN_ADMIN = "⚙️ Admin Panel"

_VALID_BUTTON_STYLES = ("primary", "success", "danger")


def _mkb(text: str, style: str = "primary") -> KeyboardButton:
    if style not in _VALID_BUTTON_STYLES:
        style = "primary"
    try:
        return KeyboardButton(text, style=style)
    except (TypeError, ValueError):
        return KeyboardButton(text)


def _menu_rows(admin: bool):
    rows = [
        [_mkb(BTN_PREMIUM, "primary"), _mkb(BTN_REFER_EARN, "success")],
        [_mkb(BTN_BALANCE, "success"), _mkb(BTN_REFERRAL, "primary")],
        [_mkb(BTN_MONEY, "primary"), _mkb(BTN_ACCOUNT, "success")],
        [_mkb(BTN_HOW, "success"), _mkb(BTN_SUPPORT, "danger")],
    ]
    if admin:
        rows.append([_mkb(BTN_ADMIN, "primary")])
    return rows


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(_menu_rows(False), resize_keyboard=True)


def admin_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(_menu_rows(True), resize_keyboard=True)


def menu_for(user_id: int) -> ReplyKeyboardMarkup:
    return admin_main_menu() if is_admin(user_id) else main_menu()


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[_mkb(BTN_CANCEL, "danger")]], resize_keyboard=True)


# ------------------------------------------------------------------- db
def db():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=20.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(DB_PATH, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT,
                first_name      TEXT DEFAULT '',
                balance         INTEGER NOT NULL DEFAULT 0,
                referrer_id     INTEGER,
                last_product_id INTEGER
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
                method     TEXT DEFAULT 'bkash',
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "first_name" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT ''")
        if "last_product_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_product_id INTEGER")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(money_requests)")]
        if "method" not in cols:
            conn.execute("ALTER TABLE money_requests ADD COLUMN method TEXT DEFAULT 'bkash'")
        conn.commit()


def get_user(user_id: int, username: str | None = None, first_name: str | None = None) -> sqlite3.Row:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, username or "", first_name or ""),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        else:
            updates, params = [], []
            if username is not None and row["username"] != username:
                updates.append("username = ?")
                params.append(username)
            if first_name is not None and row["first_name"] != first_name:
                updates.append("first_name = ?")
                params.append(first_name)
            if updates:
                params.append(user_id)
                conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row


def get_balance(user_id: int) -> int:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0


def change_balance(user_id: int, delta: int) -> int:
    with _db_lock, db() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
        conn.commit()
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0


def try_charge(user_id: int, price: int) -> int | None:
    with _db_lock, db() as conn:
        cur = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (price, user_id, price),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0


def referral_stats(user_id: int):
    with _db_lock, db() as conn:
        ref_rows = conn.execute(
            "SELECT referred_id FROM referrals WHERE referrer_id = ? ORDER BY referred_id",
            (user_id,),
        ).fetchall()
        referred = []
        for r in ref_rows:
            u = conn.execute(
                "SELECT username, first_name FROM users WHERE user_id = ?",
                (r["referred_id"],),
            ).fetchone()
            if u is not None:
                referred.append((r["referred_id"], u["first_name"] or "", u["username"] or ""))
            else:
                referred.append((r["referred_id"], "", ""))
    count = len(referred)
    return count, count * get_refer_bonus(), referred


def get_setting(key: str, default: str = "") -> str:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row and row["value"] is not None and row["value"] != "":
            return row["value"]
    return default


def set_setting(key: str, value: str) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_bkash_number() -> str:
    return get_setting("bkash_number", ENV_BKASH)


def set_bkash_number(number: str) -> None:
    set_setting("bkash_number", number)


def get_nagad_number() -> str:
    return get_setting("nagad_number", "")


def set_nagad_number(number: str) -> None:
    set_setting("nagad_number", number)


def get_rocket_number() -> str:
    return get_setting("rocket_number", "")


def set_rocket_number(number: str) -> None:
    set_setting("rocket_number", number)


DEPOSIT_METHODS = (("bkash", "bKash"), ("nagad", "Nagad"), ("rocket", "Rocket"))
_METHOD_BUTTONS = {
    "bkash": "🔴 বিকাশ (পার্সোনাল Send Money ⚡)",
    "nagad": "🟠 নগদ (পার্সোনাল Send Money ⚡)",
    "rocket": "🟣 রকেট (পার্সোনাল Send Money ⚡)",
}
_METHOD_BN = {
    "bkash": ("🔴", "বিকাশ"),
    "nagad": ("🟠", "নগদ"),
    "rocket": ("🟣", "রকেট"),
}
_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━"


def get_method_number(method: str) -> str:
    method_key = (method or "bkash").lower()
    if method_key == "bkash":
        return get_bkash_number()
    if method_key == "nagad":
        return get_nagad_number()
    return get_rocket_number()


def is_method_enabled(method: str) -> bool:
    return get_setting(f"method_{method}", "on") == "on"


def toggle_method(method: str) -> bool:
    new_state = not is_method_enabled(method)
    set_setting(f"method_{method}", "on" if new_state else "off")
    return new_state


def available_methods():
    offered = []
    for key, name in DEPOSIT_METHODS:
        number = get_method_number(key)
        if is_method_enabled(key) and number:
            offered.append((key, name, number))
    return offered


def deposit_methods_status_text() -> str:
    lines = []
    for key, name in DEPOSIT_METHODS:
        state = "ON" if is_method_enabled(key) else "OFF"
        num = get_method_number(key)
        number_str = f"({num})" if num else "(no number)"
        lines.append(f"• {name}: {state} {number_str}")
    return "\n".join(lines)


def get_support_id() -> str:
    return get_setting("support_id", str(ADMIN_ID))


def set_support_id(support_id: str) -> None:
    set_setting("support_id", support_id)


def get_support_username() -> str:
    return get_setting("support_username", "")


def set_support_username(username: str) -> None:
    set_setting("support_username", username)


def get_refer_bonus() -> int:
    try:
        bonus = int(get_setting("refer_bonus", ""))
        return bonus if bonus >= 0 else DEFAULT_REFER_BONUS
    except ValueError:
        return DEFAULT_REFER_BONUS


def set_refer_bonus(amount: int) -> None:
    set_setting("refer_bonus", str(amount))


def get_force_channel() -> str:
    return get_setting("force_channel", "")


def set_force_channel(channel: str) -> None:
    set_setting("force_channel", channel)


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ------------------------------------------------------------------ /start
async def _credit_referral(user_id: int, referrer_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not referrer_id or referrer_id == user_id:
        return False
    with _db_lock, db() as conn:
        already = conn.execute(
            "SELECT 1 FROM referrals WHERE referrer_id = ? AND referred_id = ?",
            (referrer_id, user_id),
        ).fetchone()
        row = conn.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if already or (row is not None and row["referrer_id"] is not None):
            return False
        # Ensure referrer exists in users table so balance update works
        ref_exists = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer_id,)).fetchone()
        if not ref_exists:
            conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, '', '')", (referrer_id,))
        bonus = get_refer_bonus()
        conn.execute("INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, user_id))
        conn.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, referrer_id))
        conn.commit()
    try:
        await context.bot.send_message(
            referrer_id,
            f"🎉 Great news! A new friend just joined using your refer link!\n"
            f"💰 ৳{bonus} bonus has been added to your balance.\n"
            f"👥 Keep sharing your link to earn more!",
        )
    except Exception:
        pass
    return True


async def _force_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel: str):
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
    except Exception:
        return None
    return member.status in ("creator", "administrator", "member", "restricted")


def _force_gate_markup(channel: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{channel.lstrip('@')}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=url)],
            [InlineKeyboardButton("✅ I Joined", callback_data="force_check")],
        ]
    )


BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.jpg")


def _embedded_banner_bytes() -> bytes | None:
    if not _EMBEDDED_BANNER_B64:
        return None
    try:
        return base64.b64decode(_EMBEDDED_BANNER_B64)
    except Exception:
        return None


def _welcome_text() -> str:
    try:
        bonus = get_refer_bonus()
    except Exception:
        bonus = DEFAULT_REFER_BONUS
    return (
        "👋 Welcome to Premium Files Bot!\n\n"
        "🛍️ Buy premium files instantly\n"
        "💰 Add money to your balance\n"
        f"👥 Refer friends and earn ৳{bonus} per friend\n\n"
        "Tap a button below to get started ✨"
    )


async def _deliver_welcome(send_text, send_photo, user_id: int) -> None:
    text = _welcome_text()
    markup = menu_for(user_id)
    banner = BANNER_PATH if os.path.isfile(BANNER_PATH) else None
    if banner is not None:
        try:
            with open(banner, "rb") as photo:
                await send_photo(photo, text, markup)
            return
        except Exception:
            pass
    embedded = _embedded_banner_bytes()
    if embedded is not None:
        try:
            await send_photo(io.BytesIO(embedded), text, markup)
            return
        except Exception:
            pass
    await send_text(text, markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    context.user_data["nav"] = "main"

    pending_ref = 0
    if context.args and context.args[0].startswith("ref_"):
        try:
            pending_ref = int(context.args[0][4:])
        except ValueError:
            pending_ref = 0

    channel = get_force_channel()
    if channel and not is_admin(user.id):
        status = await _force_membership(context, user.id, channel)
        if status is False:
            if pending_ref:
                context.user_data["pending_ref"] = pending_ref
            await update.message.reply_text(
                "👋 Welcome! To use this bot, please join our channel first:\n\n"
                f"📢 {channel}\n\nTap Join below, then tap ✅ I Joined.",
                reply_markup=_force_gate_markup(channel),
            )
            return ConversationHandler.END

    if pending_ref:
        await _credit_referral(user.id, pending_ref, context)

    await _deliver_welcome(
        lambda t, m: update.message.reply_text(t, reply_markup=m),
        lambda p, t, m: update.message.reply_photo(photo=p, caption=t, reply_markup=m),
        user.id,
    )
    return ConversationHandler.END


async def force_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    channel = get_force_channel()
    if not channel or is_admin(user.id):
        status = True
    else:
        status = await _force_membership(context, user.id, channel)

    if status is False:
        await query.answer(
            "⚠️ You haven't joined the channel yet. Please join first, then tap ✅ I Joined again.",
            show_alert=True,
        )
        return
    await query.answer()
    pending_ref = context.user_data.pop("pending_ref", 0)
    if pending_ref:
        await _credit_referral(user.id, pending_ref, context)
    try:
        await query.edit_message_text("✅ Thanks for joining! Welcome to the shop. 🎉")
    except Exception:
        pass

    await _deliver_welcome(
        lambda t, m: context.bot.send_message(chat_id=user.id, text=t, reply_markup=m),
        lambda p, t, m: context.bot.send_photo(chat_id=user.id, photo=p, caption=t, reply_markup=m),
        user.id,
    )


# ------------------------------------------------------------ premium files
def _product_button_label(product_id: int, name: str, price: int) -> str:
    return f"{name} — ৳{price} (#{product_id})"


def products_keyboard() -> ReplyKeyboardMarkup:
    with _db_lock, db() as conn:
        products = conn.execute("SELECT id, name, price FROM products ORDER BY id").fetchall()
    rows = []
    for i, p in enumerate(products):
        style = "primary" if i % 2 == 0 else "success"
        label = _product_button_label(p["id"], p["name"], p["price"])
        rows.append([_mkb(label, style)])
    rows.append([_mkb(BTN_BACK, "primary")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def set_last_product(user_id: int, product_id: int) -> None:
    with _db_lock, db() as conn:
        conn.execute("UPDATE users SET last_product_id = ? WHERE user_id = ?", (product_id, user_id))
        conn.commit()


def get_last_product(user_id: int):
    with _db_lock, db() as conn:
        row = conn.execute("SELECT last_product_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["last_product_id"] if row and row["last_product_id"] else None


async def premium_files_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with _db_lock, db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count == 0:
        await update.message.reply_text(
            "📁 No premium files have been added yet. Please check again later.",
            reply_markup=menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    context.user_data["nav"] = "products"
    await update.message.reply_text(
        f"{BTN_PREMIUM} — tap the file name you want to buy:",
        reply_markup=products_keyboard(),
    )
    return ConversationHandler.END


def _find_product_by_label(label: str):
    text = label.strip()
    with _db_lock, db() as conn:
        m = re.search(r"#(\d+)\)\s*$", text)
        if m:
            pid = int(m.group(1))
            row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if row is not None:
                return row
        m = re.match(r"^(.*) — ৳\d+", text)
        if m:
            name = m.group(1).strip()
            rows = conn.execute("SELECT * FROM products WHERE name = ? ORDER BY id", (name,)).fetchall()
            if len(rows) == 1:
                return rows[0]
        rows = conn.execute("SELECT * FROM products WHERE name = ? ORDER BY id", (text,)).fetchall()
        if len(rows) == 1:
            return rows[0]
    return None


async def product_detail_by_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    text = update.message.text.strip()
    product = _find_product_by_label(text)
    if product is None:
        await update.message.reply_text(
            "❌ Sorry, I didn't understand that. Please choose from the menu.",
            reply_markup=menu_for(update.effective_user.id),
        )
        return
    set_last_product(user.id, product["id"])
    context.user_data["last_product_id"] = product["id"]
    context.user_data["nav"] = "detail"
    keyboard = ReplyKeyboardMarkup(
        [[_mkb(BTN_BUY, "success")], [_mkb(BTN_BACK, "primary")]],
        resize_keyboard=True,
    )
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=product["photo_id"],
        caption=f"📌 {product['name']}\n\n{product['description']}\n\n💰 Price: ৳{product['price']}",
        reply_markup=keyboard,
    )


async def buy_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    product_id = context.user_data.get("last_product_id") or get_last_product(user_id)
    if not product_id:
        await update.message.reply_text(
            f"❌ First choose a file from {BTN_PREMIUM}.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END
    with _db_lock, db() as conn:
        product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        await update.message.reply_text(
            "❌ This product is no longer available.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END
    new_balance = try_charge(user_id, product["price"])
    if new_balance is None:
        balance = get_balance(user_id)
        await update.message.reply_text(
            f"⚠️ Your balance is too low! Current balance: ৳{balance}, price: ৳{product['price']}.\n"
            f"First add money from {BTN_MONEY}.",
            reply_markup=menu_for(user_id),
        )
        return ConversationHandler.END
    context.user_data["nav"] = "main"
    await context.bot.send_document(
        chat_id=user_id,
        document=product["file_id"],
        caption=f"✅ Purchase complete! {product['name']}\n💳 Current balance: ৳{new_balance}",
    )
    await update.message.reply_text(
        "👍 Thank you! Choose from the menu again.",
        reply_markup=menu_for(user_id),
    )
    return ConversationHandler.END


async def back_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow_data(context)
    nav = context.user_data.get("nav", "main")
    if nav == "detail":
        context.user_data["nav"] = "products"
        await update.message.reply_text(
            f"{BTN_PREMIUM} — tap the file name you want to buy:",
            reply_markup=products_keyboard(),
        )
    else:
        context.user_data["nav"] = "main"
        await update.message.reply_text("👋 Main menu:", reply_markup=menu_for(update.effective_user.id))
    return ConversationHandler.END


# -------------------------------------------------------------- add money
async def add_money_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    methods = available_methods()
    if not methods:
        await update.message.reply_text(
            "⚠️ বর্তমানে ডিপোজিট বন্ধ আছে। সাহায্যের জন্য Support-এ যোগাযোগ করুন।",
            reply_markup=menu_for(update.effective_user.id),
        )
        return ConversationHandler.END
    rows = [[_mkb(_METHOD_BUTTONS[key], "primary")] for key, _, _ in methods]
    rows.append([_mkb(BTN_CANCEL, "danger")])

    lines = []
    for key, _, _ in methods:
        circle, bn = _METHOD_BN[key]
        lines.append(f"{circle} {bn} (পার্সোনাল Send Money ⚡)")
    await update.message.reply_text(
        "💳 ডিপোজিট / ব্যালেন্স রিচার্জ (Send Money) ⚡\n"
        f"{_DIVIDER}\n"
        "আমাদের পার্সোনাল নাম্বারে Send Money করুন। অ্যাডমিন ম্যানুয়ালি যাচাই করে ব্যালেন্স যোগ করে দেবে:\n\n"
        + "\n".join(lines)
        + f"\n{_DIVIDER}\n🔗 পছন্দের মাধ্যমে ক্লিক করুন:",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True),
    )
    return MONEY_METHOD


async def add_money_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK):
        return await cancel(update, context)

    key = None
    for method_key, button in _METHOD_BUTTONS.items():
        if text == button:
            key = method_key
            break
    offered = [k for k, _, _ in available_methods()]
    if key is None or key not in offered:
        await update.message.reply_text("❌ এই মাধ্যমটি বর্তমানে চালু নেই। অন্য একটি বেছে নিন।")
        return MONEY_METHOD
    context.user_data["money_method"] = key
    _, bn = _METHOD_BN[key]

    await update.message.reply_text(
        f"💵 {bn} (পার্সোনাল Send Money) ⚡\n{_DIVIDER}\n"
        "কত টাকা (৳) ডিপোজিট করতে চান লিখে পাঠান (যেমন: 50, 100, 500):",
        reply_markup=cancel_keyboard(),
    )
    return MONEY_AMOUNT


async def add_money_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)

    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: 100)।", reply_markup=cancel_keyboard())
        return MONEY_AMOUNT

    context.user_data["money_amount"] = amount
    method = context.user_data.get("money_method", "bkash")
    number = get_method_number(method)
    if not number:
        await update.message.reply_text(
            "⚠️ এই মাধ্যমটি বর্তমানে চালু নেই। অন্য একটি বেছে নিন।",
            reply_markup=menu_for(update.effective_user.id),
        )
        _clear_flow_data(context)
        return ConversationHandler.END
    circle, bn = _METHOD_BN.get(method, ("🔴", "বিকাশ"))

    await update.message.reply_text(
        f"{circle} {bn} পার্সোনাল ডিপোজিট (Send Money) ⚡\n{_DIVIDER}\n"
        f"💵 ডিপোজিট পরিমাণ: {amount} ৳\n📱 {bn} পার্সোনাল নম্বর:\n{number}\n\n"
        "📌 পেমেন্ট করার নিয়ম:\n"
        f"১. ওপরের নম্বরে ঠিক {amount} ৳ Send Money করুন।\n"
        "২. সেন্ড মানি সফল হলে প্রাপ্ত মেসেজ থেকে TrxID কপি করুন।\n"
        "৩. নিচে আপনার TrxID টি লিখে পাঠিয়ে দিন।\n"
        f"{_DIVIDER}\n⚡ টাকা পাঠানোর পর অ্যাডমিন ম্যানুয়ালি যাচাই করে ব্যালেন্স যোগ করে দেবে!\n\n"
        f"দয়া করে আপনার {bn} এর TrxID টি নিচে লিখে পাঠান:",
        reply_markup=cancel_keyboard(),
    )
    return MONEY_TRXID


async def add_money_trxid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_text = update.message.text.strip()
    if raw_text in (BTN_CANCEL, BTN_BACK) or raw_text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)

    trxid = raw_text.upper()
    if not trxid:
        await update.message.reply_text("❌ আপনার TrxID টি পাঠান।", reply_markup=cancel_keyboard())
        return MONEY_TRXID

    with _db_lock, db() as conn:
        dup = conn.execute(
            "SELECT 1 FROM money_requests WHERE trxid = ? AND status != 'rejected'",
            (trxid,),
        ).fetchone()
    if dup:
        await update.message.reply_text(
            "⚠️ এই TrxID আগেই ব্যবহার হয়েছে। অনুগ্রহ করে সঠিক/নতুন TrxID দিন, অথবা ❌ Cancel চাপুন।",
            reply_markup=cancel_keyboard(),
        )
        return MONEY_TRXID

    amount = context.user_data.get("money_amount")
    method = context.user_data.get("money_method", "bkash")
    name = dict(DEPOSIT_METHODS).get(method, "bKash")
    number = get_method_number(method)
    _, bn = _METHOD_BN.get(method, ("🔴", "বিকাশ"))
    user = update.effective_user
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO money_requests (user_id, amount, trxid, method) VALUES (?, ?, ?, ?)",
            (user.id, amount, trxid, method),
        )
        req_id = cur.lastrowid
        conn.commit()
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{req_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{req_id}"),
        ]
    ]
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💰 New Add Money Request\n\n"
            f"👤 User: {user.full_name} (@{user.username or '-'}, ID: {user.id})\n"
            f"💳 Method: {name}\n💵 Amount: ৳{amount}\n🔢 TrxID: {trxid}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        pass
    await update.message.reply_text(
        "✅ ডিপোজিট রিকোয়েস্ট গ্রহণ করা হয়েছে!\n\n"
        "🕰️ আপনার ডিপোজিট রিকোয়েস্টটি বর্তমানে পেন্ডিং রয়েছে।\n\n"
        "👨‍💼 এডমিন রিভিউ: অ্যাডমিন ম্যানুয়ালি আপনার ট্রানজেকশনটি যাচাই করে ৬ ঘণ্টার মধ্যে ব্যালেন্স যোগ করে দেবে।\n\n"
        "❗ রিকোয়েস্টটি ইতিমধ্যে এডমিনের কাছে চলে গেছে, তাই সাপোর্ট আইডিতে মেসেজ বা এসএমএস করার প্রয়োজন নেই। ধৈর্য ধরে অপেক্ষা করুন!\n\n"
        f"💳 ডিপোজিট মাধ্যম: {bn}\n📱 প্রেরক নম্বর: {number}\n💵 ডিপোজিট পরিমাণ: {amount} ৳\n"
        f"🔑 TrxID: {trxid}\n⏳ স্ট্যাটাস: ম্যানুয়াল রিভিউ পেন্ডিং (Pending)",
        reply_markup=menu_for(update.effective_user.id),
    )
    _clear_flow_data(context)
    return ConversationHandler.END


async def money_request_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ Only the admin can do this.")
        return
    action, req_id_str = query.data.split("_", 1)
    req_id = int(req_id_str)
    with _db_lock, db() as conn:
        req = conn.execute("SELECT * FROM money_requests WHERE id = ?", (req_id,)).fetchone()
        if req is None or req["status"] != "pending":
            await query.edit_message_text("⚠️ This request is no longer pending.")
            return
        status = "approved" if action == "approve" else "rejected"
        conn.execute("UPDATE money_requests SET status = ? WHERE id = ?", (status, req_id))
        conn.commit()
    method_key = (req["method"] or "bkash").lower()
    method_name = dict(DEPOSIT_METHODS).get(method_key, "bKash")
    if action == "approve":
        get_user(req["user_id"])
        new_balance = change_balance(req["user_id"], req["amount"])
        try:
            await context.bot.send_message(
                req["user_id"],
                f"✅ Your ৳{req['amount']} Add Money request has been approved!\n💳 Current balance: ৳{new_balance}",
            )
        except Exception:
            pass
        await query.edit_message_text(
            f"✅ Approved — ৳{req['amount']} ({method_name}) has been added to User {req['user_id']}'s balance."
        )
    else:
        try:
            await context.bot.send_message(
                req["user_id"],
                f"❌ Your ৳{req['amount']} Add Money request was rejected. If your TrxID is correct, please request again.",
            )
        except Exception:
            pass
        await query.edit_message_text(
            f"❌ Rejected — User {req['user_id']}'s ৳{req['amount']} ({method_name}) request has been rejected."
        )


# ------------------------------------------------------------- refer earn
async def refer_earn_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    count, earnings, _ = referral_stats(user.id)
    username = context.bot.username or "YourBotUsername"
    link = f"https://t.me/{username}?start=ref_{user.id}"
    await update.message.reply_text(
        "🎁 Earn With Referrals\n\nShare your personal link and earn "
        f"৳{get_refer_bonus()} for every new friend who joins!\n\n"
        f"🔗 Your Refer Link:\n{link}\n\n"
        f"💰 Total Referral Earnings: ৳{earnings}\n👥 Total Friends Referred: {count}\n\n"
        "✨ No limit — the more friends you invite, the more you earn. Your bonus is added to your balance instantly.",
        reply_markup=menu_for(user.id),
    )
    return ConversationHandler.END


# ------------------------------------------------------------ my referral
async def my_referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    count, earnings, referred = referral_stats(user.id)
    if not referred:
        await update.message.reply_text(
            "🔗 My Referrals\n\n😊 No one has joined with your link yet — no problem!\n\n"
            f"Share your link with friends and earn ৳{get_refer_bonus()} for every new member.\n\n"
            f"👉 Get your link from {BTN_REFER_EARN} and start earning today!",
            reply_markup=menu_for(user.id),
        )
        return ConversationHandler.END
    lines = [
        "🔗 My Referrals\n",
        f"👥 Total Friends Referred: {count}",
        f"💰 Total Earnings: ৳{earnings}\n",
        "🌟 Your Referred Friends:",
    ]
    for rid, first_name, uname in referred:
        label = first_name or "-"
        if uname:
            label += f" (@{uname})"
        else:
            label += f" (ID: {rid})"
        lines.append(f"• {label}")
    lines.append("\n🎉 Keep sharing your link to earn more!")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_for(user.id))
    return ConversationHandler.END


# ------------------------------------------------------------- my balance
async def balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    balance = get_balance(user.id)
    await update.message.reply_text(
        f"💳 Your current balance: ৳{balance}",
        reply_markup=menu_for(user.id),
    )
    return ConversationHandler.END


# ------------------------------------------------------------- my account
async def my_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    get_user(user.id, user.username, user.first_name)
    balance = get_balance(user.id)
    count, earnings, _ = referral_stats(user.id)
    name_line = user.first_name or "-"
    if user.username:
        name_line += f" (@{user.username})"
    await update.message.reply_text(
        "👤 My Account\n\n"
        f"📛 Name: {name_line}\n🆔 User ID: {user.id}\n💳 Balance: ৳{balance}\n"
        f"👥 Total refers: {count}\n💰 Referral earning: ৳{earnings}",
        reply_markup=menu_for(user.id),
    )
    return ConversationHandler.END


# ------------------------------------------------------------ how it works
def how_it_works_text() -> str:
    return (
        "❓ How it works\n\n"
        f"📁 To buy a file:\nGo to {BTN_PREMIUM}, tap a file name, then tap the {BTN_BUY} button. "
        "If you have enough balance, you will get the file right away.\n\n"
        f"💰 To add money:\nEnter an amount in {BTN_MONEY}, send the money via bKash, Nagad or Rocket and send the TrxID. "
        "Your balance will be updated once the admin approves it.\n\n"
        f"👥 Earn With Referrals:\nShare your link from {BTN_REFER_EARN}. Every time a new friend joins with your link, "
        f"you earn ৳{get_refer_bonus()} — added straight to your balance. No limit!"
    )


async def how_it_works_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        how_it_works_text(),
        reply_markup=menu_for(update.effective_user.id),
    )
    return ConversationHandler.END


# ------------------------------------------------------------------ support
async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = get_support_username()
    if username:
        text = (
            f"🛟 Need help? Message the admin directly on Telegram:\n@{username}\nhttps://t.me/{username}\n\n"
            "Or write your problem here, it will be sent to the admin:"
        )
    else:
        text = f"🛟 Contact the admin: {get_support_id()}\n\nWrite your problem here, it will be sent to the admin:"
    await update.message.reply_text(text, reply_markup=cancel_keyboard())
    return SUPPORT_MSG


async def support_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    problem = update.message.text.strip()
    if problem in (BTN_CANCEL, BTN_BACK) or problem.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"🛟 New Support Message\n\n👤 User: {user.full_name} (@{user.username or '-'}, ID: {user.id})\n\n📝 Problem:\n{problem}",
        )
    except Exception:
        pass
    await update.message.reply_text(
        "✅ Your message has been sent to the admin. The admin will reply to you directly.",
        reply_markup=menu_for(user.id),
    )
    return ConversationHandler.END


# ------------------------------------------------------------------- admin
BTN_P_ADD = "➕ Product Add"
BTN_P_LIST = "📋 Product List"
BTN_P_DEL = "🗑️ Product Delete"
BTN_P_PENDING = "⏳ Pending Request"
BTN_P_BAL = "💰 Balance Add"
BTN_P_BKASH = "⚙️ bKash Number Set"
BTN_P_SUPPORT = "🛟 Support Username Set"
BTN_P_BONUS = "🎁 Refer Bonus Set"
BTN_P_FORCE = "📢 Force Channel Set"
BTN_P_NAGAD = "⚙️ Nagad Number Set"
BTN_P_ROCKET = "⚙️ Rocket Number Set"
BTN_P_TOGGLE_BKASH = "🔄 Toggle bKash"
BTN_P_TOGGLE_NAGAD = "🔄 Toggle Nagad"
BTN_P_TOGGLE_ROCKET = "🔄 Toggle Rocket"


def admin_panel_markup() -> ReplyKeyboardMarkup:
    rows = [
        [_mkb(BTN_P_ADD, "success"), _mkb(BTN_P_LIST, "primary"), _mkb(BTN_P_DEL, "danger")],
        [_mkb(BTN_P_PENDING, "primary"), _mkb(BTN_P_BAL, "success")],
        [_mkb(BTN_P_BKASH, "primary"), _mkb(BTN_P_SUPPORT, "primary")],
        [_mkb(BTN_P_NAGAD, "primary"), _mkb(BTN_P_ROCKET, "primary")],
        [_mkb(BTN_P_TOGGLE_BKASH, "primary"), _mkb(BTN_P_TOGGLE_NAGAD, "primary"), _mkb(BTN_P_TOGGLE_ROCKET, "primary")],
        [_mkb(BTN_P_BONUS, "success"), _mkb(BTN_P_FORCE, "primary")],
        [_mkb(BTN_BACK, "primary")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _admin_only(update: Update) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Only the admin can use this.",
            reply_markup=menu_for(update.effective_user.id),
        )
        return False
    return True


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    context.user_data["nav"] = "admin"
    await update.message.reply_text(
        "⚙️ Admin Panel\n🛍️ Manage your shop — choose an option below:\n\n"
        f"💳 Deposit methods:\n{deposit_methods_status_text()}",
        reply_markup=admin_panel_markup(),
    )
    return ConversationHandler.END


async def set_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Only the admin can use this.")
        return ConversationHandler.END
    if not context.args:
        await update.message.reply_text("Usage: /setbkash <bKash number>\nExample: /setbkash 01XXXXXXXXX")
        return ConversationHandler.END
    set_bkash_number(context.args[0].strip())
    await update.message.reply_text(f"✅ bKash number set: {context.args[0].strip()}")
    return ConversationHandler.END


async def panel_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    with _db_lock, db() as conn:
        products = conn.execute("SELECT id, name, price FROM products ORDER BY id").fetchall()
    if not products:
        ptext = "📋 No products yet."
    else:
        lines = ["📋 Product List:\n"]
        for p in products:
            lines.append(f"{p['id']}. {p['name']} — ৳{p['price']}")
        ptext = "\n".join(lines)
    await update.message.reply_text(ptext, reply_markup=admin_panel_markup())
    return ConversationHandler.END


async def panel_delete_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    with _db_lock, db() as conn:
        products = conn.execute("SELECT id, name FROM products ORDER BY id").fetchall()
    if not products:
        await update.message.reply_text("🗑️ There is no product to delete.", reply_markup=admin_panel_markup())
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton(f"🗑️ {p['name']} (#{p['id']})", callback_data=f"del_{p['id']}")]
        for p in products
    ]
    await update.message.reply_text(
        "🗑️ Which product do you want to delete?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def panel_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    with _db_lock, db() as conn:
        reqs = conn.execute("SELECT * FROM money_requests WHERE status = 'pending' ORDER BY id").fetchall()
    if not reqs:
        await update.message.reply_text("⏳ No pending requests.", reply_markup=admin_panel_markup())
        return ConversationHandler.END
    for r in reqs:
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{r['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{r['id']}"),
            ]
        ]
        method_key = (r["method"] or "bkash").lower()
        await context.bot.send_message(
            update.effective_chat.id,
            f"💰 Request #{r['id']}\n👤 User ID: {r['user_id']}\n💵 Amount: ৳{r['amount']}\n"
            f"💳 Method: {dict(DEPOSIT_METHODS).get(method_key, 'bKash')}\n🔢 TrxID: {r['trxid']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return ConversationHandler.END


async def panel_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    await update.message.reply_text("➕ Enter the product name:", reply_markup=cancel_keyboard())
    return ADD_NAME


async def panel_add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "💰 Whose balance do you want to increase? Enter the User ID:",
        reply_markup=cancel_keyboard(),
    )
    return BAL_USER


async def panel_set_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    current = get_bkash_number() or "not set"
    await update.message.reply_text(
        f"⚙️ Current bKash number: {current}\nEnter a new bKash number:",
        reply_markup=cancel_keyboard(),
    )
    return SET_BKASH


async def panel_set_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    current = get_nagad_number() or "not set"
    await update.message.reply_text(
        f"⚙️ Current Nagad number: {current}\nEnter a new Nagad number:",
        reply_markup=cancel_keyboard(),
    )
    return SET_NAGAD


async def panel_set_rocket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    current = get_rocket_number() or "not set"
    await update.message.reply_text(
        f"⚙️ Current Rocket number: {current}\nEnter a new Rocket number:",
        reply_markup=cancel_keyboard(),
    )
    return SET_ROCKET


async def panel_set_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    current = get_support_username()
    current_text = f"@{current}" if current else "not set"
    await update.message.reply_text(
        f"🛟 Current Support Username: {current_text}\nEnter your Telegram username (without @):",
        reply_markup=cancel_keyboard(),
    )
    return SET_SUPPORT


async def panel_set_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    current = get_refer_bonus()
    await update.message.reply_text(
        f"🎁 Current Refer Bonus: ৳{current}\nEnter a new Refer Bonus amount (taka, numbers only):",
        reply_markup=cancel_keyboard(),
    )
    return SET_BONUS


async def panel_set_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    current = get_force_channel() or "OFF (not set)"
    await update.message.reply_text(
        f"📢 Current Force Channel: {current}\nEnter a channel username (e.g. @mychannel) or OFF to disable.\n"
        "Note: the bot must be an admin of the channel for membership checks to work.",
        reply_markup=cancel_keyboard(),
    )
    return SET_FORCE


async def _toggle_method(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str) -> int:
    if not await _admin_only(update):
        return ConversationHandler.END
    new_state = toggle_method(method)
    name = dict(DEPOSIT_METHODS)[method]
    await update.message.reply_text(
        f"✅ {name} deposits are now {'ON' if new_state else 'OFF'}.\n\n"
        f"💳 Deposit methods:\n{deposit_methods_status_text()}",
        reply_markup=admin_panel_markup(),
    )
    return ConversationHandler.END


async def panel_toggle_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _toggle_method(update, context, "bkash")


async def panel_toggle_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _toggle_method(update, context, "nagad")


async def panel_toggle_rocket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _toggle_method(update, context, "rocket")


async def admin_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ Only the admin can do this.")
        return
    product_id = int(query.data.split("_", 1)[1])
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
    await query.edit_message_text(f"🗑️ Product #{product_id} has been deleted.")


# -------------------------------------------- admin: settings (steps)
async def set_bkash_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = update.message.text.strip()
    if number in (BTN_CANCEL, BTN_BACK) or number.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    if not number:
        await update.message.reply_text("❌ It cannot be empty. Please enter again:", reply_markup=cancel_keyboard())
        return SET_BKASH
    set_bkash_number(number)
    await update.message.reply_text(f"✅ bKash number set: {number}", reply_markup=admin_panel_markup())
    return ConversationHandler.END


async def set_nagad_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = update.message.text.strip()
    if number in (BTN_CANCEL, BTN_BACK) or number.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    if not number:
        await update.message.reply_text("❌ It cannot be empty. Please enter again:", reply_markup=cancel_keyboard())
        return SET_NAGAD
    set_nagad_number(number)
    await update.message.reply_text(f"✅ Nagad number set: {number}", reply_markup=admin_panel_markup())
    return ConversationHandler.END


async def set_rocket_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = update.message.text.strip()
    if number in (BTN_CANCEL, BTN_BACK) or number.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    if not number:
        await update.message.reply_text("❌ It cannot be empty. Please enter again:", reply_markup=cancel_keyboard())
        return SET_ROCKET
    set_rocket_number(number)
    await update.message.reply_text(f"✅ Rocket number set: {number}", reply_markup=admin_panel_markup())
    return ConversationHandler.END


async def set_support_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if raw in (BTN_CANCEL, BTN_BACK) or raw.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    username = raw.lstrip("@")
    if not (5 <= len(username) <= 32 and all(c.isalnum() or c == "_" for c in username)):
        await update.message.reply_text(
            "❌ Please enter a valid Telegram username (letters, numbers and _ only, without @).",
            reply_markup=cancel_keyboard(),
        )
        return SET_SUPPORT
    set_support_username(username)
    await update.message.reply_text(f"✅ Support username set: @{username}", reply_markup=admin_panel_markup())
    return ConversationHandler.END


async def set_bonus_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if raw in (BTN_CANCEL, BTN_BACK) or raw.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    try:
        amount = int(raw)
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid amount (0 or more).", reply_markup=cancel_keyboard())
        return SET_BONUS
    set_refer_bonus(amount)
    await update.message.reply_text(f"✅ Refer Bonus set: ৳{amount}", reply_markup=admin_panel_markup())
    return ConversationHandler.END


async def set_force_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    if text.upper() == "OFF":
        set_force_channel("")
        await update.message.reply_text(
            "✅ Force Channel disabled. Users can now use the bot without joining any channel.",
            reply_markup=admin_panel_markup(),
        )
        return ConversationHandler.END
    channel = text if text.startswith("@") else f"@{text}"
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,}", channel):
        await update.message.reply_text(
            "❌ Please enter a valid channel username starting with @ (e.g. @mychannel) or OFF to disable.",
            reply_markup=cancel_keyboard(),
        )
        return SET_FORCE
    set_force_channel(channel)
    await update.message.reply_text(
        f"✅ Force Channel set: {channel}\nUsers must join this channel before using the bot. "
        "Make sure the bot is an admin of the channel.",
        reply_markup=admin_panel_markup(),
    )
    return ConversationHandler.END


# -------------------------------------------- admin: add product (steps)
async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    context.user_data["prod_name"] = text
    await update.message.reply_text("🖼️ Now send the product photo:", reply_markup=cancel_keyboard())
    return ADD_PHOTO


async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Please send a photo (as a photo, not a file).",
            reply_markup=cancel_keyboard(),
        )
        return ADD_PHOTO
    context.user_data["prod_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("📝 Now write the product description:", reply_markup=cancel_keyboard())
    return ADD_DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    context.user_data["prod_desc"] = text
    await update.message.reply_text(
        "💰 Now enter the price (numbers only, e.g. 100):",
        reply_markup=cancel_keyboard(),
    )
    return ADD_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    try:
        price = int(text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid price (e.g. 100).", reply_markup=cancel_keyboard())
        return ADD_PRICE
    context.user_data["prod_price"] = price
    await update.message.reply_text("📎 Now send the file as a document:", reply_markup=cancel_keyboard())
    return ADD_FILE


async def add_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.document:
        await update.message.reply_text("❌ Please send the file as a document.", reply_markup=cancel_keyboard())
        return ADD_FILE
    data = context.user_data
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO products (name, photo_id, description, price, file_id) VALUES (?, ?, ?, ?, ?)",
            (
                data["prod_name"],
                data["prod_photo"],
                data["prod_desc"],
                data["prod_price"],
                update.message.document.file_id,
            ),
        )
        conn.commit()
        prod_id = cur.lastrowid
    await update.message.reply_text(
        f"✅ Product added!\n📌 {data['prod_name']} — ৳{data['prod_price']} (ID: #{prod_id})",
        reply_markup=admin_panel_markup(),
    )
    _clear_flow_data(context)
    return ConversationHandler.END


# ------------------------------------------- admin: manual balance add
async def bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    try:
        user_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid User ID (numbers only).",
            reply_markup=cancel_keyboard(),
        )
        return BAL_USER
    context.user_data["bal_user"] = user_id
    await update.message.reply_text(
        "💰 How much money do you want to add? Enter the number:",
        reply_markup=cancel_keyboard(),
    )
    return BAL_AMOUNT


async def bal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in (BTN_CANCEL, BTN_BACK) or text.upper() == BTN_CANCEL.upper():
        return await cancel(update, context)
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid number (e.g. 100).",
            reply_markup=cancel_keyboard(),
        )
        return BAL_AMOUNT
    user_id = context.user_data.pop("bal_user")
    get_user(user_id)
    new_balance = change_balance(user_id, amount)
    await update.message.reply_text(
        f"✅ ৳{amount} has been added to User {user_id}'s balance. Current balance: ৳{new_balance}",
        reply_markup=admin_panel_markup(),
    )
    try:
        await context.bot.send_message(
            user_id,
            f"💰 The admin has added ৳{amount} to your balance.\n💳 Current balance: ৳{new_balance}",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow_data(context)
    nav = context.user_data.get("nav", "main")
    user_id = update.effective_user.id
    if nav == "admin" and is_admin(user_id):
        markup = admin_panel_markup()
    else:
        context.user_data["nav"] = "main"
        markup = menu_for(user_id)
    await update.message.reply_text("❌ Cancelled.", reply_markup=markup)
    return ConversationHandler.END


# ------------------------------------------- menu-button router
MENU_ACTIONS = {
    BTN_PREMIUM: premium_files_menu,
    BTN_REFER_EARN: refer_earn_menu,
    BTN_BALANCE: balance_menu,
    BTN_REFERRAL: my_referral_menu,
    BTN_MONEY: add_money_start,
    BTN_ACCOUNT: my_account_menu,
    BTN_HOW: how_it_works_menu,
    BTN_SUPPORT: support_start,
    BTN_ADMIN: admin_panel,
    BTN_P_ADD: panel_add_product,
    BTN_P_LIST: panel_list,
    BTN_P_DEL: panel_delete_picker,
    BTN_P_PENDING: panel_pending,
    BTN_P_BAL: panel_add_balance,
    BTN_P_BKASH: panel_set_bkash,
    BTN_P_NAGAD: panel_set_nagad,
    BTN_P_ROCKET: panel_set_rocket,
    BTN_P_TOGGLE_BKASH: panel_toggle_bkash,
    BTN_P_TOGGLE_NAGAD: panel_toggle_nagad,
    BTN_P_TOGGLE_ROCKET: panel_toggle_rocket,
    BTN_P_SUPPORT: panel_set_support,
    BTN_P_BONUS: panel_set_bonus,
    BTN_P_FORCE: panel_set_force,
    BTN_BACK: back_button,
    BTN_CANCEL: cancel,
}
_MENU_PATTERN = "^(" + "|".join(re.escape(b) for b in MENU_ACTIONS) + ")$"


def _clear_flow_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("money_amount", None)
    context.user_data.pop("money_method", None)
    for key in ("prod_name", "prod_photo", "prod_desc", "prod_price", "bal_user"):
        context.user_data.pop(key, None)


def _make_menu_router(current_state: int | None):
    """Returns an async handler (NOT a coroutine) so MessageHandler can call it properly."""
    async def router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        action = MENU_ACTIONS.get(text)
        if action is None:
            return ConversationHandler.END
        _clear_flow_data(context)
        result = await action(update, context)
        return result if result is not None else ConversationHandler.END

    return router


# ------------------------------------------------- friendly step hints
async def _hint(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, state: int) -> int:
    await update.message.reply_text(text)
    return state


async def hint_name(update, context):
    return await _hint(update, context, "📌 Please write the product name (or tap ❌ Cancel).", ADD_NAME)


async def hint_photo(update, context):
    return await _hint(update, context, "🖼️ Please send the product photo (as a photo, not a file) or tap ❌ Cancel.", ADD_PHOTO)


async def hint_desc(update, context):
    return await _hint(update, context, "📝 Please write the product description (or tap ❌ Cancel).", ADD_DESC)


async def hint_price(update, context):
    return await _hint(update, context, "💰 Please enter the price in numbers only (e.g. 100).", ADD_PRICE)


async def hint_file(update, context):
    return await _hint(update, context, "📎 Please send the file as a document (or tap ❌ Cancel).", ADD_FILE)


async def hint_bal_user(update, context):
    return await _hint(update, context, "🆔 Please enter the User ID (numbers only).", BAL_USER)


async def hint_bal_amount(update, context):
    return await _hint(update, context, "💰 Please enter the number (e.g. 100).", BAL_AMOUNT)


async def hint_money_amount(update, context):
    return await _hint(update, context, "💰 Please enter only the number (e.g. 100) or tap ❌ Cancel.", MONEY_AMOUNT)


async def hint_money_trxid(update, context):
    return await _hint(update, context, "🔢 Please write the TrxID or tap ❌ Cancel.", MONEY_TRXID)


async def hint_support(update, context):
    return await _hint(update, context, "📝 Please write your problem or tap ❌ Cancel.", SUPPORT_MSG)


async def hint_bkash(update, context):
    return await _hint(update, context, "📱 Please enter the bKash number.", SET_BKASH)


async def hint_money_method(update, context):
    return await _hint(update, context, "💳 Please choose a payment method from the buttons.", MONEY_METHOD)


async def hint_nagad(update, context):
    return await _hint(update, context, "📱 Please enter the Nagad number.", SET_NAGAD)


async def hint_rocket(update, context):
    return await _hint(update, context, "📱 Please enter the Rocket number.", SET_ROCKET)


async def hint_support_id(update, context):
    return await _hint(update, context, "🛟 Please enter the Support username (without @).", SET_SUPPORT)


async def hint_bonus(update, context):
    return await _hint(update, context, "🎁 Please enter the bonus amount in numbers.", SET_BONUS)


async def hint_force(update, context):
    return await _hint(update, context, "📢 Please enter the channel username (e.g. @mychannel) or OFF to disable.", SET_FORCE)


def _state_handlers(state: int, *handlers, hint=None):
    router = _make_menu_router(state)
    ordered = [MessageHandler(filters.Regex(_MENU_PATTERN), router)]
    ordered.extend(handlers)
    if hint is not None:
        ordered.append(MessageHandler(filters.ALL & ~filters.COMMAND, hint))
    return ordered


SHARED_STATES = {
    ADD_NAME: _state_handlers(ADD_NAME, MessageHandler(filters.TEXT & ~filters.COMMAND, add_name), hint=hint_name),
    ADD_PHOTO: _state_handlers(ADD_PHOTO, MessageHandler(filters.PHOTO, add_photo), hint=hint_photo),
    ADD_DESC: _state_handlers(ADD_DESC, MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc), hint=hint_desc),
    ADD_PRICE: _state_handlers(ADD_PRICE, MessageHandler(filters.TEXT & ~filters.COMMAND, add_price), hint=hint_price),
    ADD_FILE: _state_handlers(ADD_FILE, MessageHandler(filters.Document.ALL, add_file), hint=hint_file),
    BAL_USER: _state_handlers(BAL_USER, MessageHandler(filters.TEXT & ~filters.COMMAND, bal_user), hint=hint_bal_user),
    BAL_AMOUNT: _state_handlers(BAL_AMOUNT, MessageHandler(filters.TEXT & ~filters.COMMAND, bal_amount), hint=hint_bal_amount),
    MONEY_AMOUNT: _state_handlers(MONEY_AMOUNT, MessageHandler(filters.TEXT & ~filters.COMMAND, add_money_amount), hint=hint_money_amount),
    MONEY_TRXID: _state_handlers(MONEY_TRXID, MessageHandler(filters.TEXT & ~filters.COMMAND, add_money_trxid), hint=hint_money_trxid),
    MONEY_METHOD: _state_handlers(MONEY_METHOD, MessageHandler(filters.TEXT & ~filters.COMMAND, add_money_method), hint=hint_money_method),
    SUPPORT_MSG: _state_handlers(SUPPORT_MSG, MessageHandler(filters.TEXT & ~filters.COMMAND, support_receive), hint=hint_support),
    SET_BKASH: _state_handlers(SET_BKASH, MessageHandler(filters.TEXT & ~filters.COMMAND, set_bkash_step), hint=hint_bkash),
    SET_NAGAD: _state_handlers(SET_NAGAD, MessageHandler(filters.TEXT & ~filters.COMMAND, set_nagad_step), hint=hint_nagad),
    SET_ROCKET: _state_handlers(SET_ROCKET, MessageHandler(filters.TEXT & ~filters.COMMAND, set_rocket_step), hint=hint_rocket),
    SET_SUPPORT: _state_handlers(SET_SUPPORT, MessageHandler(filters.TEXT & ~filters.COMMAND, set_support_step), hint=hint_support_id),
    SET_BONUS: _state_handlers(SET_BONUS, MessageHandler(filters.TEXT & ~filters.COMMAND, set_bonus_step), hint=hint_bonus),
    SET_FORCE: _state_handlers(SET_FORCE, MessageHandler(filters.TEXT & ~filters.COMMAND, set_force_step), hint=hint_force),
}


# ------------------------------------------- command fallbacks
async def restart_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow_data(context)
    return await start(update, context)


async def restart_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow_data(context)
    return await admin_panel(update, context)


async def restart_setbkash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow_data(context)
    return await set_bkash(update, context)


CONV_FALLBACKS = [
    CommandHandler("cancel", cancel),
    CommandHandler("start", restart_start),
    CommandHandler("admin", restart_admin),
    CommandHandler("setbkash", restart_setbkash),
    MessageHandler(filters.Regex(f"^({re.escape(BTN_CANCEL)}|{re.escape(BTN_BACK)})$"), cancel),
]


# ------------------------------------------------------- global error net
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Bot error", exc_info=context.error)
    try:
        message = update.effective_message if isinstance(update, Update) else None
        if message is not None:
            user_id = update.effective_user.id if update.effective_user else 0
            await message.reply_text("⚠️ Something went wrong. Please try again.", reply_markup=menu_for(user_id))
    except Exception:
        pass


# ------------------------------------------------------- keep-alive
def create_keepalive_app():
    if not _FLASK_AVAILABLE:
        return None
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "OK", 200

    @app.route("/ping")
    def ping():
        return "OK", 200

    return app


keepalive_app = create_keepalive_app()


def _run_keepalive_server(app, port: int) -> None:
    try:
        app.run(host="0.0.0.0", port=port, use_reloader=False)
    except Exception as exc:
        logger.warning("Keep-alive server stopped: %s", exc)


def start_keepalive_server() -> None:
    if keepalive_app is None:
        logger.warning("Flask is not installed; keep-alive endpoint disabled, polling continues normally.")
        return
    try:
        port = int(os.environ.get("PORT", "8080") or 8080)
    except ValueError:
        port = 8080
    thread = threading.Thread(target=_run_keepalive_server, args=(keepalive_app, port), daemon=True, name="keepalive")
    thread.start()
    logger.info("Keep-alive server listening on port %s (/ and /ping).", port)


# --------------------------------------------------------------------- main
def main() -> None:
    if not TOKEN:
        raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN environment variable is not set.")
    if ADMIN_ID == 0:
        raise SystemExit("ERROR: ADMIN_ID environment variable is not set.")
    init_db()

    app = Application.builder().token(TOKEN).build()

    # Single unified ConversationHandler so switching between any menu/flow works without conflict
    master_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_MONEY)}$"), add_money_start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_SUPPORT)}$"), support_start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_ADD)}$"), panel_add_product),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_BAL)}$"), panel_add_balance),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_BKASH)}$"), panel_set_bkash),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_NAGAD)}$"), panel_set_nagad),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_ROCKET)}$"), panel_set_rocket),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_SUPPORT)}$"), panel_set_support),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_BONUS)}$"), panel_set_bonus),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_P_FORCE)}$"), panel_set_force),
        ],
        states=SHARED_STATES,
        fallbacks=CONV_FALLBACKS,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("setbkash", set_bkash))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(master_conv)

    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_P_LIST)}$"), panel_list))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_P_DEL)}$"), panel_delete_picker))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_P_PENDING)}$"), panel_pending))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_P_TOGGLE_BKASH)}$"), panel_toggle_bkash))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_P_TOGGLE_NAGAD)}$"), panel_toggle_nagad))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_P_TOGGLE_ROCKET)}$"), panel_toggle_rocket))

    app.add_handler(CallbackQueryHandler(force_check, pattern="^force_check$"))
    app.add_handler(CallbackQueryHandler(admin_delete_product, pattern="^del_[0-9]+$"))
    app.add_handler(CallbackQueryHandler(money_request_decision, pattern="^(approve|reject)_[0-9]+$"))

    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_PREMIUM)}$"), premium_files_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_REFER_EARN)}$"), refer_earn_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_BALANCE)}$"), balance_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_REFERRAL)}$"), my_referral_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ACCOUNT)}$"), my_account_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HOW)}$"), how_it_works_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN)}$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_BUY)}$"), buy_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_BACK)}$"), back_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, product_detail_by_name))

    app.add_error_handler(error_handler)

    start_keepalive_server()

    logger.info("Paid File Sell Bot v5.3 is running.")

    # FIXED FOR RENDER: close_loop=False & drop_pending_updates=True
    app.run_polling(close_loop=False, drop_pending_updates=True)


if __name__ == "__main__":
    main()