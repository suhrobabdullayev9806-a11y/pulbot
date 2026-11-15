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
    "start_bonus": (500, "Start bonus (so'm)", "price"),
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
            kb.row(ib(f"📢 {s['title']}", url=s["link"]))
        kb.row(ib("✅ Tekshirish", callback_data="chk", style=OK))
        await bot.send_message(chat_id, "Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
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


# ----------------------------------------------------------------------------
# FOYDALANUVCHI QISMI
# ----------------------------------------------------------------------------
@user.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    uid = m.from_user.id
    if not await get_user(uid):
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
        await ex("UPDATE users SET username=?, full_name=? WHERE id=?",
                 (m.from_user.username, m.from_user.full_name, uid))
    await answer_html(m, await gs("welcome_text"))
    if await gate(bot, uid, m.chat.id):
        await m.answer("🏠 Asosiy menyu", reply_markup=MENU)


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
        await bot.send_message(c.message.chat.id, "🏠 Asosiy menyu", reply_markup=MENU)
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
        await view_balance(m)
    elif t == B_INV:
        await view_invite(m, bot)
    elif t == B_DAILY:
        await view_daily(m)
    elif t == B_WD:
        await withdraw_start(m, state)
    elif t == B_RATE:
        await view_rating(m)
    elif t == B_PREM:
        await view_premium(m)
    elif t == B_RULES:
        await answer_html(m, await gs("rules_text"))
    elif t == B_HELP:
        await state.set_state(H.msg)
        await m.answer("✍️ Savol yoki muammoingizni yozing\n(bekor qilish: /cancel):")


async def view_balance(m: Message):
    u = await get_user(m.from_user.id)
    rows = await q("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 5", (u["id"],))
    hist = "\n".join(f"{'➕' if r['amount'] > 0 else '➖'} {fmt(abs(r['amount']))} — "
                     f"{REASONS.get(r['reason'], r['reason'])}" for r in rows) or "Hali amallar yo'q"
    await m.answer(f"💰 <b>Balans: {fmt(u['balance'])} so'm</b>\n\n"
                   f"Jami topilgan: {fmt(u['total_earned'])} so'm\n"
                   f"Jami yechilgan: {fmt(u['total_withdrawn'])} so'm\n\n"
                   f"<b>Oxirgi amallar:</b>\n{hist}")


def make_card(name, balance, link) -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (900, 500), (24, 119, 242))
        d = ImageDraw.Draw(img)
        d.text((50, 60), "Men yig'dim:", fill="white", font=ImageFont.load_default(size=50))
        d.text((50, 150), f"{fmt(balance)} so'm", fill=(255, 235, 59), font=ImageFont.load_default(size=110))
        d.text((50, 340), f"{str(name)[:30]}", fill="white", font=ImageFont.load_default(size=40))
        d.text((50, 410), link, fill="white", font=ImageFont.load_default(size=32))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        logging.exception("Karta yaratishda xato")
        return None


async def view_invite(m: Message, bot: Bot):
    uid = m.from_user.id
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
    text = (f"👥 <b>Do'st taklif qiling</b>\n\nHar bir do'st uchun: <b>{fmt(l1 * mult)} so'm</b>\n"
            f"Do'stingizning do'sti uchun: <b>{fmt(l2)} so'm</b>\n\n🔗 Havolangiz:\n<code>{link}</code>\n\n"
            f"✅ Tasdiqlangan: {st.get('confirmed', 0)}\n⏳ Kutilayotgan: {st.get('pending', 0)}\n"
            f"↩️ Bekor qilingan: {st.get('revoked', 0) + st.get('limited', 0)}")
    card = make_card(m.from_user.full_name, u["total_earned"], link)
    if card:
        await m.answer_photo(BufferedInputFile(card, "card.png"), caption=text, reply_markup=kb.as_markup())
    else:
        await m.answer(text, reply_markup=kb.as_markup())


async def view_daily(m: Message):
    u = await get_user(m.from_user.id)
    today = today_local().isoformat()
    yest = (today_local() - timedelta(days=1)).isoformat()
    if u["last_daily"] == today:
        return await m.answer(f"⏳ Bugungi bonus olingan. Streak: <b>{u['streak']}</b> kun. Ertaga qaytib keling!")
    streak = u["streak"] + 1 if u["last_daily"] == yest else 1
    lo, hi = await gs("daily_min"), await gs("daily_max")
    amt = random.randint(min(lo, hi), max(lo, hi))
    if u["is_premium"]:
        amt *= await gs("premium_multiplier")
    await ex("UPDATE users SET last_daily=?, streak=? WHERE id=?", (today, streak, u["id"]))
    await add_balance(u["id"], amt, "daily")
    text = f"🎁 Kunlik bonus: <b>+{fmt(amt)} so'm</b>\n🔥 Streak: {streak} kun"
    if streak % 7 == 0:
        sb = await gs("streak_bonus")
        if sb:
            await add_balance(u["id"], sb, "streak")
            text += f"\n🏅 7 kunlik streak bonusi: <b>+{fmt(sb)} so'm</b>"
    await m.answer(text)


async def view_rating(m: Message):
    cutoff = now() - 7 * 86400
    rows = await q("SELECT inviter_id, COUNT(*) c FROM referrals WHERE status='confirmed' AND confirmed_at>? "
                   "GROUP BY inviter_id ORDER BY c DESC LIMIT 10", (cutoff,))
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = []
    for i, r in enumerate(rows):
        u = await get_user(r["inviter_id"])
        name = esc(u["full_name"])[:20] if u else f"ID {r['inviter_id']}"
        lines.append(f"{medals[i]} {i + 1}. {name} — {r['c']} ta")
    mine = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed' "
                     "AND confirmed_at>?", (m.from_user.id, cutoff)))["c"]
    p1, p2, p3 = await gs("week_prize_1"), await gs("week_prize_2"), await gs("week_prize_3")
    await m.answer("🏆 <b>Haftalik reyting (oxirgi 7 kun)</b>\n\n" + ("\n".join(lines) or "Hali hech kim yo'q") +
                   f"\n\n📌 Sizning natijangiz: <b>{mine}</b> ta\n\n🎁 Mukofotlar (yakshanba kuni):\n"
                   f"🥇 {fmt(p1)} so'm\n🥈 {fmt(p2)} so'm\n🥉 {fmt(p3)} so'm")


async def view_premium(m: Message):
    u = await get_user(m.from_user.id)
    need = await gs("premium_refs")
    have = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed'",
                     (u["id"],)))["c"]
    mult = await gs("premium_multiplier")
    if u["is_premium"]:
        return await m.answer(f"⭐ Siz <b>Premium</b> a'zosisiz! Barcha bonuslar <b>{mult}x</b>.")
    await m.answer(f"⭐ <b>Premium</b>\n\nImkoniyat: taklif va kunlik bonuslar <b>{mult}x</b>.\n"
                   f"Shart: {need} ta tasdiqlangan taklif (sizda: {have}).\n"
                   f"Premium pulga sotilmaydi — faqat faollik uchun beriladi.")


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
async def withdraw_start(m: Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    mn = await gs("min_withdraw")
    if u["balance"] < mn:
        return await m.answer(f"❌ Minimal yechish: <b>{fmt(mn)} so'm</b>.\nSizda: {fmt(u['balance'])} so'm.")
    await state.set_state(W.amount)
    await m.answer(f"💳 Yechmoqchi bo'lgan summani kiriting (so'm).\nMin: {fmt(mn)}, sizda: {fmt(u['balance'])}\n"
                   f"Komissiya: {await gs('withdraw_fee_percent')}%\n(bekor qilish: /cancel)")


@user.message(W.amount, F.text)
async def w_amount(m: Message, state: FSMContext):
    txt = m.text.replace(" ", "")
    if not txt.isdigit():
        return await m.answer("Faqat raqam kiriting (masalan: 50000). /cancel — bekor qilish")
    amt, u = int(txt), await get_user(m.from_user.id)
    if amt < await gs("min_withdraw"):
        return await m.answer(f"Minimal summa: {fmt(await gs('min_withdraw'))} so'm")
    if amt > u["balance"]:
        return await m.answer("Balansingiz yetarli emas.")
    used = (await q1("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE user_id=? AND status!='rejected' "
                     "AND created_at>?", (u["id"], now() - 86400)))["s"]
    if used + amt > await gs("max_withdraw_daily"):
        return await m.answer(f"Kunlik limit: {fmt(await gs('max_withdraw_daily'))} so'm.")
    await state.update_data(amount=amt)
    await state.set_state(W.method)
    kb = InlineKeyboardBuilder()
    kb.row(ib("💳 Karta (Uzcard/Humo)", callback_data="wm:card", style=MAIN),
           ib("📱 Telefon puli", callback_data="wm:phone", style=OK))
    await m.answer("To'lov usulini tanlang:", reply_markup=kb.as_markup())


@user.callback_query(W.method, F.data.startswith("wm:"))
async def w_method(c: CallbackQuery, state: FSMContext):
    method = c.data.split(":")[1]
    await state.update_data(method=method)
    await state.set_state(W.details)
    await c.message.answer("Karta raqamini (16 xonali) kiriting:" if method == "card"
                           else "Telefon raqamni kiriting (+998901234567):")
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
    await m.answer(f"✅ So'rov #{wid} qabul qilindi.\nTo'lanadi: <b>{fmt(amt - fee)} so'm</b>\n"
                   f"Admin ko'rib chiqadi va sizga xabar beradi.", reply_markup=MENU)
    await notify_admins(bot, wd_text(wid, uid, amt, fee, data["method"], details, m.from_user.full_name),
                        roles=("owner", "accountant"), kb=wd_kb(wid))


def wd_text(wid, uid, amt, fee, method, details, name):
    return (f"💳 <b>Yechish so'rovi #{wid}</b>\nFoydalanuvchi: {esc(name)} (<code>{uid}</code>)\n"
            f"Summa: {fmt(amt)} (komissiya {fmt(fee)}) → <b>{fmt(amt - fee)} so'm</b>\n"
            f"Usul: {method}\nRekvizit: <code>{details}</code>")


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


def home_kb(role):
    kb = InlineKeyboardBuilder()
    for key, title in SECTIONS:
        if role in PERMS[key]:
            kb.row(ib(title, callback_data=f"adm:{key}", style=SECTION_STYLE.get(key, MAIN)))
    return kb


@admin.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    role = await role_of(m.from_user.id)
    if not role:
        return await m.answer("⛔ Sizda admin panelga ruxsat yo'q.")
    await m.answer(f"🔐 <b>Admin panel</b> ({role})", reply_markup=home_kb(role).as_markup())


@admin.message(Command("reply"))
async def cmd_reply(m: Message, command: CommandObject, bot: Bot):
    if await role_of(m.from_user.id) not in ("owner", "moderator"):
        return
    try:
        uid, text = (command.args or "").split(" ", 1)
        await bot.send_message(int(uid), f"📩 <b>Admin javobi:</b>\n\n{esc(text)}")
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
    return (f"📊 <b>Dashboard</b>\n\n👥 Jami: {fmt(total)} | Bugun: +{fmt(today)}\n"
            f"✅ Obuna bo'lgan: {fmt(ver)} | Faol (3 kun): {fmt(active)}\n\n"
            f"💵 Kirim (homiylardan): <b>{fmt(income)}</b> so'm\n💸 Chiqim (to'langan): <b>{fmt(paid)}</b> so'm\n"
            f"📈 Foyda: <b>{fmt(income - paid)}</b> so'm\n"
            f"🧾 Foydalanuvchilar balansi (qarz): {fmt(debt)} so'm\n"
            f"⏳ Kutilayotgan yechish: {pend['c']} ta ({fmt(pend['s'])} so'm)")


async def profit_calc():
    s = (await q1("SELECT COALESCE(SUM(reward),0) s FROM sponsors WHERE active=1"))["s"]
    cost = await gs("start_bonus") + await gs("ref_l1") + await gs("ref_l2")
    p = s - cost
    warn = "\n⚠️ <b>DIQQAT: zarar!</b> Bonuslarni kamaytiring yoki homiy narxini oshiring." if p < 0 else ""
    return (f"🧮 <b>Foyda kalkulyatori</b> (1 ta taklif qilingan foydalanuvchi)\nHomiylardan: {fmt(s)} so'm\n"
            f"Bonuslar (start+1+2-daraja): {fmt(cost)} so'm\n<b>Foyda: {fmt(p)} so'm</b>{warn}")


async def sponsors_view(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    lines = ["📢 <b>Homiy kanallar</b>\n"]
    rows = await q("SELECT * FROM sponsors")
    if not rows:
        lines.append("Hali kanal qo'shilmagan. Quyidagi tugma orqali qo'shing.")
    for s in rows:
        st = "🟢" if s["active"] else "🔴"
        lim = s["max_members"] or "∞"
        lines.append(f"{st} #{s['id']} {esc(s['title'])} — {fmt(s['reward'])} so'm/kishi, {s['joined']}/{lim}")
        kb.row(ib(f"🔁 #{s['id']} yoq/o'chir", callback_data=f"sp_t:{s['id']}", style=MAIN),
               ib(f"🗑 #{s['id']} o'chirish", callback_data=f"sp_d:{s['id']}", style=BAD))
    kb.row(ib("➕ Kanal qo'shish", callback_data="sp_add", style=OK))
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
        return await show(c, f"🔐 <b>Admin panel</b> ({role})", home_kb(role))
    if sec == "dash":
        return await show(c, await dashboard_text(), back_kb())
    if sec in ("prices", "sec", "adm"):
        groups = {"prices": ("price",), "sec": ("limit",), "adm": ("sys", "text")}[sec]
        kb = InlineKeyboardBuilder()
        for key, (d, title, g) in SETTINGS.items():
            if g in groups:
                val = await gs(key)
                if g == "text":
                    kb.row(ib(f"✏️ {title}", callback_data=f"set:{key}:{sec}", style=MAIN))
                else:
                    kb.row(ib(f"{title}: {val}", callback_data=f"set:{key}:{sec}", style=OK))
        text = {"prices": "💰 <b>Narxlar va bonuslar</b>\nO'zgartirish uchun tugmani bosing.\n\n" + await profit_calc(),
                "sec": "🛡 <b>Xavfsizlik</b>\n\n" + await security_text(),
                "adm": "⚙️ <b>Sozlamalar</b>\nO'zgartirish uchun tugmani bosing."}[sec]
        if sec == "adm":
            kb.row(ib("👮 Adminlar boshqaruvi", callback_data="admlist", style=MAIN))
        kb.row(back_btn("home"))
        return await show(c, text, kb)
    if sec == "wd":
        pend = (await q1("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'"))["c"]
        kb = InlineKeyboardBuilder()
        kb.row(ib(f"⏳ Kutilayotganlar ({pend})", callback_data="wdl:pending", style=MAIN))
        kb.row(ib("✅ To'langanlar", callback_data="wdl:paid", style=OK),
               ib("❌ Rad etilganlar", callback_data="wdl:rejected", style=BAD))
        kb.row(back_btn("home"))
        return await show(c, "💳 <b>Yechish so'rovlari</b>\nMin/max/komissiya: «Narxlar» bo'limida.", kb)
    if sec == "sp":
        return await sponsors_view(c)
    if sec == "users":
        await state.set_state(A.user_search)
        return await show(c, "👥 Foydalanuvchi ID yoki @username ni yuboring:\n(bekor qilish: /cancel)", back_kb())
    if sec == "bc":
        kb = InlineKeyboardBuilder()
        kb.row(ib("🌍 Hammaga", callback_data="bc:all", style=MAIN),
               ib("✅ Faollarga", callback_data="bc:active", style=OK),
               ib("⭐ Premiumga", callback_data="bc:premium", style=MAIN))
        kb.row(back_btn("home"))
        return await show(c, "📣 <b>Xabar yuborish</b>\nKimga yuboramiz?", kb)
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
        text = (f"📈 <b>Hisobot (7 kun)</b>\nYangi: {new}\nKirim: {fmt(inc)}\nTo'langan: {fmt(paid)}\n"
                f"Foyda: {fmt(inc - paid)}\n\n<b>Homiylar bo'yicha:</b>\n" +
                ("\n".join(f"• {esc(r['title'])}: {fmt(r['s'])} so'm" for r in sp) or "—") +
                "\n\n<b>Eng faol referrerlar:</b>\n" +
                ("\n".join(f"• <code>{r['inviter_id']}</code>: {r['c']}" for r in top) or "—"))
        kb = InlineKeyboardBuilder()
        kb.row(ib("📥 Foydalanuvchilar CSV", callback_data="csv:users", style=MAIN),
               ib("📥 To'lovlar CSV", callback_data="csv:wd", style=MAIN))
        kb.row(back_btn("home"))
        return await show(c, text, kb)


async def security_text():
    cards = await q("SELECT details, COUNT(DISTINCT user_id) n FROM withdrawals GROUP BY details HAVING n>1")
    rev = await q("SELECT inviter_id, COUNT(*) n FROM referrals WHERE status='revoked' GROUP BY inviter_id "
                  "HAVING n>=3 ORDER BY n DESC LIMIT 10")
    lim = (await q1("SELECT COUNT(*) c FROM referrals WHERE status='limited'"))["c"]
    t = f"Limitga tushgan takliflar: {lim}\n\n<b>Bir rekvizitda ko'p akkaunt:</b>\n"
    t += "\n".join(f"• <code>{esc(r['details'])}</code> ({r['n']} ta)" for r in cards) or "—"
    t += "\n\n<b>Obunadan chiqqan ko'p taklif (≥3):</b>\n"
    t += "\n".join(f"• <code>{r['inviter_id']}</code>: {r['n']} ta" for r in rev) or "—"
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
    await c.message.answer(f"<b>{SETTINGS[key][1]}</b>\nHozirgi qiymat: <code>{esc(cur)}</code>\n\n"
                           f"Yangi qiymatni yuboring:\n(bekor qilish: /cancel)")
    await c.answer()


@admin.message(A.val, F.text)
async def set_value(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    default = SETTINGS[key][0]
    val = m.html_text if isinstance(default, str) else m.text.replace(" ", "")
    if isinstance(default, int):
        if not val.isdigit():
            return await m.answer("Faqat musbat butun son kiriting.")
        val = int(val)
    old = await gs(key)
    await set_setting(key, val)
    await state.clear()
    await notify_admins(m.bot, f"🧾 {esc(m.from_user.full_name)} o'zgartirdi: {SETTINGS[key][1]}: "
                               f"{esc(str(old)[:40])} → {esc(str(val)[:40])}", roles=("owner",))
    await m.answer("✅ Saqlandi (darrov amal qiladi).", reply_markup=back_kb(data["back"]).as_markup())


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
        return await c.message.answer("Ro'yxat bo'sh.")
    for w in rows:
        txt = wd_text(w["id"], w["user_id"], w["amount"], w["fee"], w["method"], w["details"], w["full_name"])
        if st == "rejected":
            txt += f"\nSabab: {esc(w['reason'])}"
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
    await safe_send(bot, w["user_id"], f"✅ So'rov #{wid} to'landi: <b>{fmt(w['amount'] - w['fee'])} so'm</b>")
    try:
        await c.message.edit_text(c.message.html_text + f"\n\n✅ To'landi ({esc(c.from_user.full_name)})")
    except TelegramBadRequest:
        pass
    await c.answer("Bajarildi")


@admin.callback_query(F.data.startswith("wd_no:"))
async def wd_no(c: CallbackQuery, state: FSMContext):
    if await role_of(c.from_user.id) not in PERMS["wd"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    await state.set_state(A.reject)
    await state.update_data(wid=int(c.data.split(":")[1]))
    await c.message.answer("Rad etish sababini yozing (bekor qilish: /cancel):")
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
    await safe_send(bot, w["user_id"], f"❌ So'rov #{wid} rad etildi.\nSabab: {esc(m.text)}\n"
                                       f"Summa balansga qaytarildi.")
    await m.answer(f"❌ #{wid} rad etildi, pul foydalanuvchiga qaytarildi.")


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
    await c.message.answer("Kanalni quyidagi formatda yuboring:\n<code>@kanal_username 600 1000</code>\n"
                           "(kanal, 1 obunachi uchun sizga to'lanadigan so'm, limit; 0 = cheksiz)\n"
                           "⚠️ Bot kanalda ADMIN bo'lishi shart.\n(bekor qilish: /cancel)")
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
        return await m.answer("❌ Narx va limit faqat son bo'lishi kerak. Masalan: <code>@kanal 600 1000</code>")
    except Exception as e:
        return await m.answer(f"❌ Xato: {esc(e)}\nFormat va bot kanalda admin ekanini tekshiring.")
    await state.clear()
    await m.answer(f"✅ Qo'shildi: {esc(chat.title)}", reply_markup=back_kb("sp").as_markup())


# --- Foydalanuvchilar ---
async def user_card(uid):
    u = await get_user(uid)
    if not u:
        return None, None
    refs = (await q1("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='confirmed'", (uid,)))["c"]
    text = (f"👤 <b>{esc(u['full_name'])}</b>" + (f" @{esc(u['username'])}" if u["username"] else "") + "\n"
            f"ID: <code>{uid}</code>\n"
            f"Telefon: {esc(u['phone']) if u['phone'] else '—'}\n"
            f"Balans: <b>{fmt(u['balance'])}</b> | Topilgan: {fmt(u['total_earned'])} | "
            f"Yechilgan: {fmt(u['total_withdrawn'])}\nTakliflar: {refs} | Taklif qilgan: {u['referrer_id'] or '—'}\n"
            f"Premium: {'ha' if u['is_premium'] else 'yo‘q'} | Bloklangan: {'ha' if u['is_banned'] else 'yo‘q'}")
    kb = InlineKeyboardBuilder()
    kb.row(ib("✅ Blokdan chiqarish" if u["is_banned"] else "🚫 Bloklash",
              callback_data=f"u:ban:{uid}", style=OK if u["is_banned"] else BAD),
           ib("⭐ Premium", callback_data=f"u:prem:{uid}", style=MAIN))
    kb.row(ib("💰 Balansni o'zgartirish", callback_data=f"u:bal:{uid}", style=OK),
           ib("🌳 Referral daraxti", callback_data=f"u:tree:{uid}", style=MAIN))
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
        await state.set_state(A.user_bal)
        await state.update_data(uid=uid)
        await c.message.answer("Format: <code>+500 sabab</code> yoki <code>-300 sabab</code>\n"
                               "(sabab majburiy, bekor qilish: /cancel)")
        return await c.answer()
    elif act == "tree":
        rows = await q("SELECT r.invited_id, r.status, u.full_name FROM referrals r JOIN users u ON u.id=r.invited_id "
                       "WHERE r.inviter_id=? ORDER BY r.created_at DESC LIMIT 20", (uid,))
        await c.message.answer("🌳 <b>Takliflari (oxirgi 20):</b>\n" + (
            "\n".join(f"• {esc(r['full_name'])} (<code>{r['invited_id']}</code>) — {r['status']}" for r in rows) or "—"))
        return await c.answer()
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
        return await m.answer("Format: +500 sabab")
    uid = (await state.get_data())["uid"]
    amt = int(mt.group(1))
    u = await get_user(uid)
    if not u:
        await state.clear()
        return await m.answer("Foydalanuvchi topilmadi.")
    await add_balance(uid, amt, "admin", earn=False)
    await state.clear()
    await notify_admins(bot, f"🧾 {esc(m.from_user.full_name)}: <code>{uid}</code> balansi {amt:+d} ({esc(mt.group(2))})",
                        roles=("owner",))
    await safe_send(bot, uid, f"ℹ️ Balansingiz admin tomonidan o'zgartirildi: {amt:+d} so'm")
    await m.answer("✅ Bajarildi")


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
    await c.message.answer("Yuboriladigan xabarni jo'nating (matn, rasm, tugma bilan — hammasi bo'ladi).\n"
                           "Bekor qilish: /cancel")
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
    await m.answer("Yuborishni tasdiqlaysizmi?", reply_markup=kb.as_markup())


@admin.callback_query(F.data == "bcgo")
async def bc_go(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if not d or "where" not in d:
        return await c.answer("Eskirgan so'rov", show_alert=True)
    if await role_of(c.from_user.id) not in PERMS["bc"]:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    try:
        await c.message.edit_text("📣 Yuborish boshlandi...")
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
        await safe_send(bot, c.from_user.id, f"📣 Yuborildi: {ok}/{len(ids)}")
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
    for a in await q("SELECT * FROM admins"):
        kb.row(ib(f"🗑 {a['user_id']} ({a['role']})", callback_data=f"admdel:{a['user_id']}", style=BAD))
    kb.row(ib("➕ Admin qo'shish", callback_data="admadd", style=OK))
    kb.row(back_btn("adm"))
    await show(c, "👮 <b>Adminlar</b>\nRollar: owner, moderator, accountant (hisobchi)\n\n"
                  f"Ega (owner, .env): <code>{OWNER_ID}</code>", kb)
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
    await c.message.answer("Format: <code>123456789 moderator</code>\n"
                           "Rollar: owner, moderator, accountant\n(bekor qilish: /cancel)")
    await c.answer()


@admin.message(A.admin_add, F.text)
async def adm_save(m: Message, state: FSMContext):
    try:
        uid, role = m.text.split()
        assert role in ("owner", "moderator", "accountant")
        int(uid)
        await ex("INSERT OR REPLACE INTO admins(user_id,role) VALUES(?,?)", (int(uid), role))
    except Exception:
        return await m.answer("❌ Format noto'g'ri. Masalan: <code>123456789 moderator</code>")
    await state.clear()
    await m.answer("✅ Qo'shildi", reply_markup=back_kb("adm").as_markup())


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
