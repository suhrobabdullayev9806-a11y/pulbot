"""
PulBot — Referral bot (so'mda balans) - aiogram 3 + SQLite.
Hamma narx/limit/matnlar admin paneldan boshqariladi.
Barcha tugmalar rangli (Bot API: success/danger/primary).

Render (Web Service) uchun tayyor: PORT muhit o'zgaruvchisiga
sog'liq (health) serveri ishlaydi.

Ishga tushirish:
    BOT_TOKEN=... OWNER_ID=123 python PulBot.py
"""
import asyncio
import csv
import html
import io
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent,
                           InlineKeyboardButton, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "8080"))

UZ_TZ = timezone(timedelta(hours=5))  # O'zbekiston (UTC+5)


def today_local() -> date:
    return datetime.now(UZ_TZ).date()


# ----------------------------------------------------------------------------
# SOZLAMALAR (boshlang'ich qiymatlar; keyin admin paneldan o'zgaradi)
# key: (default, sarlavha, guruh)   guruh: price | limit | sys | text | hidden
# ----------------------------------------------------------------------------
SETTINGS = {
    "start_bonus": (0, "Start bonus (so'm, 0 = o'chirilgan)", "price"),
    "ref_l1": (300, "Taklif uchun bonus, 1-daraja (so'm)", "price"),
    "ref_l2": (50, "Taklif uchun bonus, 2-daraja (so'm)", "price"),
    "daily_min": (50, "Kunlik bonus MIN (so'm)", "price"),
    "daily_max": (100, "Kunlik bonus MAX (so'm)", "price"),
    "streak_bonus": (500, "7 kunlik streak bonusi (so'm)", "price"),
    "premium_multiplier": (2, "Premium koeffitsiyenti (x)", "price"),
    "premium_refs": (100, "Premium uchun kerakli takliflar soni", "price"),
    "week_prize_1": (100000, "Haftalik konkurs 1-o'rin (so'm)", "price"),
    "week_prize_2": (50000, "Haftalik konkurs 2-o'rin (so'm)", "price"),
    "week_prize_3": (25000, "Haftalik konkurs 3-o'rin (so'm)", "price"),
    "min_withdraw": (20000, "Minimal yechish summasi (so'm)", "price"),
    "max_withdraw_daily": (200000, "Kunlik maksimal yechish (so'm)", "price"),
    "withdraw_fee_percent": (0, "Yechish komissiyasi (%)", "price"),
    "daily_ref_limit": (50, "Kunlik taklif limiti (ta)", "limit"),
    "check_days": (3, "Obunani qayta tekshirish (necha kundan keyin)", "limit"),
    "require_phone": (0, "Telefon tasdiqlash (1=ha, 0=yo'q)", "limit"),
    "maintenance": (0, "Texnik rejim (1=yoqilgan, 0=o'chiq)", "sys"),
    "welcome_text": ("👋 Xush kelibsiz! Do'stlaringizni taklif qiling va so'm ishlang.",
                     "Xush kelibsiz matni", "text"),
    "rules_text": ("📜 <b>Qoidalar</b>\n\n1. Ball faqat do'stingiz barcha kanallarga obuna "
                   "bo'lgandan keyin beriladi.\n2. Obunadan chiqib ketsa, bonus qaytarib olinadi.\n"
                   "3. Soxta akkauntlar bloklanadi.\n4. Botda hech qachon pul to'lamaysiz.",
                   "Qoidalar matni", "text"),
    "last_week_paid": ("", "", "hidden"),
    "last_report": ("", "", "hidden"),
}

REASONS = {"start": "Start bonus", "referral": "Taklif", "referral_l2": "2-daraja taklif",
           "daily": "Kunlik bonus", "streak": "Streak bonus", "withdraw": "Pul yechish",
           "refund": "Qaytarildi", "revoke": "Bonus bekor qilindi", "contest": "Konkurs mukofoti",
           "admin": "Admin tomonidan"}

PERMS = {
    "dash": {"owner", "moderator", "accountant"},
    "prices": {"owner"}, "wd": {"owner", "accountant"}, "sp": {"owner"},
    "users": {"owner", "moderator"}, "sec": {"owner", "moderator"},
    "bc": {"owner", "moderator"}, "rep": {"owner", "accountant"}, "adm": {"owner"},
}
SECTIONS = [("dash", "📊 Dashboard"), ("prices", "💰 Narxlar va bonuslar"),
            ("wd", "💳 Yechish so'rovlari"), ("sp", "📢 Homiy kanallar"),
            ("users", "👥 Foydalanuvchilar"), ("sec", "🛡 Xavfsizlik"),
            ("bc", "📣 Xabar yuborish"), ("rep", "📈 Hisobotlar"), ("adm", "⚙️ Sozlamalar")]
SECTION_STYLE = {"dash": "primary", "prices": "success", "wd": "danger", "sp": "primary",
                 "users": "success", "sec": "danger", "bc": "success", "rep": "primary", "adm": "primary"}

# Tugma ranglari (Bot API: success=yashil, danger=qizil, primary=ko'k)
OK, BAD, MAIN = "success", "danger", "primary"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, phone TEXT,
  balance INTEGER DEFAULT 0, total_earned INTEGER DEFAULT 0, total_withdrawn INTEGER DEFAULT 0,
  referrer_id INTEGER, is_premium INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0,
  verified INTEGER DEFAULT 0, last_daily TEXT, streak INTEGER DEFAULT 0, created_at INTEGER);
CREATE TABLE IF NOT EXISTS referrals(
  invited_id INTEGER PRIMARY KEY, inviter_id INTEGER, status TEXT DEFAULT 'pending',
  reward INTEGER DEFAULT 0, l2_inviter INTEGER, l2_reward INTEGER DEFAULT 0,
  checked INTEGER DEFAULT 0, created_at INTEGER, confirmed_at INTEGER);
CREATE TABLE IF NOT EXISTS sponsors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, link TEXT, title TEXT,
  reward INTEGER DEFAULT 0, max_members INTEGER DEFAULT 0, joined INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS income(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sponsor_id INTEGER, amount INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS withdrawals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, fee INTEGER DEFAULT 0,
  method TEXT, details TEXT, status TEXT DEFAULT 'pending', admin_id INTEGER,
  reason TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, reason TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, role TEXT);
CREATE INDEX IF NOT EXISTS idx_referrals_inviter ON referrals(inviter_id, status);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, created_at);
"""

db: aiosqlite.Connection


# ----------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ----------------------------------------------------------------------------
def now() -> int:
    return int(time.time())


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def esc(s) -> str:
    return html.escape(str(s or ""))


DIV = "━━━━━━━━━━━━━━━━"
THIN = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def bar(pct, width: int = 10) -> str:
    """Progress-bar: ▓▓▓▓▓░░░░░ 52%"""
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100 * width))
    return "▓" * filled + "░" * (width - filled) + f" {int(pct)}%"


def money(n) -> str:
    return f"<code>{fmt(n)}</code> so'm"


async def q(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchall()


async def q1(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchone()


async def ex(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur


_cache: dict = {}
_cache_ts = 0.0


async def load_settings():
    global _cache, _cache_ts
    _cache = {r["key"]: r["value"] for r in await q("SELECT key, value FROM settings")}
    _cache_ts = time.time()


async def gs(key):
    if time.time() - _cache_ts > 30:
        await load_settings()
    default = SETTINGS[key][0]
    val = _cache.get(key, str(default))
    if isinstance(default, int):
        try:
            return int(val)
        except (TypeError, ValueError):
            return int(default)
    return val


async def set_setting(key, val):
    await ex("INSERT INTO settings(key,value) VALUES(?,?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(val)))
    await load_settings()


async def get_user(uid):
    return await q1("SELECT * FROM users WHERE id=?", (uid,))


async def add_balance(uid, amount, reason, earn=True):
    if amount == 0:
        return
    if amount > 0:
        if earn:
            await db.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?",
                             (amount, amount, uid))
        else:
            await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    else:
        await db.execute("UPDATE users SET balance=MAX(balance+?,0) WHERE id=?", (amount, uid))
    await db.execute("INSERT INTO transactions(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                     (uid, amount, reason, now()))
    await db.commit()


async def role_of(uid):
    if uid == OWNER_ID:
        return "owner"
    r = await q1("SELECT role FROM admins WHERE user_id=?", (uid,))
    return r["role"] if r else None


async def notify_admins(bot, text, roles=("owner", "accountant", "moderator"), kb=None):
    ids = {OWNER_ID} if "owner" in roles else set()
    for r in await q("SELECT user_id, role FROM admins"):
        if r["role"] in roles:
            ids.add(r["user_id"])
    for i in ids:
        if i:
            try:
                await bot.send_message(i, text, reply_markup=kb)
            except Exception:
                pass


async def safe_send(bot, uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
    except Exception:
        pass


async def answer_html(message: Message, text: str, **kw):
    """Admin kiritgan noto'g'ri HTML bo'lsa, oddiy matn sifatida yuboradi."""
    try:
        await message.answer(text, **kw)
    except TelegramBadRequest:
        await message.answer(html.escape(re.sub(r"<[^>]+>", "", text)), **kw)


class Ctx:
    """Message va CallbackQuery ikkalasida ham bir xil ishlaydigan kontekst."""

    def __init__(self, bot: Bot, chat_id: int, uid: int, name: str = ""):
        self.bot = bot
        self.chat_id = chat_id
        self.uid = uid
        self.name = name

    async def send(self, text, **kw):
        return await self.bot.send_message(self.chat_id, text, **kw)


# ----------------------------------------------------------------------------
# TUGMALAR (hammasi rangli)
# ----------------------------------------------------------------------------
def back_btn(sec="home"):
    return InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"adm:{sec}", style=MAIN)


def back_kb(sec="home"):
    kb = InlineKeyboardBuilder()
    kb.row(back_btn(sec))
    return kb


def ib(text, callback_data=None, url=None, style=MAIN):
    """Qisqa inline tugma yaratish."""
    kw = {"text": text, "style": style}
    if callback_data is not None:
        kw["callback_data"] = callback_data
    if url is not None:
        kw["url"] = url
    return InlineKeyboardButton(**kw)


# ----------------------------------------------------------------------------
# OBUNA, TASDIQLASH, REFERRAL MANTIQI
# ----------------------------------------------------------------------------
async def unsubscribed(bot, uid):
    bad = []
    for s in await q("SELECT * FROM sponsors WHERE active=1"):
        try:
            m = await bot.get_chat_member(s["channel_id"], uid)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not m.is_member):
                bad.append(s)
        except Exception:
            pass  # bot kanalda admin emas -> o'tkazib yuboramiz
    return bad


async def gate(bot, uid, chat_id) -> bool:
    """Foydalanuvchi obuna va (kerak bo'lsa) telefon tekshiruvidan o'tganmi."""
    bad = await unsubscribed(bot, uid)
    if bad:
        kb = InlineKeyboardBuilder()
        for s in bad:
            kb.row(ib(f"📢 {esc(s['title'])[:30]}", url=s["link"]))
        kb.row(ib("✅ Tekshirish", callback_data="chk", style=OK))
        await bot.send_message(chat_id,
                               f"🔒 <b>OBUNA TALAB QILINADI</b>\n{DIV}\n"
                               f"Botdan foydalanish uchun quyidagi kanallarga\n"
                               f"obuna bo'ling, so'ng «✅ Tekshirish»ni bosing.\n"
                               f"{THIN}\n"
                               f"<i>Obuna bo'lgach bonuslar avtomatik faollashadi.</i>",
                               reply_markup=kb.as_markup())
        return False
    u = await get_user(uid)
    if await gs("require_phone") and u and not u["phone"]:
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Raqamni yuborish",
                                                           request_contact=True, style=OK)]],
                                 resize_keyboard=True, one_time_keyboard=True)
        await bot.send_message(chat_id, "Telefon raqamingizni tasdiqlang:", reply_markup=kb)
        return False
    await process_verified(bot, uid)
    return True


async def process_verified(bot, uid):
    u = await get_user(uid)
    if not u or u["verified"]:
        return
    await db.execute("UPDATE users SET verified=1 WHERE id=?", (uid,))
    for s in await q("SELECT * FROM sponsors WHERE active=1"):
        await db.execute("INSERT INTO income(sponsor_id,amount,created_at) VALUES(?,?,?)",
                         (s["id"], s["reward"], now()))
        await db.execute("UPDATE sponsors SET joined=joined+1 WHERE id=?", (s["id"],))
        if s["max_members"] > 0 and s["joined"] + 1 >= s["max_members"]:
            await db.execute("UPDATE sponsors SET active=0 WHERE id=?", (s["id"],))
    await db.commit()
    sb = await gs("start_bonus")
    if sb:
        await add_balance(uid, sb, "start")
        await safe_send(bot, uid, f"🎉 Start bonus: <b>+{fmt(sb)} so'm</b>")
    ref = await q1("SELECT * FROM referrals WHERE invited_id=? AND status='pending'", (uid,))
    if ref:
        await confirm_referral(bot, ref)


async def confirm_referral(bot, ref):
    inv = ref["inviter_id"]
    cnt = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed' "
                    "AND confirmed_at>?", (inv, now() - 86400)))["c"]
    if cnt >= await gs("daily_ref_limit"):
        await ex("UPDATE referrals SET status='limited' WHERE invited_id=?", (ref["invited_id"],))
        return
    inviter = await get_user(inv)
    if not inviter or inviter["is_banned"]:
        await ex("UPDATE referrals SET status='revoked' WHERE invited_id=?", (ref["invited_id"],))
        return
    mult = await gs("premium_multiplier") if inviter["is_premium"] else 1
    r1 = await gs("ref_l1") * mult
    await add_balance(inv, r1, "referral")
    l2, r2 = inviter["referrer_id"], 0
    if l2:
        l2u = await get_user(l2)
        r2 = await gs("ref_l2")
        if l2u and not l2u["is_banned"] and r2:
            await add_balance(l2, r2, "referral_l2")
            await safe_send(bot, l2, f"👥 2-daraja taklif: <b>+{fmt(r2)} so'm</b>")
        else:
            l2, r2 = None, 0
    await ex("UPDATE referrals SET status='confirmed', reward=?, l2_inviter=?, l2_reward=?, "
             "confirmed_at=? WHERE invited_id=?", (r1, l2, r2, now(), ref["invited_id"]))
    await safe_send(bot, inv, f"✅ Do'stingiz obuna bo'ldi! <b>+{fmt(r1)} so'm</b>")
    total = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed'",
                      (inv,)))["c"]
    if not inviter["is_premium"] and total >= await gs("premium_refs"):
        await ex("UPDATE users SET is_premium=1 WHERE id=?", (inv,))
        await safe_send(bot, inv, "⭐ Tabriklaymiz! Siz Premium maqomiga ega bo'ldingiz.")


# ----------------------------------------------------------------------------
# MIDDLEWARE: ban, texnik rejim, spamdan himoya
# ----------------------------------------------------------------------------
class Guard(BaseMiddleware):
    def __init__(self):
        self.last = {}

    async def __call__(self, handler, event, data):
        u = event.from_user
        if u and not u.is_bot:
            t = time.monotonic()
            if len(self.last) > 5000:  # xotira to'lmasligi uchun eski yozuvlarni tozalash
                self.last = {k: v for k, v in self.last.items() if t - v < 60}
            if t - self.last.get(u.id, 0) < 0.4:
                return
            self.last[u.id] = t
            row = await get_user(u.id)
            if row and row["is_banned"]:
                return
            if await gs("maintenance") and not await role_of(u.id):
                txt = "🛠 Botda texnik ishlar olib borilmoqda. Keyinroq urinib ko'ring."
                if isinstance(event, CallbackQuery):
                    await event.answer(txt, show_alert=True)
                else:
                    await event.answer(txt)
                return
        return await handler(event, data)


# ----------------------------------------------------------------------------
# FSM HOLATLARI
# ----------------------------------------------------------------------------
class W(StatesGroup):
    amount = State()
    method = State()
    details = State()


class H(StatesGroup):
    msg = State()


class A(StatesGroup):
    val = State()
    sponsor = State()
    user_search = State()
    user_bal = State()
    reject = State()
    bc_msg = State()
    admin_add = State()


user = Router()
admin = Router()

B_BAL, B_INV, B_DAILY, B_WD = "💰 Balansim", "👥 Do'st taklif qilish", "🎁 Kunlik bonus", "💳 Pul yechish"
B_RATE, B_PREM, B_RULES, B_HELP = "🏆 Reyting", "⭐ Premium", "📜 Qoidalar", "🆘 Yordam"
MENU_BTNS = {B_BAL, B_INV, B_DAILY, B_WD, B_RATE, B_PREM, B_RULES, B_HELP}
MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=B_BAL, style=MAIN), KeyboardButton(text=B_INV, style=OK)],
    [KeyboardButton(text=B_DAILY, style=OK), KeyboardButton(text=B_WD, style=BAD)],
    [KeyboardButton(text=B_RATE, style=MAIN), KeyboardButton(text=B_PREM, style=MAIN)],
    [KeyboardButton(text=B_RULES, style=MAIN), KeyboardButton(text=B_HELP, style=OK)]])


def main_kb():
    """/start da chiqadigan rangli tezkor tugmalar."""
    kb = InlineKeyboardBuilder()
    kb.row(ib("🎁 Bonus olish", callback_data="m:daily", style=OK),
           ib("👥 Do'st taklif", callback_data="m:invite", style=OK))
    kb.row(ib("💰 Balansim", callback_data="m:bal", style=MAIN),
           ib("💳 Pul yechish", callback_data="m:wd", style=BAD))
    kb.row(ib("🏆 Reyting", callback_data="m:rate", style=MAIN),
           ib("⭐ Premium", callback_data="m:prem", style=MAIN))
    kb.row(ib("📜 Qoidalar", callback_data="m:rules", style=MAIN),
           ib("🆘 Yordam", callback_data="m:help", style=OK))
    return kb


async def start_card(bot, chat_id, uid):
    """Asosiy menyu + hisob kartochkasi."""
    u = await get_user(uid)
    if not u:
        return
    refs = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed'",
                     (uid,)))["c"]
    daily = "⏳ bugun olingan" if u["last_daily"] == today_local().isoformat() else "✅ mavjud"
    prem = "⭐ Premium" if u["is_premium"] else "🔹 Standart"
    name = esc((u["full_name"] or "do'stim").split()[0][:20])
    await answer_html_msg(bot, chat_id,
                         f"👋 Salom, <b>{name}</b>!\n{DIV}\n"
                         f"🏦 <b>Hisobingiz</b>\n"
                         f"├ 💰 Balans: {money(u['balance'])}\n"
                         f"├ 🎁 Kunlik bonus: {daily}\n"
                         f"├ 👥 Takliflar: <b>{refs} ta</b>\n"
                         f"└ {prem}\n{DIV}\n"
                         f"👇 Quyidagi menyudan foydalaning",
                         reply_markup=MENU)
    await bot.send_message(chat_id, "⚡ <b>Tez amallar</b>",
                           reply_markup=main_kb().as_markup())


async def answer_html_msg(bot, chat_id, text, **kw):
    try:
        await bot.send_message(chat_id, text, **kw)
    except TelegramBadRequest:
        await bot.send_message(chat_id, html.escape(re.sub(r"<[^>]+>", "", text)), **kw)


async def rules_view(bot, chat_id):
    kb = InlineKeyboardBuilder()
    kb.row(ib("🎁 Bonus olish", callback_data="m:daily", style=OK),
           ib("👥 Taklif qilish", callback_data="m:invite", style=OK))
    kb.row(ib("💰 Balansim", callback_data="m:bal", style=MAIN),
           ib("⭐ Premium", callback_data="m:prem", style=MAIN))
    await answer_html_msg(bot, chat_id,
                          f"{await gs('rules_text')}\n{DIV}\n"
                          f"<i>Savollar bo'lsa 🆘 Yordam bo'limiga yozing.</i>",
                          reply_markup=kb.as_markup())



# ----------------------------------------------------------------------------
# FOYDALANUVCHI QISMI
# ----------------------------------------------------------------------------
@user.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    uid = m.from_user.id
    if not await get_user(uid):
        is_new = True
        ref = None
        if command.args and command.args.isdigit() and int(command.args) != uid:
            r = await get_user(int(command.args))
            if r and not r["is_banned"]:
                ref = r["id"]
        await db.execute("INSERT INTO users(id,username,full_name,referrer_id,created_at) VALUES(?,?,?,?,?)",
                         (uid, m.from_user.username, m.from_user.full_name, ref, now()))
        if ref:
            await db.execute("INSERT OR IGNORE INTO referrals(invited_id,inviter_id,created_at) VALUES(?,?,?)",
                             (uid, ref, now()))
        await db.commit()
    else:
        is_new = False
        await ex("UPDATE users SET username=?, full_name=? WHERE id=?",
                 (m.from_user.username, m.from_user.full_name, uid))
    if is_new:
        await answer_html(m, await gs("welcome_text"))
    if await gate(bot, uid, m.chat.id):
        await start_card(bot, m.chat.id, uid)


@user.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Bekor qilindi.", reply_markup=MENU)


@user.callback_query(F.data == "chk")
async def cb_check(c: CallbackQuery, bot: Bot):
    if await gate(bot, c.from_user.id, c.message.chat.id):
        await c.answer("✅ Tasdiqlandi")
        try:
            await c.message.delete()
        except Exception:
            pass
        await start_card(bot, c.message.chat.id, c.from_user.id)
    else:
        await c.answer("Hali hamma kanalga obuna bo'lmadingiz", show_alert=True)


@user.message(F.contact)
async def got_contact(m: Message, bot: Bot):
    if m.contact.user_id != m.from_user.id:
        return await m.answer("Iltimos, o'zingizning raqamingizni yuboring.")
    phone = re.sub(r"\D", "", m.contact.phone_number)
    dup = await q1("SELECT id FROM users WHERE phone=? AND id!=?", (phone, m.from_user.id))
    if dup:
        return await m.answer("❌ Bu raqam boshqa akkauntga biriktirilgan.", reply_markup=ReplyKeyboardRemove())
    await ex("UPDATE users SET phone=? WHERE id=?", (phone, m.from_user.id))
    if await gate(bot, m.from_user.id, m.chat.id):
        await m.answer("✅ Tasdiqlandi", reply_markup=MENU)


@user.message(F.text.in_(MENU_BTNS))
async def menu_router(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = m.from_user.id
    if not await get_user(uid):
        return await m.answer("Avval /start bosing")
    if not await gate(bot, uid, m.chat.id):
        return
    t = m.text
    if t == B_BAL:
        await view_balance(bot, m.chat.id, uid)
    elif t == B_INV:
        await view_invite(bot, m.chat.id, uid, m.from_user.full_name)
    elif t == B_DAILY:
        await view_daily(bot, m.chat.id, uid)
    elif t == B_WD:
        await withdraw_start(m, state)
    elif t == B_RATE:
        await view_rating(bot, m.chat.id, uid)
    elif t == B_PREM:
        await view_premium(bot, m.chat.id, uid)
    elif t == B_RULES:
        await rules_view(bot, m.chat.id)
    elif t == B_HELP:
        await state.set_state(H.msg)
        await m.answer(f"✍️ <b>Yordam</b>\n{DIV}\nSavol yoki muammoingizni yozing.\n"
                       f"Admin tez orada javob beradi.\n\n<i>Bekor qilish: /cancel</i>")


@user.callback_query(F.data.startswith("m:"))
async def menu_cb(c: CallbackQuery, state: FSMContext, bot: Bot):
    """Inline tezkor tugmalar."""
    act = c.data.split(":")[1]
    uid, chat_id = c.from_user.id, c.message.chat.id
    if act == "wd":
        await c.answer()
        await withdraw_start(c.message, state, uid=uid)
        return
    if not await gate(bot, uid, chat_id):
        return await c.answer("Avval kanallarga obuna bo'ling", show_alert=True)
    await c.answer()
    if act == "bal":
        await view_balance(bot, chat_id, uid)
    elif act == "invite":
        await view_invite(bot, chat_id, uid, c.from_user.full_name)
    elif act == "daily":
        await view_daily(bot, chat_id, uid)
    elif act == "rate":
        await view_rating(bot, chat_id, uid)
    elif act == "prem":
        await view_premium(bot, chat_id, uid)
    elif act == "rules":
        await rules_view(bot, chat_id)
    elif act == "help":
        await state.set_state(H.msg)
        await bot.send_message(chat_id, f"🆘 <b>YORDAM</b>\n{DIV}\n"
                                      f"Savol yoki muammoingizni bitta xabarda yozing.\n"
                                      f"Admin tez orada javob beradi.\n\n"
                                      f"<i>Bekor qilish: /cancel</i>")


async def view_balance(bot: Bot, chat_id: int, uid: int):
    u = await get_user(uid)
    if not u:
        return
    rows = await q("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,))
    hist = "\n".join(f"{'🟢' if r['amount'] > 0 else '🔴'} "
                     f"{'+' if r['amount'] > 0 else '−'}{fmt(abs(r['amount'])).replace(' ', '')} so'm · "
                     f"{REASONS.get(r['reason'], r['reason'])}" for r in rows) or "Hali amallar yo'q"
    mn = await gs("min_withdraw")
    pct = (u["balance"] / mn * 100) if mn else 100
    kb = InlineKeyboardBuilder()
    kb.row(ib("🎁 Bonus olish", callback_data="m:daily", style=OK),
           ib("👥 Taklif qilish", callback_data="m:invite", style=OK))
    kb.row(ib("💳 Pul yechish", callback_data="m:wd", style=BAD))
    await bot.send_message(chat_id,
                           f"💼 <b>HISOBIM</b>\n{DIV}\n"
                           f"💳 Balans: <b>{fmt(u['balance'])} so'm</b>\n"
                           f"{THIN}\n"
                           f"📈 Jami topilgan: {money(u['total_earned'])}\n"
                           f"📉 Jami yechilgan: {money(u['total_withdrawn'])}\n"
                           f"⭐ Maqom: <b>{'Premium' if u['is_premium'] else 'Standart'}</b>\n"
                           f"{DIV}\n"
                           f"🎯 Yechish minimumi: {money(mn)}\n"
                           f"{bar(pct)}\n"
                           f"{THIN}\n"
                           f"🧾 <b>Oxirgi amallar</b>\n{hist}",
                           reply_markup=kb.as_markup())


def make_card(name, balance, link) -> bytes | None:
    """Do'stlarga yuborish uchun chiroyli karta."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        W_, H_ = 1000, 560
        img = Image.new("RGB", (W_, H_), (12, 16, 28))
        d = ImageDraw.Draw(img)
        # gradient fon
        for y in range(H_):
            t = y / H_
            d.line([(0, y), (W_, y)], fill=(int(12 + 30 * t), int(16 + 50 * t), int(28 + 90 * t)))
        # oltin ramka
        d.rounded_rectangle([30, 30, W_ - 30, H_ - 30], radius=28, outline=(255, 200, 60), width=3)
        d.ellipse([W_ - 210, -90, W_ + 60, 180], fill=(255, 200, 60))
        d.text((70, 78), "PUL ISHLA UZ", fill=(255, 200, 60), font=ImageFont.load_default(size=30))
        d.text((70, 128), "MEN YIG'DIM", fill=(150, 165, 190), font=ImageFont.load_default(size=26))
        d.text((70, 185), f"{fmt(balance)} so'm", fill="white", font=ImageFont.load_default(size=92))
        d.line([(70, 340), (W_ - 70, 340)], fill=(70, 85, 110), width=2)
        d.text((70, 368), str(name)[:28] or "Foydalanuvchi", fill=(210, 220, 235),
               font=ImageFont.load_default(size=36))
        link_short = link.replace("https://", "")
        d.text((70, 424), link_short[:46], fill=(120, 175, 255), font=ImageFont.load_default(size=28))
        d.text((70, 482), "Siz ham taklif qiling va pul ishlang!", fill=(255, 200, 60),
               font=ImageFont.load_default(size=24))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        logging.exception("Karta yaratishda xato")
        return None


async def view_invite(bot: Bot, chat_id: int, uid: int, name: str = ""):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={uid}"
    st = {r["status"]: r["c"] for r in await q(
        "SELECT status, COUNT(*) c FROM referrals WHERE inviter_id=? GROUP BY status", (uid,))}
    l1, l2 = await gs("ref_l1"), await gs("ref_l2")
    u = await get_user(uid)
    mult = await gs("premium_multiplier") if u["is_premium"] else 1
    kb = InlineKeyboardBuilder()
    kb.row(ib("📤 Do'stlarga ulashish",
              url=f"https://t.me/share/url?url={link}&text=Bu botda so'm ishlash mumkin!",
              style=OK))
    kb.row(ib("💰 Balansim", callback_data="m:bal", style=MAIN),
           ib("🏆 Reyting", callback_data="m:rate", style=MAIN))
    text = (f"👥 <b>DO'ST TAKLIF QILISH</b>\n{DIV}\n"
            f"💸 Har bir do'st uchun: <b>{fmt(l1 * mult)} so'm</b>\n"
            f"🎁 Do'stingizning do'sti uchun: <b>{fmt(l2)} so'm</b>\n"
            f"{THIN}\n"
            f"✅ Tasdiqlangan: <b>{st.get('confirmed', 0)}</b>\n"
            f"⏳ Kutilayotgan: <b>{st.get('pending', 0)}</b>\n"
            f"↩️ Bekor qilingan: <b>{st.get('revoked', 0) + st.get('limited', 0)}</b>\n"
            f"{DIV}\n"
            f"🔗 <b>Sizning havolangiz:</b>\n<code>{link}</code>")
    card = make_card(name or (u["full_name"] or ""), u["total_earned"], link)
    if card:
        await bot.send_photo(chat_id, BufferedInputFile(card, "card.png"),
                             caption=text, reply_markup=kb.as_markup())
    else:
        await bot.send_message(chat_id, text, reply_markup=kb.as_markup())


async def view_daily(bot: Bot, chat_id: int, uid: int):
    u = await get_user(uid)
    today = today_local().isoformat()
    yest = (today_local() - timedelta(days=1)).isoformat()
    if u["last_daily"] == today:
        kb = InlineKeyboardBuilder()
        kb.row(ib("👥 Do'st taklif qilish", callback_data="m:invite", style=OK),
               ib("💰 Balansim", callback_data="m:bal", style=MAIN))
        kb.row(ib("🏆 Reyting", callback_data="m:rate", style=MAIN))
        return await bot.send_message(chat_id,
                                      f"⏳ <b>BUGUNGI BONUS OLINGAN</b>\n{DIV}\n"
                                      f"🔥 Streak: <b>{u['streak']} kun</b>\n"
                                      f"🗓 Ertaga qayta tashrif buyuring — bonus yangilanadi!\n"
                                      f"{THIN}\n"
                                      f"<i>Bugun do'st taklif qilib daromad qilishingiz mumkin.</i>",
                                      reply_markup=kb.as_markup())
    streak = u["streak"] + 1 if u["last_daily"] == yest else 1
    lo, hi = await gs("daily_min"), await gs("daily_max")
    amt = random.randint(min(lo, hi), max(lo, hi))
    if u["is_premium"]:
        amt *= await gs("premium_multiplier")
    await ex("UPDATE users SET last_daily=?, streak=? WHERE id=?", (today, streak, uid))
    await add_balance(uid, amt, "daily")
    text = (f"🎁 <b>KUNLIK BONUS</b>\n{DIV}\n"
            f"✅ Hisobingizga qo'shildi: <b>+{fmt(amt)} so'm</b>\n"
            f"🔥 Streak: <b>{streak} kun</b>  {bar(min(100, streak / 7 * 100)).rsplit(' ', 1)[0]}\n"
            f"<i>7-kunda katta bonus sizni kutmoqda!</i>")
    if streak % 7 == 0:
        sb = await gs("streak_bonus")
        if sb:
            await add_balance(uid, sb, "streak")
            text += f"\n🏅 7 kunlik streak bonusi: <b>+{fmt(sb)} so'm</b>"
    kb = InlineKeyboardBuilder()
    kb.row(ib("👥 Do'st taklif qilish", callback_data="m:invite", style=OK),
           ib("💰 Balansim", callback_data="m:bal", style=MAIN))
    await bot.send_message(chat_id, text, reply_markup=kb.as_markup())


async def view_rating(bot: Bot, chat_id: int, uid: int):
    cutoff = now() - 7 * 86400
    rows = await q("SELECT inviter_id, COUNT(*) c FROM referrals WHERE status='confirmed' AND confirmed_at>? "
                   "GROUP BY inviter_id ORDER BY c DESC LIMIT 10", (cutoff,))
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = []
    for i, r in enumerate(rows):
        u = await get_user(r["inviter_id"])
        name = esc(u["full_name"])[:20] if u else f"ID {r['inviter_id']}"
        lines.append(f"{medals[i]} <b>{i + 1}.</b> {name} — <b>{r['c']}</b> ta")
    mine = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed' "
                     "AND confirmed_at>?", (uid, cutoff)))["c"]
    p1, p2, p3 = await gs("week_prize_1"), await gs("week_prize_2"), await gs("week_prize_3")
    kb = InlineKeyboardBuilder()
    kb.row(ib("👥 Taklif qilish", callback_data="m:invite", style=OK),
           ib("💰 Balansim", callback_data="m:bal", style=MAIN))
    await bot.send_message(chat_id,
                           f"🏆 <b>HAFTALIK REYTING</b>\n<i>oxirgi 7 kun</i>\n{DIV}\n" +
                           ("\n".join(lines) or "Hali hech kim yo'q — birinchi bo'ling! 🚀") +
                           f"\n{DIV}\n📌 Sizning natijangiz: <b>{mine} ta</b>\n{THIN}\n"
                           f"🎁 <b>Mukofotlar (yakshanba):</b>\n"
                           f"🥇 {money(p1)}\n🥈 {money(p2)}\n🥉 {money(p3)}",
                           reply_markup=kb.as_markup())


async def view_premium(bot: Bot, chat_id: int, uid: int):
    u = await get_user(uid)
    need = await gs("premium_refs")
    have = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed'",
                     (uid,)))["c"]
    mult = await gs("premium_multiplier")
    kb = InlineKeyboardBuilder()
    if u["is_premium"]:
        kb.row(ib("🎁 Bonus olish", callback_data="m:daily", style=OK),
               ib("💰 Balansim", callback_data="m:bal", style=MAIN))
        return await bot.send_message(
            chat_id, f"⭐ <b>PREMIUM FAOL</b>\n{DIV}\n"
                     f"Tabriklaymiz! Siz Premium a'zosisiz.\n"
                     f"💸 Barcha bonuslar <b>{mult}x</b> koeffitsiyent bilan hisoblanadi.",
            reply_markup=kb.as_markup())
    kb.row(ib("👥 Taklif qilish", callback_data="m:invite", style=OK))
    await bot.send_message(
        chat_id,
        f"⭐ <b>PREMIUM</b>\n{DIV}\n"
        f"💸 Taklif va kunlik bonuslar <b>{mult}x</b> ko'payadi.\n"
        f"{THIN}\n"
        f"🎯 Shart: <b>{need} ta</b> tasdiqlangan taklif\n"
        f"📊 Sizda: <b>{have} ta</b>\n"
        f"{bar(have / need * 100 if need else 100)}\n"
        f"{DIV}\n"
        f"<i>Premium pulga sotilmaydi — faqat faollik uchun beriladi.</i>",
        reply_markup=kb.as_markup())


# --- Yordam ---
@user.message(H.msg, F.text)
async def help_msg(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uname = f"@{esc(m.from_user.username)}" if m.from_user.username else "—"
    await notify_admins(bot, f"🆘 <b>Yordam so'rovi</b>\nID: <code>{m.from_user.id}</code> {uname}\n\n"
                             f"{esc(m.text)}\n\nJavob: /reply {m.from_user.id} matn",
                        roles=("owner", "moderator"))
    await m.answer("✅ Xabaringiz adminga yuborildi.", reply_markup=MENU)


# --- Pul yechish ---
async def withdraw_start(m: Message, state: FSMContext, uid: int | None = None):
    uid = uid or m.from_user.id
    u = await get_user(uid)
    mn = await gs("min_withdraw")
    if u["balance"] < mn:
        return await m.answer(f"❌ <b>Yechish uchun balans yetarli emas</b>\n{DIV}\n"
                              f"🎯 Minimum: {money(mn)}\n"
                              f"💰 Sizda: {money(u['balance'])}\n"
                              f"{bar(u['balance'] / mn * 100 if mn else 100)}\n\n"
                              f"<i>Do'st taklif qilib balansni to'ldiring!</i>")
    await state.set_state(W.amount)
    await m.answer(f"💳 <b>PUL YECHISH</b>\n{DIV}\n"
                   f"💰 Balans: {money(u['balance'])}\n"
                   f"🎯 Minimum: {money(mn)}\n"
                   f"🧾 Komissiya: <b>{await gs('withdraw_fee_percent')}%</b>\n"
                   f"{THIN}\n"
                   f"Yechmoqchi bo'lgan summani raqamda yuboring:\n"
                   f"<i>Masalan: 50000</i>\n\n"
                   f"<i>Bekor qilish: /cancel</i>")


@user.message(W.amount, F.text)
async def w_amount(m: Message, state: FSMContext):
    txt = m.text.replace(" ", "")
    if not txt.isdigit():
        return await m.answer("🔢 Faqat raqam kiriting (masalan: 50000).\n<i>/cancel — bekor qilish</i>")
    amt, u = int(txt), await get_user(m.from_user.id)
    if amt < await gs("min_withdraw"):
        return await m.answer(f"❌ Minimal summa: {money(await gs('min_withdraw'))}")
    if amt > u["balance"]:
        return await m.answer(f"❌ Balansingiz yetarli emas.\nSizda: {money(u['balance'])}")
    used = (await q1("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE user_id=? AND status!='rejected' "
                     "AND created_at>?", (u["id"], now() - 86400)))["s"]
    if used + amt > await gs("max_withdraw_daily"):
        return await m.answer(f"❌ Kunlik limit: {money(await gs('max_withdraw_daily'))}")
    await state.update_data(amount=amt)
    await state.set_state(W.method)
    fee = amt * await gs("withdraw_fee_percent") // 100
    kb = InlineKeyboardBuilder()
    kb.row(ib("💳 Karta (Uzcard/Humo)", callback_data="wm:card", style=MAIN),
           ib("📱 Telefon puli", callback_data="wm:phone", style=OK))
    await m.answer(f"🏦 <b>TO'LOV USULI</b>\n{DIV}\n"
                   f"💰 Summa: {money(amt)}\n"
                   f"🧾 Komissiya: {money(fee)} → olinadi: <b>{fmt(amt - fee)} so'm</b>\n"
                   f"{THIN}\nUsulni tanlang:",
                   reply_markup=kb.as_markup())


@user.callback_query(W.method, F.data.startswith("wm:"))
async def w_method(c: CallbackQuery, state: FSMContext):
    method = c.data.split(":")[1]
    await state.update_data(method=method)
    await state.set_state(W.details)
    if method == "card":
        await c.message.answer(f"💳 <b>Karta raqami</b>\n{DIV}\n"
                               f"Uzcard (8600...) yoki Humo (9860...) — 16 xonali raqamni yuboring:\n"
                               f"<i>Masalan: 8600123456789012</i>\n\n<i>Bekor qilish: /cancel</i>")
    else:
        await c.message.answer(f"📱 <b>Telefon raqami</b>\n{DIV}\n"
                               f"Raqamni yuboring:\n"
                               f"<i>Masalan: +998901234567</i>\n\n<i>Bekor qilish: /cancel</i>")
    await c.answer()


@user.message(W.details, F.text)
async def w_details(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    digits = re.sub(r"\D", "", m.text)
    if data.get("method") == "card":
        if len(digits) != 16 or not digits.startswith(("8600", "9860")):
            return await m.answer("❌ Noto'g'ri karta. Uzcard (8600) yoki Humo (9860) raqamini kiriting.")
        details = digits
    else:
        digits = digits[-9:]
        if len(digits) != 9:
            return await m.answer("❌ Noto'g'ri raqam. Masalan: +998901234567")
        details = "+998" + digits
    uid, amt = m.from_user.id, data["amount"]
    other = await q1("SELECT user_id FROM withdrawals WHERE details=? AND user_id!=? AND status!='rejected'",
                     (details, uid))
    if other:
        return await m.answer("❌ Bu rekvizit boshqa akkauntda ishlatilgan (bitta karta = bitta akkaunt).")
    cur = await db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amt, uid, amt))
    if cur.rowcount == 0:
        await db.commit()
        await state.clear()
        return await m.answer("❌ Balans yetarli emas.", reply_markup=MENU)
    fee = amt * await gs("withdraw_fee_percent") // 100
    cur = await db.execute("INSERT INTO withdrawals(user_id,amount,fee,method,details,created_at) "
                           "VALUES(?,?,?,?,?,?)", (uid, amt, fee, data["method"], details, now()))
    wid = cur.lastrowid
    await db.execute("INSERT INTO transactions(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                     (uid, -amt, "withdraw", now()))
    await db.commit()
    await state.clear()
    await m.answer(f"✅ <b>SO'ROV QABUL QILINDI</b>\n{DIV}\n"
                   f"🧾 So'rov raqami: <b>#{wid}</b>\n"
                   f"💰 Summa: {money(amt)}\n"
                   f"🧾 Komissiya: {money(fee)}\n"
                   f"✅ To'lanadi: <b>{fmt(amt - fee)} so'm</b>\n"
                   f"🏦 Rekvizit: <code>{details}</code>\n"
                   f"{DIV}\n"
                   f"<i>Admin 24 soat ichida ko'rib chiqadi va sizga xabar beradi.</i>",
                   reply_markup=MENU)
    await notify_admins(bot, wd_text(wid, uid, amt, fee, data["method"], details, m.from_user.full_name),
                        roles=("owner", "accountant"), kb=wd_kb(wid))


def wd_text(wid, uid, amt, fee, method, details, name):
    return (f"💳 <b>YECHISH SO'ROVI #{wid}</b>\n{DIV}\n"
            f"👤 {esc(name)}\n"
            f"🆔 <code>{uid}</code>\n"
            f"{THIN}\n"
            f"💰 Summa: {money(amt)}\n"
            f"🧾 Komissiya: {money(fee)}\n"
            f"✅ Beriladi: <b>{fmt(amt - fee)} so'm</b>\n"
            f"🏦 Usul: <b>{method}</b>\n"
            f"💳 Rekvizit: <code>{details}</code>")


def wd_kb(wid):
    kb = InlineKeyboardBuilder()
    kb.row(ib("✅ To'landi", callback_data=f"wd_ok:{wid}", style=OK),
           ib("❌ Rad etish", callback_data=f"wd_no:{wid}", style=BAD))
    return kb.as_markup()


# ----------------------------------------------------------------------------
# ADMIN PANEL
# ----------------------------------------------------------------------------
async def show(c: CallbackQuery, text, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb.as_markup() if kb else None)
    except TelegramBadRequest:
        await c.message.answer(text, reply_markup=kb.as_markup() if kb else None)


async def admin_home_text(role, name):
    total = (await q1("SELECT COUNT(*) c FROM users"))["c"]
    pend = (await q1("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'"))["c"]
    t = datetime.now(UZ_TZ).strftime("%d.%m.%Y · %H:%M")
    return (f"🔐 <b>ADMIN PANEL</b>\n{DIV}\n"
            f"👤 {esc(name.split()[0] if name else 'Admin')} · <b>{role}</b>\n"
            f"🕐 {t} <i>(Toshkent)</i>\n"
            f"{THIN}\n"
            f"👥 Foydalanuvchilar: <b>{fmt(total)}</b>\n"
            f"⏳ Kutilayotgan so'rovlar: <b>{pend}</b>\n"
            f"{DIV}\n"
            f"Kerakli bo'limni tanlang 👇")


def home_kb(role):
    kb = InlineKeyboardBuilder()
    allowed = [(k, t) for k, t in SECTIONS if role in PERMS[k]]
    for i in range(0, len(allowed), 2):
        kb.row(*[ib(t, callback_data=f"adm:{k}", style=SECTION_STYLE.get(k, MAIN))
                 for k, t in allowed[i:i + 2]])
    return kb


@admin.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    role = await role_of(m.from_user.id)
    if not role:
        return await m.answer("⛔ Sizda admin panelga ruxsat yo'q.")
    await m.answer(await admin_home_text(role, m.from_user.full_name), reply_markup=home_kb(role).as_markup())


@admin.message(Command("reply"))
async def cmd_reply(m: Message, command: CommandObject, bot: Bot):
    if await role_of(m.from_user.id) not in ("owner", "moderator"):
        return
    try:
        uid, text = (command.args or "").split(" ", 1)
        await bot.send_message(int(uid), f"📩 <b>Admin javobi:</b>\n{DIV}\n{esc(text)}")
        await m.answer("✅ Yuborildi")
    except Exception:
        await m.answer("Format: /reply ID matn")


async def dashboard_text():
    day0 = int(datetime.combine(today_local(), datetime.min.time()).timestamp())
    n = lambda s, a=(): q1(s, a)
    total = (await n("SELECT COUNT(*) c FROM users"))["c"]
    today = (await n("SELECT COUNT(*) c FROM users WHERE created_at>=?", (day0,)))["c"]
    ver = (await n("SELECT COUNT(*) c FROM users WHERE verified=1"))["c"]
    active = (await n("SELECT COUNT(*) c FROM users WHERE last_daily>=?",
                      ((today_local() - timedelta(days=3)).isoformat(),)))["c"]
    income = (await n("SELECT COALESCE(SUM(amount),0) s FROM income"))["s"]
    paid = (await n("SELECT COALESCE(SUM(amount-fee),0) s FROM withdrawals WHERE status='paid'"))["s"]
    pend = await n("SELECT COUNT(*) c, COALESCE(SUM(amount-fee),0) s FROM withdrawals WHERE status='pending'")
    debt = (await n("SELECT COALESCE(SUM(balance),0) s FROM users"))["s"]
    conv = (ver / total * 100) if total else 0
    return (f"📊 <b>DASHBOARD</b>\n{DIV}\n"
            f"👥 <b>Foydalanuvchilar</b>\n"
            f"├ Jami: <b>{fmt(total)}</b>\n"
            f"├ Bugun qo'shildi: <b>+{fmt(today)}</b>\n"
            f"├ Obunani tasdiqlagan: <b>{fmt(ver)}</b>\n"
            f"├ Faol (3 kun): <b>{fmt(active)}</b>\n"
            f"└ Konversiya: <b>{bar(conv, 8)}</b>\n"
            f"{THIN}\n"
            f"💵 <b>Moliya</b>\n"
            f"├ Kirim (homiylar): {money(income)}\n"
            f"├ To'langan: {money(paid)}\n"
            f"├ Foyda: <b>{fmt(income - paid)} so'm</b>\n"
            f"├ Foydalanuvchilar balansi: {money(debt)}\n"
            f"└ ⏳ Kutilayotgan: <b>{pend['c']} ta</b> ({fmt(pend['s'])} so'm)")


async def profit_calc():
    s = (await q1("SELECT COALESCE(SUM(reward),0) s FROM sponsors WHERE active=1"))["s"]
    cost = await gs("start_bonus") + await gs("ref_l1") + await gs("ref_l2")
    p = s - cost
    warn = "\n⚠️ <b>DIQQAT: zarar!</b> Bonuslarni kamaytiring yoki homiy narxini oshiring." if p < 0 else ""
    return (f"🧮 <b>Foyda kalkulyatori</b>\n<i>1 ta taklif qilingan foydalanuvchi</i>\n{DIV}\n"
            f"📥 Homiylardan: {money(s)}\n"
            f"📤 Bonuslar (start + 1 + 2): {money(cost)}\n"
            f"📈 <b>Foyda: {fmt(p)} so'm</b>{warn}")


async def sponsors_view(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    rows = await q("SELECT * FROM sponsors")
    lines = [f"📢 <b>HOMIY KANALLAR</b>\n{DIV}"]
    if not rows:
        lines.append("Hali kanal qo'shilmagan.\nQuyidagi tugma orqali qo'shing 👇")
    for s in rows:
        st = "🟢" if s["active"] else "🔴"
        lim = s["max_members"] or "∞"
        lines.append(f"{st} <b>#{s['id']}</b> {esc(s['title'])}\n"
                     f"   ├ 💵 {fmt(s['reward'])} so'm/kishi\n"
                     f"   └ 👥 {s['joined']}/{lim}")
        kb.row(ib(f"🔁 #{s['id']} yoq/o'chirish", callback_data=f"sp_t:{s['id']}", style=MAIN),
               ib(f"🗑 #{s['id']} o'chirish", callback_data=f"sp_d:{s['id']}", style=BAD))
    kb.row(ib("➕ Kanal qo'shish", callback_data="sp_add", style=OK),
           ib("🔄 Yangilash", callback_data="adm:sp", style=MAIN))
    kb.row(back_btn("home"))
    await show(c, "\n".join(lines), kb)


@admin.callback_query(F.data.startswith("adm:"))
async def adm_section(c: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    sec = c.data.split(":")[1]
    role = await role_of(c.from_user.id)
    if not role or (sec != "home" and role not in PERMS.get(sec, set())):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await c.answer()
    if sec == "home":
        return await show(c, await admin_home_text(role, c.from_user.full_name), home_kb(role))
    if sec == "dash":
        kb = InlineKeyboardBuilder()
        kb.row(ib("🔄 Yangilash", callback_data="adm:dash", style=MAIN),
               ib("💳 So'rovlar", callback_data="adm:wd", style=BAD))
        kb.row(back_btn("home"))
        return await show(c, await dashboard_text(), kb)
    if sec in ("prices", "sec", "adm"):
        groups = {"prices": ("price",), "sec": ("limit",), "adm": ("sys", "text")}[sec]
        kb = InlineKeyboardBuilder()
        for key, (d, title, g) in SETTINGS.items():
            if g in groups:
                val = await gs(key)
                if g == "text":
                    kb.row(ib(f"✏️ {title}", callback_data=f"set:{key}:{sec}", style=MAIN))
                elif key == "maintenance":
                    st = "🟢 Yoqilgan" if val else "🔴 O'chiq"
                    kb.row(ib(f"🛠 Texnik rejim: {st}", callback_data=f"set:{key}:{sec}",
                              style=BAD if val else OK))
                else:
                    kb.row(ib(f"{title.split(' (')[0]}: {val}", callback_data=f"set:{key}:{sec}", style=OK))
        text = {"prices": f"💰 <b>NARXLAR VA BONUSLAR</b>\n{DIV}\n" + await profit_calc() +
                          f"\n{THIN}\n📝 O'zgartirish uchun tugmani bosing.\n"
                          f"<i>0 qiymati = o'chirilgan.</i>",
                "sec": f"🛡 <b>XAVFSIZLIK</b>\n{DIV}\n" + await security_text(),
                "adm": f"⚙️ <b>SOZLAMALAR</b>\n{DIV}\n"
                       f"🛠 Texnik rejim, matnlar va adminlar boshqaruvi.\n"
                       f"O'zgartirish uchun tugmani bosing."}[sec]
        if sec == "adm":
            kb.row(ib("👮 Adminlar boshqaruvi", callback_data="admlist", style=MAIN))
        kb.row(ib("🔄 Yangilash", callback_data=f"adm:{sec}", style=MAIN))
        kb.row(back_btn("home"))
        return await show(c, text, kb)
    if sec == "wd":
        pend = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount-fee),0) s FROM withdrawals WHERE status='pending'")
        today_wd = (await q1("SELECT COUNT(*) c FROM withdrawals WHERE created_at>?",
                             (now() - 86400,)))["c"]
        kb = InlineKeyboardBuilder()
        kb.row(ib(f"⏳ Kutilayotganlar ({pend['c']})", callback_data="wdl:pending", style=BAD))
        kb.row(ib("✅ To'langanlar", callback_data="wdl:paid", style=OK),
               ib("❌ Rad etilganlar", callback_data="wdl:rejected", style=MAIN))
        kb.row(ib("🔄 Yangilash", callback_data="adm:wd", style=MAIN))
        kb.row(back_btn("home"))
        return await show(c,
                          f"💳 <b>YECHISH SO'ROVLARI</b>\n{DIV}\n"
                          f"⏳ Kutilayotgan: <b>{pend['c']} ta</b> ({fmt(pend['s'])} so'm)\n"
                          f"🕐 Oxirgi 24 soatda: <b>{today_wd} ta</b>\n"
                          f"{THIN}\n"
                          f"<i>Min/max/komissiya: «Narxlar» bo'limida.</i>",
                          kb)
    if sec == "sp":
        return await sponsors_view(c)
    if sec == "users":
        await state.set_state(A.user_search)
        return await show(c,
                          f"👥 <b>FOYDALANUVCHILAR</b>\n{DIV}\n"
                          f"🔍 ID, @username yoki telefon raqamni yuboring.\n"
                          f"{THIN}\n"
                          f"<i>Misol: 8567272412 yoki @username</i>\n"
                          f"<i>Bekor qilish: /cancel</i>", back_kb())
    if sec == "bc":
        kb = InlineKeyboardBuilder()
        total = (await q1("SELECT COUNT(*) c FROM users WHERE is_banned=0"))["c"]
        act = (await q1("SELECT COUNT(*) c FROM users WHERE is_banned=0 AND last_daily>=?",
                        ((today_local() - timedelta(days=3)).isoformat(),)))["c"]
        prem = (await q1("SELECT COUNT(*) c FROM users WHERE is_banned=0 AND is_premium=1"))["c"]
        kb.row(ib(f"🌍 Hammaga ({total})", callback_data="bc:all", style=MAIN))
        kb.row(ib(f"✅ Faollarga ({act})", callback_data="bc:active", style=OK),
               ib(f"⭐ Premiumga ({prem})", callback_data="bc:premium", style=MAIN))
        kb.row(back_btn("home"))
        return await show(c, f"📣 <b>XABAR YUBORISH</b>\n{DIV}\nKimga yuboramiz?", kb)
    if sec == "rep":
        d7 = now() - 7 * 86400
        new = (await q1("SELECT COUNT(*) c FROM users WHERE created_at>?", (d7,)))["c"]
        inc = (await q1("SELECT COALESCE(SUM(amount),0) s FROM income WHERE created_at>?", (d7,)))["s"]
        paid = (await q1("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='paid' "
                         "AND created_at>?", (d7,)))["s"]
        top = await q("SELECT inviter_id, COUNT(*) c FROM referrals WHERE status='confirmed' GROUP BY inviter_id "
                      "ORDER BY c DESC LIMIT 5")
        sp = await q("SELECT s.title, COALESCE(SUM(i.amount),0) s FROM sponsors s LEFT JOIN income i "
                     "ON i.sponsor_id=s.id GROUP BY s.id")
        text = (f"📈 <b>HISOBOT — 7 KUN</b>\n{DIV}\n"
                f"👥 Yangi: <b>{fmt(new)}</b>\n"
                f"💵 Kirim: {money(inc)}\n"
                f"💸 To'langan: {money(paid)}\n"
                f"📊 Foyda: <b>{fmt(inc - paid)} so'm</b>\n"
                f"{THIN}\n<b>📢 Homiylar bo'yicha:</b>\n" +
                ("\n".join(f"├ {esc(r['title'])}: {fmt(r['s'])} so'm" for r in sp) or "—") +
                f"\n{THIN}\n<b>🏆 Eng faol referrerlar:</b>\n" +
                ("\n".join(f"├ <code>{r['inviter_id']}</code>: {r['c']} ta" for r in top) or "—"))
        kb = InlineKeyboardBuilder()
        kb.row(ib("📥 Foydalanuvchilar CSV", callback_data="csv:users", style=MAIN),
               ib("📥 To'lovlar CSV", callback_data="csv:wd", style=MAIN))
        kb.row(ib("🔄 Yangilash", callback_data="adm:rep", style=OK))
        kb.row(back_btn("home"))
        return await show(c, text, kb)


async def security_text():
    cards = await q("SELECT details, COUNT(DISTINCT user_id) n FROM withdrawals GROUP BY details HAVING n>1")
    rev = await q("SELECT inviter_id, COUNT(*) n FROM referrals WHERE status='revoked' GROUP BY inviter_id "
                  "HAVING n>=3 ORDER BY n DESC LIMIT 10")
    lim = (await q1("SELECT COUNT(*) c FROM referrals WHERE status='limited'"))["c"]
    t = (f"📉 Limitga tushgan takliflar: <b>{lim}</b>\n"
         f"{THIN}\n<b>💳 Bir rekvizitda ko'p akkaunt:</b>\n")
    t += "\n".join(f"├ <code>{esc(r['details'])}</code> — {r['n']} ta" for r in cards) or "├ Xavfli holat yo'q ✅"
    t += f"\n{THIN}\n<b>↩️ Obunadan chiqqan ko'p taklif (≥3):</b>\n"
    t += "\n".join(f"├ <code>{r['inviter_id']}</code> — {r['n']} ta" for r in rev) or "├ Xavfli holat yo'q ✅"
    return t



# --- Sozlamani tahrirlash ---
@admin.callback_query(F.data.startswith("set:"))
async def set_start(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    if len(parts) != 3:
        return await c.answer("Noto'g'ri so'rov", show_alert=True)
    _, key, back = parts
    if key not in SETTINGS or SETTINGS[key][2] == "hidden":
        return await c.answer("Noto'g'ri sozlama", show_alert=True)
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(A.val)
    await state.update_data(key=key, back=back)
    cur = await gs(key)
    hint = ("Matn yoki HTML yuboring" if SETTINGS[key][2] == "text"
            else "Faqat butun son yuboring (0 = o'chirilgan)")
    await c.message.answer(f"⚙️ <b>SOZLAMANI O'ZGARTIRISH</b>\n{DIV}\n"
                           f"📝 {esc(SETTINGS[key][1])}\n"
                           f"📌 Hozirgi qiymat: <code>{esc(cur)}</code>\n"
                           f"{THIN}\n"
                           f"{hint}:\n\n"
                           f"<i>Bekor qilish: /cancel</i>")
    await c.answer()


@admin.message(A.val, F.text)
async def set_value(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    default = SETTINGS[key][0]
    val = m.html_text if isinstance(default, str) else m.text.replace(" ", "")
    if isinstance(default, int):
        if not val.isdigit():
            return await m.answer("🔢 Faqat musbat butun son kiriting.\n<i>Bekor qilish: /cancel</i>")
        val = int(val)
    old = await gs(key)
    await set_setting(key, val)
    await state.clear()
    await notify_admins(m.bot, f"🧾 {esc(m.from_user.full_name)} o'zgartirdi: {SETTINGS[key][1]}: "
                               f"{esc(str(old)[:40])} → {esc(str(val)[:40])}", roles=("owner",))
    await m.answer(f"✅ <b>SAQLANDI</b>\n{DIV}\n"
                   f"📝 {esc(SETTINGS[key][1])}\n"
                   f"🗂 Avval: <code>{esc(str(old)[:60])}</code>\n"
                   f"🆕 Endi: <code>{esc(str(val)[:60])}</code>\n"
                   f"{THIN}\n"
                   f"<i>Yangi qiymat darrov amal qiladi.</i>",
                   reply_markup=back_kb(data["back"]).as_markup())


# --- Yechish so'rovlari ---
@admin.callback_query(F.data.startswith("wdl:"))
async def wd_list(c: CallbackQuery):
    if await role_of(c.from_user.id) not in PERMS["wd"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    st = c.data.split(":")[1]
    if st not in ("pending", "paid", "rejected"):
        return await c.answer("Noto'g'ri filtr", show_alert=True)
    rows = await q("SELECT w.*, u.full_name FROM withdrawals w JOIN users u ON u.id=w.user_id "
                   "WHERE w.status=? ORDER BY w.id DESC LIMIT 10", (st,))
    await c.answer()
    if not rows:
        labels = {"pending": "⏳ Kutilayotgan", "paid": "✅ To'langan", "rejected": "❌ Rad etilgan"}
        kb = InlineKeyboardBuilder()
        kb.row(back_btn("wd"))
        return await c.message.answer(f"{labels[st]} so'rovlar yo'q.\n{DIV}\n"
                                      f"<i>Hozircha bu bo'limda hech narsa yo'q.</i>",
                                      reply_markup=kb.as_markup())
    for w in rows:
        txt = wd_text(w["id"], w["user_id"], w["amount"], w["fee"], w["method"], w["details"], w["full_name"])
        if st == "rejected":
            txt += f"\n{THIN}\n❌ Sabab: {esc(w['reason'])}"
        await c.message.answer(txt, reply_markup=wd_kb(w["id"]) if st == "pending" else None)


@admin.callback_query(F.data.startswith("wd_ok:"))
async def wd_ok(c: CallbackQuery, bot: Bot):
    if await role_of(c.from_user.id) not in PERMS["wd"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    wid = int(c.data.split(":")[1])
    cur = await ex("UPDATE withdrawals SET status='paid', admin_id=? WHERE id=? AND status='pending'",
                   (c.from_user.id, wid))
    if cur.rowcount == 0:
        return await c.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
    w = await q1("SELECT * FROM withdrawals WHERE id=?", (wid,))
    await ex("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE id=?", (w["amount"], w["user_id"]))
    await safe_send(bot, w["user_id"],
                    f"✅ <b>TO'LOV AMALGA OSHIRILDI</b>\n{DIV}\n"
                    f"🧾 So'rov: <b>#{wid}</b>\n"
                    f"💰 Hisobingizga: <b>{fmt(w['amount'] - w['fee'])} so'm</b>\n"
                    f"{THIN}\n<i>Yana do'st taklif qilib daromadni oshiring!</i>")
    try:
        await c.message.edit_text(c.message.html_text +
                                 f"\n{DIV}\n✅ <b>TO'LANDI</b> · {esc(c.from_user.full_name)}",
                                 reply_markup=None)
    except TelegramBadRequest:
        pass
    await c.answer("✅ To'landi deb belgilandi")


@admin.callback_query(F.data.startswith("wd_no:"))
async def wd_no(c: CallbackQuery, state: FSMContext):
    if await role_of(c.from_user.id) not in PERMS["wd"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(A.reject)
    await state.update_data(wid=int(c.data.split(":")[1]))
    await c.message.answer(f"❌ <b>RAD ETISH</b>\n{DIV}\n"
                           f"Sababni yozing — foydalanuvchiga ko'rsatiladi\n"
                           f"va summa balansiga qaytariladi.\n\n"
                           f"<i>Bekor qilish: /cancel</i>")
    await c.answer()


@admin.message(A.reject, F.text)
async def wd_reject(m: Message, state: FSMContext, bot: Bot):
    wid = (await state.get_data())["wid"]
    await state.clear()
    cur = await ex("UPDATE withdrawals SET status='rejected', admin_id=?, reason=? WHERE id=? AND status='pending'",
                   (m.from_user.id, m.text, wid))
    if cur.rowcount == 0:
        return await m.answer("Allaqachon ko'rib chiqilgan.")
    w = await q1("SELECT * FROM withdrawals WHERE id=?", (wid,))
    await add_balance(w["user_id"], w["amount"], "refund", earn=False)
    await safe_send(bot, w["user_id"],
                    f"❌ <b>SO'ROV RAD ETILDI</b>\n{DIV}\n"
                    f"🧾 So'rov: <b>#{wid}</b>\n"
                    f"💰 Summa: <b>{fmt(w['amount'])} so'm</b> — balansga qaytarildi\n"
                    f"{THIN}\n📄 Sabab: {esc(m.text)}\n\n"
                    f"<i>Savollar bo'lsa /start → 🆘 Yordam orqali yozing.</i>")
    await m.answer(f"❌ <b>#{wid} rad etildi</b>\n{DIV}\n"
                   f"💰 {fmt(w['amount'])} so'm foydalanuvchi balansiga qaytarildi.",
                   reply_markup=back_kb("wd").as_markup())


# --- Homiylar (sponsors) ---
@admin.callback_query(F.data.startswith("sp_t:"))
async def sp_toggle(c: CallbackQuery):
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await ex("UPDATE sponsors SET active=1-active WHERE id=?", (int(c.data.split(":")[1]),))
    await c.answer("Holat o'zgartirildi")
    await sponsors_view(c)


@admin.callback_query(F.data.startswith("sp_d:"))
async def sp_del(c: CallbackQuery):
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await ex("DELETE FROM sponsors WHERE id=?", (int(c.data.split(":")[1]),))
    await c.answer("O'chirildi")
    await sponsors_view(c)


@admin.callback_query(F.data == "sp_add")
async def sp_add(c: CallbackQuery, state: FSMContext):
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(A.sponsor)
    await c.message.answer(f"➕ <b>KANAL QO'SHISH</b>\n{DIV}\n"
                           f"Format:\n<code>@kanal_username 600 1000</code>\n"
                           f"{THIN}\n"
                           f"1️⃣ Kanal (username yoki -100... ID)\n"
                           f"2️⃣ 1 obunachi uchun sizga tushadigan so'm\n"
                           f"3️⃣ Obunachi limiti (0 = cheksiz)\n"
                           f"{DIV}\n"
                           f"⚠️ <b>Bot kanalda admin bo'lishi shart!</b>\n"
                           f"<i>Bekor qilish: /cancel</i>")
    await c.answer()


@admin.message(A.sponsor, F.text)
async def sp_save(m: Message, state: FSMContext, bot: Bot):
    try:
        ch, reward, limit = m.text.split()
        int(reward), int(limit)  # tekshiruv
        chat = await bot.get_chat(int(ch) if ch.lstrip("-").isdigit() else ch)
        link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else
                                    await bot.export_chat_invite_link(chat.id))
        await ex("INSERT INTO sponsors(channel_id,link,title,reward,max_members) VALUES(?,?,?,?,?)",
                 (chat.id, link, chat.title, int(reward), int(limit)))
    except ValueError:
        return await m.answer("❌ Narx va limit faqat son bo'lishi kerak.\n"
                              "Misol: <code>@kanal 600 1000</code>")
    except Exception as e:
        return await m.answer(f"❌ <b>Xato:</b> {esc(e)}\n{DIV}\n"
                              f"Formatni va bot kanalda admin ekanini tekshiring.")
    await state.clear()
    await m.answer(f"✅ <b>KANAL QO'SHILDI</b>\n{DIV}\n"
                   f"📢 {esc(chat.title)}\n"
                   f"🆔 <code>{chat.id}</code>\n"
                   f"💵 {fmt(int(reward))} so'm / obunachi\n"
                   f"👥 Limit: {int(limit) or '∞'}\n"
                   f"{THIN}\n<i>Endi foydalanuvchilar obuna bo'lishi shart.</i>",
                   reply_markup=back_kb("sp").as_markup())


# --- Foydalanuvchilar ---
async def user_card(uid):
    u = await get_user(uid)
    if not u:
        return None, None
    refs = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed'", (uid,)))["c"]
    text = (f"👤 <b>FOYDALANUVCHI KARTOCHKASI</b>\n{DIV}\n"
            f"🧑 <b>{esc(u['full_name'])}</b>"
            + (f" · @{esc(u['username'])}" if u["username"] else "") + "\n"
            f"🆔 <code>{uid}</code>\n"
            f"📱 {esc(u['phone']) if u['phone'] else '—'}\n"
            f"{THIN}\n"
            f"💰 Balans: <b>{fmt(u['balance'])} so'm</b>\n"
            f"📈 Topilgan: {money(u['total_earned'])}\n"
            f"📉 Yechilgan: {money(u['total_withdrawn'])}\n"
            f"{THIN}\n"
            f"👥 Tasdiqlangan takliflar: <b>{refs} ta</b>\n"
            f"🔗 Taklif qilgan: <code>{u['referrer_id'] or '—'}</code>\n"
            f"⭐ Premium: <b>{'ha' if u['is_premium'] else 'yo‘q'}</b>\n"
            f"🚫 Bloklangan: <b>{'ha' if u['is_banned'] else 'yo‘q'}</b>\n"
            f"🔥 Streak: <b>{u['streak']} kun</b>")
    kb = InlineKeyboardBuilder()
    kb.row(ib("✅ Blokdan chiqarish" if u["is_banned"] else "🚫 Bloklash",
              callback_data=f"u:ban:{uid}", style=OK if u["is_banned"] else BAD),
           ib("⭐ Premium", callback_data=f"u:prem:{uid}", style=MAIN))
    kb.row(ib("💰 Balansni o'zgartirish", callback_data=f"u:bal:{uid}", style=OK),
           ib("🌳 Referral daraxti", callback_data=f"u:tree:{uid}", style=MAIN))
    kb.row(ib("🔄 Yangilash", callback_data=f"u:ref:{uid}", style=MAIN))
    kb.row(back_btn("home"))
    return text, kb


@admin.message(A.user_search, F.text)
async def user_search(m: Message, state: FSMContext):
    s = m.text.strip().lstrip("@")
    u = await get_user(int(s)) if s.isdigit() else await q1("SELECT * FROM users WHERE username=?", (s,))
    if not u and s.isdigit():
        u = await q1("SELECT * FROM users WHERE phone LIKE ?", (f"%{s}",))
    if not u:
        return await m.answer("Topilmadi. Qayta urinib ko'ring.")
    await state.clear()
    text, kb = await user_card(u["id"])
    await m.answer(text, reply_markup=kb.as_markup())


@admin.callback_query(F.data.startswith("u:"))
async def user_actions(c: CallbackQuery, state: FSMContext):
    if await role_of(c.from_user.id) not in PERMS["users"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    try:
        _, act, uid_s = c.data.split(":")
        uid = int(uid_s)
    except ValueError:
        return await c.answer("Noto'g'ri so'rov", show_alert=True)
    if act == "ban":
        await ex("UPDATE users SET is_banned=1-is_banned WHERE id=?", (uid,))
    elif act == "prem":
        await ex("UPDATE users SET is_premium=1-is_premium WHERE id=?", (uid,))
    elif act == "bal":
        u = await get_user(uid)
        if not u:
            return await c.answer("Foydalanuvchi topilmadi", show_alert=True)
        await state.set_state(A.user_bal)
        await state.update_data(uid=uid)
        await c.message.answer(f"💰 <b>BALANSNI O'ZGARTIRISH</b>\n{DIV}\n"
                               f"👤 {esc(u['full_name'])[:25]} · <code>{uid}</code>\n"
                               f"💳 Hozirgi balans: <b>{fmt(u['balance'])} so'm</b>\n"
                               f"{THIN}\n"
                               f"Format: <code>+500 sabab</code> yoki <code>-300 sabab</code>\n"
                               f"<i>Sabab majburiy. Bekor qilish: /cancel</i>")
        return await c.answer()
    elif act == "tree":
        rows = await q("SELECT r.invited_id, r.status, r.created_at, u.full_name FROM referrals r "
                       "JOIN users u ON u.id=r.invited_id WHERE r.inviter_id=? "
                       "ORDER BY r.created_at DESC LIMIT 20", (uid,))
        icons = {"confirmed": "✅", "pending": "⏳", "revoked": "↩️", "limited": "🚫"}
        body = "\n".join(f"{icons.get(r['status'], '•')} {esc(r['full_name'])[:22]} "
                         f"(<code>{r['invited_id']}</code>)" for r in rows)
        await c.message.answer(f"🌳 <b>REFERRAL DARAXTI</b>\n{DIV}\n"
                               f"👤 <code>{uid}</code>\n{THIN}\n"
                               f"{body or 'Takliflar yo‘q'}",
                               reply_markup=back_kb("home").as_markup())
        return await c.answer()
    elif act == "ref":
        pass
    else:
        return await c.answer("Noma'lum amal", show_alert=True)
    text, kb = await user_card(uid)
    if text:
        await show(c, text, kb)
    await c.answer("Bajarildi")


@admin.message(A.user_bal, F.text)
async def user_bal(m: Message, state: FSMContext, bot: Bot):
    mt = re.match(r"^([+-]?\d+)\s+(.+)$", m.text.strip())
    if not mt:
        return await m.answer("🔢 Format: <code>+500 sabab</code>\n<i>Bekor qilish: /cancel</i>")
    uid = (await state.get_data())["uid"]
    amt = int(mt.group(1))
    u = await get_user(uid)
    if not u:
        await state.clear()
        return await m.answer("Foydalanuvchi topilmadi.")
    await add_balance(uid, amt, "admin", earn=False)
    await state.clear()
    await notify_admins(m.bot, f"🧾 {esc(m.from_user.full_name)}: <code>{uid}</code> balansi "
                               f"{amt:+d} ({esc(mt.group(2))})", roles=("owner",))
    await safe_send(m.bot, uid,
                    f"ℹ️ <b>BALANS O'ZGARTIRILDI</b>\n{DIV}\n"
                    f"{'➕' if amt > 0 else '➖'} <b>{fmt(abs(amt))} so'm</b>\n"
                    f"{THIN}\n📄 Sabab: {esc(mt.group(2))}\n"
                    f"💳 Yangi balans: <b>{fmt(u['balance'] + amt)} so'm</b>")
    await m.answer(f"✅ <b>BAJARILDI</b>\n{DIV}\n"
                   f"👤 <code>{uid}</code>\n"
                   f"{'➕' if amt > 0 else '➖'} {fmt(abs(amt))} so'm\n"
                   f"💳 Yangi balans: <b>{fmt(u['balance'] + amt)} so'm</b>",
                   reply_markup=back_kb("home").as_markup())


# --- Xabar yuborish ---
@admin.callback_query(F.data.startswith("bc:"))
async def bc_start(c: CallbackQuery, state: FSMContext):
    if await role_of(c.from_user.id) not in PERMS["bc"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    aud = c.data.split(":")[1]
    if aud not in ("all", "active", "premium"):
        return await c.answer("Noto'g'ri auditoriya", show_alert=True)
    await state.set_state(A.bc_msg)
    await state.update_data(aud=aud)
    aud_name = {"all": "🌍 Barcha foydalanuvchilar", "active": "✅ Faollar (3 kun)",
                "premium": "⭐ Premium a'zolar"}[aud]
    await c.message.answer(f"📣 <b>XABAR YUBORISH</b>\n{DIV}\n"
                           f"🎯 Auditoriya: {aud_name}\n"
                           f"{THIN}\n"
                           f"Yuboriladigan xabarni jo'nating.\n"
                           f"<i>Matn, rasm, video, tugma — hammasi bo'ladi.</i>\n\n"
                           f"<i>Bekor qilish: /cancel</i>")
    await c.answer()


@admin.message(A.bc_msg)
async def bc_msg(m: Message, state: FSMContext):
    aud = (await state.get_data())["aud"]
    where = {"all": "1=1", "active": "last_daily>='" + (today_local() - timedelta(days=3)).isoformat() + "'",
             "premium": "is_premium=1"}[aud]
    n = (await q1(f"SELECT COUNT(*) c FROM users WHERE is_banned=0 AND {where}"))["c"]
    await state.update_data(chat=m.chat.id, mid=m.message_id, where=where)
    kb = InlineKeyboardBuilder()
    kb.row(ib(f"✅ Yuborish ({n} ta)", callback_data="bcgo", style=OK),
           ib("❌ Bekor", callback_data="adm:home", style=BAD))
    await m.answer(f"📣 <b>TASDIQLASH</b>\n{DIV}\n"
                   f"👥 Qabul qiluvchilar: <b>{fmt(n)} ta</b>\n"
                   f"{THIN}\n"
                   f"Yuborishni tasdiqlaysizmi?\n"
                   f"<i>Bekor qilsangiz hech kimga yuborilmaydi.</i>",
                   reply_markup=kb.as_markup())


@admin.callback_query(F.data == "bcgo")
async def bc_go(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if not d or "where" not in d:
        return await c.answer("Eskirgan so'rov", show_alert=True)
    if await role_of(c.from_user.id) not in PERMS["bc"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    try:
        await c.message.edit_text(f"📣 <b>Yuborish boshlandi...</b>\n{DIV}\n"
                                  f"<i>Har bir foydalanuvchiga yuborilmoqda, iltimos kuting.</i>")
    except TelegramBadRequest:
        pass

    async def run():
        ids = [r["id"] for r in await q(f"SELECT id FROM users WHERE is_banned=0 AND {d['where']}")]
        ok = 0
        for i in ids:
            try:
                await bot.copy_message(i, d["chat"], d["mid"])
                ok += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await safe_send(bot, c.from_user.id,
                        f"📣 <b>XABAR YUBORILDI</b>\n{DIV}\n"
                        f"✅ Yetkazildi: <b>{fmt(ok)}</b>\n"
                        f"👥 Jami: <b>{fmt(len(ids))}</b>\n"
                        f"{THIN}\n"
                        f"<i>Bloklagan foydalanuvchilarga yetib bormadi.</i>")
    asyncio.create_task(run())


# --- CSV ---
@admin.callback_query(F.data.startswith("csv:"))
async def csv_export(c: CallbackQuery):
    if await role_of(c.from_user.id) not in PERMS["rep"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    kind = c.data.split(":")[1]
    if kind == "users":
        rows = await q("SELECT id,username,full_name,phone,balance,total_earned,total_withdrawn,is_premium,is_banned "
                       "FROM users")
    elif kind == "wd":
        rows = await q("SELECT * FROM withdrawals")
    else:
        return await c.answer("Noto'g'ri tur", show_alert=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    if rows:
        w.writerow(rows[0].keys())
        w.writerows([tuple(r) for r in rows])
    await c.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), f"{kind}.csv"))
    await c.answer("✅ Yuklab olindi")


# --- Adminlar boshqaruvi ---
@admin.callback_query(F.data == "admlist")
async def adm_list(c: CallbackQuery):
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    kb = InlineKeyboardBuilder()
    admins = await q("SELECT * FROM admins ORDER BY role, user_id")
    for a in admins:
        u = await get_user(a["user_id"])
        nm = esc(u["full_name"])[:18] if u else "—"
        kb.row(ib(f"🗑 {nm} · {a['role']}", callback_data=f"admdel:{a['user_id']}", style=BAD))
    kb.row(ib("➕ Admin qo'shish", callback_data="admadd", style=OK))
    kb.row(back_btn("adm"))
    body = "\n".join(f"├ <code>{a['user_id']}</code> · <b>{a['role']}</b>" for a in admins) or "├ Qo'shimcha admin yo'q"
    await show(c, f"👮 <b>ADMINLAR BOSHQARUVI</b>\n{DIV}\n"
                  f"👑 Ega (owner): <code>{OWNER_ID}</code>\n"
                  f"{THIN}\n"
                  f"<b>Qo'shilgan adminlar:</b>\n{body}\n"
                  f"{DIV}\n"
                  f"🔑 Rollar:\n"
                  f"├ <b>owner</b> — to'liq huquq\n"
                  f"├ <b>moderator</b> — foydalanuvchi/xabar\n"
                  f"└ <b>accountant</b> — to'lov/hisobot\n"
                  f"{THIN}\n"
                  f"🗑 Tugmani bosib adminni o'chirasiz.", kb)
    await c.answer()


@admin.callback_query(F.data.startswith("admdel:"))
async def adm_del(c: CallbackQuery):
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    uid = int(c.data.split(":")[1])
    if uid == OWNER_ID:
        return await c.answer("Ega o'chirilmaydi", show_alert=True)
    await ex("DELETE FROM admins WHERE user_id=?", (uid,))
    await c.answer("O'chirildi")
    await adm_list(c)


@admin.callback_query(F.data == "admadd")
async def adm_add(c: CallbackQuery, state: FSMContext):
    if await role_of(c.from_user.id) != "owner":
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(A.admin_add)
    await c.message.answer(f"➕ <b>ADMIN QO'SHISH</b>\n{DIV}\n"
                           f"Format: <code>123456789 moderator</code>\n"
                           f"{THIN}\n"
                           f"🔑 Rollar: <code>owner</code>, <code>moderator</code>, "
                           f"<code>accountant</code>\n\n"
                           f"<i>Bekor qilish: /cancel</i>")
    await c.answer()


@admin.message(A.admin_add, F.text)
async def adm_save(m: Message, state: FSMContext):
    try:
        uid, role = m.text.split()
        assert role in ("owner", "moderator", "accountant")
        int(uid)
        await ex("INSERT OR REPLACE INTO admins(user_id,role) VALUES(?,?)", (int(uid), role))
    except Exception:
        return await m.answer("❌ Format noto'g'ri.\nMisol: <code>123456789 moderator</code>\n"
                              "<i>Bekor qilish: /cancel</i>")
    await state.clear()
    await m.answer(f"✅ <b>ADMIN QO'SHILDI</b>\n{DIV}\n"
                   f"🆔 <code>{uid}</code>\n"
                   f"🔑 Rol: <b>{role}</b>\n"
                   f"{THIN}\n<i>U /admin buyrug'i orqali kirishi mumkin.</i>",
                   reply_markup=back_kb("adm").as_markup())


# ----------------------------------------------------------------------------
# FON VAZIFALARI
# ----------------------------------------------------------------------------
async def job_recheck(bot):
    """Obunadan chiqib ketganlar uchun bonusni qaytarib olish."""
    limit = now() - await gs("check_days") * 86400
    for r in await q("SELECT * FROM referrals WHERE status='confirmed' AND checked=0 AND confirmed_at<=? "
                     "LIMIT 100", (limit,)):
        bad = await unsubscribed(bot, r["invited_id"])
        if bad:
            await add_balance(r["inviter_id"], -r["reward"], "revoke")
            if r["l2_inviter"] and r["l2_reward"]:
                await add_balance(r["l2_inviter"], -r["l2_reward"], "revoke")
            await ex("UPDATE referrals SET status='revoked', checked=1 WHERE invited_id=?", (r["invited_id"],))
            await safe_send(bot, r["inviter_id"], "↩️ Taklif qilgan do'stingiz kanaldan chiqib ketdi, "
                                                  f"bonus (-{fmt(r['reward'])} so'm) bekor qilindi.")
        else:
            await ex("UPDATE referrals SET checked=1 WHERE invited_id=?", (r["invited_id"],))
        await asyncio.sleep(0.1)


async def job_weekly(bot):
    n = datetime.now(UZ_TZ)
    week = n.strftime("%G-%V")
    if n.weekday() != 6 or n.hour < 20 or await gs("last_week_paid") == week:
        return
    await set_setting("last_week_paid", week)
    top = await q("SELECT inviter_id, COUNT(*) c FROM referrals WHERE status='confirmed' AND confirmed_at>? "
                  "GROUP BY inviter_id ORDER BY c DESC LIMIT 3", (now() - 7 * 86400,))
    for i, r in enumerate(top):
        prize = await gs(f"week_prize_{i + 1}")
        if prize:
            await add_balance(r["inviter_id"], prize, "contest")
            await safe_send(bot, r["inviter_id"], f"🏆 Haftalik konkursda {i + 1}-o'rin! "
                                                  f"Mukofot: <b>+{fmt(prize)} so'm</b>")


async def job_report(bot):
    n = datetime.now(UZ_TZ)
    if n.hour < 9 or await gs("last_report") == today_local().isoformat():
        return
    await set_setting("last_report", today_local().isoformat())
    if OWNER_ID:
        await safe_send(bot, OWNER_ID, "🗓 <b>Kunlik hisobot</b>\n\n" + await dashboard_text())


async def jobs(bot):
    while True:
        for fn in (job_recheck, job_weekly, job_report):
            try:
                await fn(bot)
            except Exception:
                logging.exception("job xatosi: %s", fn.__name__)
        await asyncio.sleep(600)


# ----------------------------------------------------------------------------
# Umumiy xato handleri — bot hech qachon "jim" o'lmaydi
# ----------------------------------------------------------------------------
async def error_handler(event: ErrorEvent):
    logging.exception("Handler xatosi: %s", event.exception)
    return True


# ----------------------------------------------------------------------------
# Render uchun health-check server ("/" va "/health" 200 qaytaradi)
# ----------------------------------------------------------------------------
async def start_health_server():
    from aiohttp import web

    async def ok(_request):
        return web.Response(text="PulBot is working ✅")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Health-check server port %s da ishga tushdi", PORT)


# ----------------------------------------------------------------------------
async def main():
    global db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not BOT_TOKEN or not OWNER_ID:
        raise RuntimeError("BOT_TOKEN va OWNER_ID muhit o'zgaruvchilarini kiriting "
                           "(Render → Environment)")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    await load_settings()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.errors.register(error_handler)
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_router(user)
    dp.include_router(admin)
    await start_health_server()
    asyncio.create_task(jobs(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
