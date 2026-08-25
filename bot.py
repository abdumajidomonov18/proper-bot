import asyncio
import logging
import json
import re
import concurrent.futures
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
)

from student_client import StudentClient

from proper_bot_full import (
    StudentClient,
    solve_multiple_choice,
    solve_construct,
    solve_write_answer,
    solve_write_answer_spell,
    solve_matching_words,
    solve_matching_words_new,
    solve_choose_answer,
    solve_checkbox
)

import os
import time
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

TOKEN = os.getenv("BOT_TOKEN", "7622338276:AAG4x9KJREIo4OZhz2gPcif0SzODTIJ-kHA")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 0.6):
        self.rate_limit = rate_limit
        self.last_user_time = {}

    async def __call__(self, handler, event, data):
        user_id = None
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id
        elif hasattr(event, "message") and event.message and event.message.from_user:
            user_id = event.message.from_user.id

        if user_id:
            now = time.time()
            last = self.last_user_time.get(user_id, 0)
            elapsed = now - last
            if elapsed < self.rate_limit:
                await asyncio.sleep(self.rate_limit - elapsed)
            self.last_user_time[user_id] = time.time()

        try:
            return await handler(event, data)
        except TelegramRetryAfter as e:
            logging.warning(f"TelegramRetryAfter: waiting {e.retry_after}s for user {user_id}")
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                return await handler(event, data)
            except Exception as retry_err:
                logging.error(f"Retry failed after retry_after: {retry_err}")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logging.error(f"TelegramBadRequest: {e}")
        except Exception as e:
            logging.error(f"Handler exception: {e}")

dp.message.middleware(AntiFloodMiddleware(rate_limit=0.6))
dp.callback_query.middleware(AntiFloodMiddleware(rate_limit=0.6))

users_db = {}
temp_clients = {}

# ─────────────────────────────────────
#  Database Persistence (SQLite)
# ─────────────────────────────────────
import sqlite3
import requests

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_sessions.db")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            phone TEXT,
            password TEXT,
            student_id TEXT,
            cookies TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS product_images (
            product_id INTEGER PRIMARY KEY,
            file_id TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_cached_image(product_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT file_id FROM product_images WHERE product_id = ?', (product_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def save_cached_image(product_id, file_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO product_images (product_id, file_id) VALUES (?, ?)', (product_id, file_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

def save_user_session(telegram_id, phone, password, student_id, session):
    cookies_dict = requests.utils.dict_from_cookiejar(session.cookies)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO users (telegram_id, phone, password, student_id, cookies)
        VALUES (?, ?, ?, ?, ?)
    ''', (telegram_id, phone, password, str(student_id or ''), json.dumps(cookies_dict)))
    conn.commit()
    conn.close()

def get_user_session(telegram_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT phone, password, student_id, cookies FROM users WHERE telegram_id = ?', (telegram_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'phone': row[0],
            'password': row[1],
            'student_id': row[2],
            'cookies': json.loads(row[3]) if row[3] else None
        }
    return None

def delete_user_session(telegram_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
    conn.commit()
    conn.close()

def verify_client_session(client):
    try:
        resp = client.session.get("https://proper.lc-up.com/student/dashboard", headers=client.headers, allow_redirects=True)
        if resp.status_code == 200 and "student" in resp.url and "login" not in resp.url:
            client.dash_html = resp.text
            return True
    except Exception:
        pass
    return False

async def get_client(chat_id) -> StudentClient:
    session_data = get_user_session(chat_id)
    if not session_data:
        users_db.pop(chat_id, None)
        return None

    client = users_db.get(chat_id)
    if client:
        is_active = await asyncio.to_thread(verify_client_session, client)
        if is_active:
            return client
        else:
            users_db.pop(chat_id, None)

    # Try saved cookies first
    client = StudentClient(session_data['phone'], session_data['password'])
    client.selected_student_id = session_data.get('student_id')

    if session_data.get('cookies'):
        client.session.cookies.update(requests.utils.cookiejar_from_dict(session_data['cookies']))
        client.is_logged_in = True
        is_active = await asyncio.to_thread(verify_client_session, client)
        if is_active:
            users_db[chat_id] = client
            return client

    # Clean auto-login if cookies expired or missing
    logging.info(f"Session expired or missing for {chat_id}. Performing clean auto-login...")
    clean_client = StudentClient(session_data['phone'], session_data['password'])
    try:
        res = await asyncio.to_thread(clean_client.login)
        if res.get('status') in ['SUCCESS', 'NEEDS_SELECTION']:
            if session_data.get('student_id'):
                await asyncio.to_thread(clean_client.select_student, str(session_data['student_id']))

            save_user_session(chat_id, session_data['phone'], session_data['password'], session_data.get('student_id'), clean_client.session)
            users_db[chat_id] = clean_client
            return clean_client
    except Exception as e:
        logging.error(f"Auto-login failed for {chat_id}: {e}")

    return None

# ─────────────────────────────────────
#  FSM States
# ─────────────────────────────────────
class LoginState(StatesGroup):
    waiting_for_phone = State()
    waiting_for_password = State()

# ─────────────────────────────────────
#  Keyboards
# ─────────────────────────────────────
def main_keyboard():
    kb = [
        [KeyboardButton(text="🪙 Hamyonim"), KeyboardButton(text="📚 Kurslarim")],
        [KeyboardButton(text="👥 Guruhlarim"), KeyboardButton(text="🛒 Do'kon")],
        [KeyboardButton(text="🚀 Avtomatik yechish"), KeyboardButton(text="🔗 Referal Tizimi")],
        [KeyboardButton(text="🚪 Chiqish")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ─────────────────────────────────────
#  /start — Kirish
# ─────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    if message.chat.id in users_db:
        await message.answer(
            "✅ Siz allaqachon tizimga kirgansiz!",
            reply_markup=main_keyboard()
        )
        return
        
    client = await get_client(message.chat.id)
    if client:
        await message.answer(
            "✅ <b>Tizimga avtomatik ravishda qayta ulandingiz!</b>\n\n"
            "Quyidagi tugmalardan foydalaning 👇",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    welcome = (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "🤖 <b>Proper Student Bot</b>ga xush kelibsiz.\n"
        "Bu bot orqali mashqlaringizni avtomatik yechishingiz mumkin.\n\n"
        "📱 Iltimos, <b>telefon raqamingizni</b> kiriting:\n"
        "<i>Masalan: +998901234567</i>"
    )
    await message.answer(welcome, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    await state.set_state(LoginState.waiting_for_phone)

# ─────────────────────────────────────
#  Login — Telefon raqam
# ─────────────────────────────────────
@dp.message(LoginState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)
    await message.answer("🔑 Endi <b>parolingizni</b> kiriting:", parse_mode="HTML")
    await state.set_state(LoginState.waiting_for_password)

# ─────────────────────────────────────
#  Login — Parol
# ─────────────────────────────────────
@dp.message(LoginState.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data.get("phone")

    msg = await message.answer("⏳ Tizimga ulanmoqda...")

    client = StudentClient(phone, password)
    res = await asyncio.to_thread(client.login)

    await msg.delete()

    if res.get('status') == 'SUCCESS':
        users_db[message.chat.id] = client
        student_id = client.session.cookies.get('student_id', '')
        client.selected_student_id = student_id
        save_user_session(message.chat.id, phone, password, student_id, client.session)
        success_text = (
            "✅ <b>Tizimga muvaffaqiyatli kirdingiz!</b>\n\n"
            "Quyidagi tugmalardan foydalaning 👇"
        )
        await message.answer(success_text, parse_mode="HTML", reply_markup=main_keyboard())
        await state.clear()

    elif res.get('status') == 'NEEDS_SELECTION':
        temp_clients[message.chat.id] = client
        students = res.get('students', [])

        kb = []
        for s in students:
            kb.append([InlineKeyboardButton(
                text=f"👤 {s['name']}",
                callback_data=f"select_student_{s['id']}"
            )])

        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await message.answer(
            "👥 Bu raqamda bir nechta akkaunt topildi.\n"
            "Qaysi biriga kirishni tanlang:",
            reply_markup=markup
        )
        await state.clear()
    else:
        await message.answer(
            "❌ <b>Login yoki parol xato!</b>\n\n"
            "Qaytadan urinish uchun /start ni bosing.",
            parse_mode="HTML"
        )
        await state.clear()

# ─────────────────────────────────────
#  Callback — Student tanlash
# ─────────────────────────────────────
@dp.callback_query(F.data.startswith("select_student_"))
async def process_student_selection(callback: types.CallbackQuery, state: FSMContext):
    student_id = callback.data.split("_")[2]
    client = temp_clients.get(callback.message.chat.id)

    if not client:
        await callback.message.answer("⚠️ Xatolik yuz berdi. Qaytadan /start bosing.")
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    msg = await callback.message.answer("⏳ Akkauntga ulanmoqda...")

    res = await asyncio.to_thread(client.select_student, student_id)

    await msg.delete()
    if res.get('status') == 'SUCCESS':
        users_db[callback.message.chat.id] = client
        del temp_clients[callback.message.chat.id]
        client.selected_student_id = student_id
        save_user_session(callback.message.chat.id, client.phone, client.password, student_id, client.session)
        await callback.message.answer(
            "✅ <b>Tizimga muvaffaqiyatli kirdingiz!</b>\n\n"
            "Quyidagi tugmalardan foydalaning 👇",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    else:
        await callback.message.answer(
            "❌ Ulanishda xatolik yuz berdi. Qaytadan /start bosing."
        )

    await callback.answer()

# ─────────────────────────────────────
#  🪙 Hamyonim
# ─────────────────────────────────────
def build_wallet_view(coins, crystals):
    text = (
        "👛 <b>Hamyoningiz holati</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 Tangalar: <b>{coins}</b>\n"
        f"💎 Kristallar: <b>{crystals}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Ballar tarixi", callback_data="coins_history")]
    ])
    return text, kb

@dp.message(F.text == "🪙 Hamyonim")
async def show_wallet(message: types.Message):
    client = await get_client(message.chat.id)
    if not client:
        await message.answer("⚠️ Avval /start orqali tizimga kiring!")
        return

    coins = await asyncio.to_thread(client.get_coins)
    crystals = await asyncio.to_thread(client.get_crystals)
    
    text, kb = build_wallet_view(coins, crystals)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "wallet_back")
async def process_wallet_back(callback: types.CallbackQuery):
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return

    await callback.answer("Yangilanmoqda...")
    coins = await asyncio.to_thread(client.get_coins)
    crystals = await asyncio.to_thread(client.get_crystals)
    
    text, kb = build_wallet_view(coins, crystals)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

def format_coin_title(title, sub):
    t_lower = (title or '').lower().strip()
    s_lower = (sub or '').lower().strip()
    
    if 'attendance' in t_lower or 'attendance' in s_lower:
        return "Teacher bahosi"
    elif 'reason' in t_lower or 'reason' in s_lower:
        return "Event uchun"
    elif 'referral' in t_lower or 'referal' in t_lower or 'referral' in s_lower:
        return "Taklif uchun"
    elif 'group' in s_lower or 'guruh' in s_lower or (title and title.isdigit()):
        return f"Teacher bergan coin ({title})"
    else:
        if sub and sub != title:
            return f"{title} ({sub})"
        return title or "Ball"

def build_coins_history_view(history_data):
    history = history_data.get('history', [])
    page = history_data.get('page', 1)
    total_pages = history_data.get('total_pages', 1)

    text = (
        f"📜 <b>Ballar tarixi</b> (Sahifa {page}/{total_pages})\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if not history:
        text += "<i>Ushbu sahifada ma'lumot topilmadi.</i>\n\n"
    else:
        for group in history:
            text += f"📅 <b>{group['date']}</b>\n"
            for item in group['items']:
                icon = "💎" if item['type'] == 'diamond' else "🪙"
                sign = item['amount']
                if not sign.startswith('+') and not sign.startswith('-') and sign != '0':
                    sign = f"+{sign}"
                
                title = format_coin_title(item.get('title', ''), item.get('sub', ''))
                text += f" • {icon} <b>{sign}</b> | {title} — <code>{item['time']}</code>\n"
            text += "\n"

    text += "━━━━━━━━━━━━━━━━━━━"

    kb = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"coins_page_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="❌", callback_data="shop_ignore"))

    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="shop_ignore"))

    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"coins_page_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="❌", callback_data="shop_ignore"))

    kb.append(nav_row)
    kb.append([InlineKeyboardButton(text="⬅️ Hamyonga qaytish", callback_data="wallet_back")])

    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    return text, markup

@dp.callback_query(F.data == "coins_history")
async def process_coins_history(callback: types.CallbackQuery):
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return

    await callback.answer("Tarix yuklanmoqda...")
    history_data = await asyncio.to_thread(client.get_coins_history, 1)
    
    text, kb = build_coins_history_view(history_data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("coins_page_"))
async def process_coins_page(callback: types.CallbackQuery):
    page_num = int(callback.data.replace("coins_page_", ""))
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return

    await callback.answer(f"{page_num}-sahifa...")
    history_data = await asyncio.to_thread(client.get_coins_history, page_num)
    
    text, kb = build_coins_history_view(history_data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

# ─────────────────────────────────────
#  🔗 Referal Tizimi
# ─────────────────────────────────────
@dp.message(F.text == "🔗 Referal Tizimi")
async def show_referrals(message: types.Message):
    client = await get_client(message.chat.id)
    if not client:
        await message.answer("⚠️ Avval /start orqali tizimga kiring!")
        return

    msg = await message.answer("🔗 Referal ma'lumotlari yuklanmoqda...")
    data = await asyncio.to_thread(client.get_referral_data)
    await msg.delete()

    if not data:
        await message.answer("⚠️ Referal ma'lumotlarini yuklab bo'lmadi.")
        return

    ref_url = data.get('referral_url', 'Topilmadi')
    referrals = data.get('referrals', [])
    
    active_list = []
    trial_list = []
    
    for r in referrals:
        r_name = r.get('name', 'Noma\'lum')
        r_status = r.get('status', 'sinov')
        r_coins = r.get('coins', 0)
        r_date = r.get('created_at', '')
        
        status_str = "Aktiv" if r_status == 'active' else "Sinovda"
        item_text = f"• <b>{r_name}</b> ({status_str}) — 🪙 <b>{r_coins}</b> bonus ({r_date})"
        if r_status == 'active':
            active_list.append(item_text)
        else:
            trial_list.append(item_text)

    text = (
        f"🔗 <b>Referal tizimi</b>\n\n"
        f"👥 Taklif qilingan do'stlar soni: <b>{len(trial_list)} ta</b>\n\n"
        f"📋 <b>Do'stlar ro'yxati:</b>\n"
    )
    
    if referrals:
        if active_list:
            text += "\n<b>Faollar:</b>\n" + "\n".join(active_list) + "\n"
        if trial_list:
            text += "\n<b>Kutishdagilar (Sinovda):</b>\n" + "\n".join(trial_list) + "\n"
    else:
        text += "<i>Hali hech kim taklif qilinmagan.</i>\n"

    text += (
        f"\n📤 <b>Sizning taklif havolangiz:</b>\n"
        f"<code>{ref_url}</code>"
    )

    import urllib.parse
    share_text = "Proper English School da ingliz tilini men bilan birga o'rganing va zavqlaning!"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(ref_url)}&text={urllib.parse.quote(share_text)}"
    
    share_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Do'stga taklif yuborish", url=share_url)]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=share_kb)

# ─────────────────────────────────────
#  👥 Guruhlarim
# ─────────────────────────────────────
def build_groups_view(groups):
    status_emojis = {
        'Aktiv': '🟢',
        'Sinov': '🟠',
        'Muzlatilgan': '❄️',
        "O'chirilgan": '🔴'
    }

    text = (
        "👥 <b>Sizning guruhlaringiz</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for g in groups:
        s_emoji = status_emojis.get(g['status'], '🔹')
        text += (
            f"{s_emoji} <b>{g['name']}</b> — <b>{g['level']}</b> ({g['status']})\n"
            f"   📅 <i>{g['days']}</i>\n"
            f"   ⏰ <code>{g['time']}</code>\n\n"
        )

    text += (
        "━━━━━━━━━━━━━━━━━━━\n"
        "<i>Batafsil ma'lumot (ustoz, to'lov) uchun guruhni tanlang:</i>"
    )

    kb = []
    row = []
    for g in groups:
        if g.get('id'):
            s_emoji = status_emojis.get(g['status'], '🔹')
            row.append(InlineKeyboardButton(text=f"{s_emoji} {g['name']}", callback_data=f"group_det_{g['id']}"))
            if len(row) == 2:
                kb.append(row)
                row = []
    if row:
        kb.append(row)

    markup = InlineKeyboardMarkup(inline_keyboard=kb) if kb else None
    return text, markup

@dp.message(F.text == "👥 Guruhlarim")
async def show_groups(message: types.Message):
    client = await get_client(message.chat.id)
    if not client:
        await message.answer("⚠️ Avval /start orqali tizimga kiring!")
        return

    msg = await message.answer("👥 Guruhlaringiz yuklanmoqda...")
    groups = await asyncio.to_thread(client.get_groups)

    if not groups:
        await msg.edit_text("📭 Sizda hech qanday guruh topilmadi.")
        return

    text, markup = build_groups_view(groups)
    await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)

@dp.callback_query(F.data.startswith("group_det_"))
async def process_group_detail(callback: types.CallbackQuery):
    group_id = callback.data.replace("group_det_", "")
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return

    await callback.answer("Yuklanmoqda...")
    detail = await asyncio.to_thread(client.get_group_detail, group_id)
    if not detail:
        await callback.answer("⚠️ Guruh tafsilotlarini yuklab bo'lmadi.", show_alert=True)
        return

    text = (
        f"🎓 <b>{detail['name']} guruhi tafsiloti</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"👨‍🏫 <b>Guruh ustozi:</b> {detail['teacher']}\n"
        f"💳 <b>Kurs narxi:</b> <code>{detail['price']} so'm</code>\n"
        f"📅 <b>Dars kunlari:</b> <i>{detail['days']}</i>\n"
        f"⏰ <b>Dars vaqti:</b> <code>{detail['time']}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Guruhlar ro'yxatiga qaytish", callback_data="group_list_back")]
    ])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "group_list_back")
async def process_group_list_back(callback: types.CallbackQuery):
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return

    await callback.answer("Yangilanmoqda...")
    groups = await asyncio.to_thread(client.get_groups)
    if not groups:
        await callback.message.edit_text("📭 Sizda hech qanday guruh topilmadi.")
        return

    text, markup = build_groups_view(groups)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

# ─────────────────────────────────────
#  📚 Kurslarim
# ─────────────────────────────────────
@dp.message(F.text == "📚 Kurslarim")
async def show_books(message: types.Message):
    client = await get_client(message.chat.id)
    if not client:
        await message.answer("⚠️ Avval /start orqali tizimga kiring!")
        return

    msg = await message.answer("📖 Kurslar ro'yxati yuklanmoqda...")
    books = await asyncio.to_thread(client.get_books)

    if not books:
        await msg.edit_text("📭 Sizda hech qanday kurs topilmadi.")
        return

    text = "📚 <b>Sizning kurslaringiz:</b>\n\n"
    for i, b in enumerate(books, 1):
        book_text = f"<b>{i}. {b['title']}</b>\n"
        units = await asyncio.to_thread(client.get_units, b['id'])
        for u in units:
            book_text += f"    📝 <i>{u['name']}</i>\n"
        book_text += "\n"

        if len(text) + len(book_text) > 3800:
            await message.answer(text, parse_mode="HTML")
            text = book_text
        else:
            text += book_text

    if text:
        await message.answer(text, parse_mode="HTML")

# ─────────────────────────────────────
#  🚪 Chiqish
# ─────────────────────────────────────
@dp.message(F.text == "🚪 Chiqish")
async def logout(message: types.Message):
    if message.chat.id in users_db:
        del users_db[message.chat.id]
    delete_user_session(message.chat.id)
    await message.answer(
        "👋 Tizimdan muvaffaqiyatli chiqdingiz.\n\n"
        "Qaytadan kirish uchun /start ni bosing.",
        reply_markup=ReplyKeyboardRemove()
    )
    
# ─────────────────────────────────────
#  🛒 Do'kon Helper & Handlers
# ─────────────────────────────────────
def get_shop_page_data(client, page=0):
    products = client.get_shop_products()
    total_products = len(products)
    items_per_page = 5
    total_pages = (total_products + items_per_page - 1) // items_per_page
    
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_products = products[start_idx:end_idx]
    
    coins = client.get_coins()
    
    text = f"🛒 <b>Proper Do'koni</b>\n\n"
    text += f"🪙 Sizning balansingiz: <b>{coins} tanga</b>\n\n"
    text += f"🛍️ <b>Mahsulotlar (Sahifa {page + 1}/{total_pages}):</b>\n"
    
    kb = []
    row = []
    for i, p in enumerate(page_products, 1):
        num = start_idx + i
        text += f"{num}. <b>{p['name']}</b> — <code>{p['tanga']}</code> tanga\n"
        
        btn = InlineKeyboardButton(text=f"{num}", callback_data=f"shop_view_{p['id']}_{page}")
        row.append(btn)
        if len(row) == 2 or i == len(page_products):
            kb.append(row)
            row = []
            
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"shop_page_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="❌", callback_data="shop_ignore"))
        
    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="shop_ignore"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"shop_page_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="❌", callback_data="shop_ignore"))
        
    kb.append(nav_row)
    kb.append([InlineKeyboardButton(text="📜 Xaridlar tarixi", callback_data="shop_history")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    return text, markup

async def display_shop_list(message_or_callback, page=0, delete_original=False):
    is_callback = isinstance(message_or_callback, types.CallbackQuery)
    chat_id = message_or_callback.message.chat.id if is_callback else message_or_callback.chat.id
    
    client = await get_client(chat_id)
    if not client:
        if is_callback:
            await message_or_callback.answer("⚠️ Avval tizimga kiring!")
        else:
            await message_or_callback.answer("⚠️ Avval tizimga kiring!")
        return
        
    text, markup = await asyncio.to_thread(get_shop_page_data, client, page)
    
    if is_callback:
        callback = message_or_callback
        if delete_original or callback.message.photo:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
        else:
            try:
                await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=markup)

@dp.message(F.text == "🛒 Do'kon")
async def show_shop(message: types.Message):
    await display_shop_list(message, page=0)

@dp.callback_query(F.data.startswith("shop_page_"))
async def process_shop_page(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    await display_shop_list(callback, page=page)

@dp.callback_query(F.data.startswith("shop_view_"))
async def process_shop_view(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    product_id = int(parts[2])
    page = int(parts[3])
    
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return
        
    await callback.answer("⏳ Yuklanmoqda...")
    
    p = await asyncio.to_thread(client.get_product_detail, product_id)
    if not p:
        await callback.message.answer("⚠️ Mahsulot tafsilotlarini yuklab bo'lmadi.")
        return
        
    text = f"📦 <b>{p['name']}</b>\n\n"
    text += f"🪙 Narxi: <b>{p['tanga']} tanga</b>\n"
    text += f"💵 So'mda: <b>{p['som']} so'm</b>\n"
    text += f"📦 Omborda qoldi: <b>{p['remaining']} ta</b>\n\n"
    text += f"🪙 Sizning balansingiz: <b>{p['student_coins']} tanga</b>"
    
    kb = [
        [InlineKeyboardButton(text="🛒 Sotib olish", callback_data=f"shop_buy_{product_id}_coins")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"shop_page_{page}")]
    ]
    
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    
    photo_sent = False
    
    # 1. Try cached telegram file_id if available
    cached_file_id = get_cached_image(product_id)
    if cached_file_id:
        try:
            sent_msg = await callback.message.answer_photo(
                photo=cached_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            try:
                await callback.message.delete()
            except Exception:
                pass
            return
        except Exception as e:
            logging.warning(f"Cached photo failed for {product_id}: {e}")
            
    # 2. Try direct image URL
    if p.get('img_url'):
        try:
            sent_msg = await callback.message.answer_photo(
                photo=p['img_url'],
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            if sent_msg.photo:
                save_cached_image(product_id, sent_msg.photo[-1].file_id)
            try:
                await callback.message.delete()
            except Exception:
                pass
            return
        except Exception as e:
            logging.warning(f"URL photo failed for {product_id}: {e}")
            
            # 3. Fallback: Download image bytes
            try:
                img_resp = await asyncio.to_thread(client.session.get, p['img_url'], headers=client.headers)
                if img_resp.status_code == 200:
                    photo_file = BufferedInputFile(img_resp.content, filename=f"product_{product_id}.png")
                    sent_msg = await callback.message.answer_photo(
                        photo=photo_file,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=markup
                    )
                    if sent_msg.photo:
                        save_cached_image(product_id, sent_msg.photo[-1].file_id)
                    try:
                        await callback.message.delete()
                    except Exception:
                        pass
                    return
            except Exception as e2:
                logging.error(f"Image download failed: {e2}")

    # Fallback to text message
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)

@dp.callback_query(F.data.startswith("shop_buy_"))
async def process_shop_buy(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    product_id = int(parts[2])
    method = parts[3]
    
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return
        
    await callback.answer("⏳ Buyurtma berilmoqda...")
    
    res = await asyncio.to_thread(client.order_product, product_id, method)
    
    if res.get('status') == 1:
        await callback.message.answer(
            f"🎉 <b>Xarid muvaffaqiyatli amalga oshirildi!</b>\n\n"
            f"ℹ️ {res.get('message')}",
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            f"❌ <b>Xarid amalga oshmadi:</b>\n"
            f"<code>{res.get('message')}</code>",
            parse_mode="HTML"
        )
        
    await display_shop_list(callback, page=0, delete_original=True)

@dp.callback_query(F.data == "shop_history")
async def process_shop_history(callback: types.CallbackQuery):
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return
        
    await callback.answer("⏳ Yuklanmoqda...")
    
    history = await asyncio.to_thread(client.get_purchase_history)
    
    if not history:
        text = "📜 <b>Sizning xaridlaringiz tarixi:</b>\n\n📭 Xaridlar tarixi bo'sh."
        kb = [[InlineKeyboardButton(text="⬅️ Do'konga qaytish", callback_data="shop_page_0")]]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        if callback.message.photo:
            await callback.message.delete()
            await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
        else:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        return
        
    text = "📜 <b>Xaridlar tarixi:</b>\n\n"
    kb = []
    for i, item in enumerate(history, 1):
        status_emoji = "⏳" if item['status'] == "Kutilmoqda" else "✅" if item['status'] == "Bajarildi" else "❌"
        text += f"{i}. <b>{item['name']}</b>\n"
        text += f"   🪙 Narxi: <code>{item['tanga']}</code> tanga\n"
        text += f"   📅 Sana: <i>{item['date']}</i>\n"
        text += f"   📊 Holati: {status_emoji} <b>{item['status']}</b>\n\n"
        
        if item['cancel_id']:
            btn = InlineKeyboardButton(text=f"❌ {i}-ni bekor qilish", callback_data=f"shop_cancel_{item['cancel_id']}")
            kb.append([btn])
            
    kb.append([InlineKeyboardButton(text="⬅️ Do'konga qaytish", callback_data="shop_page_0")])
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)

@dp.callback_query(F.data.startswith("shop_cancel_"))
async def process_shop_cancel(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    order_id = int(parts[2])
    
    client = await get_client(callback.message.chat.id)
    if not client:
        await callback.answer("⚠️ Avval tizimga kiring!")
        return
        
    await callback.answer("⏳ Buyurtma bekor qilinmoqda...")
    
    res = await asyncio.to_thread(client.cancel_order, order_id)
    
    if res.get('status') == 1:
        await callback.message.answer(
            f"✅ <b>Buyurtma muvaffaqiyatli bekor qilindi!</b>\n\n"
            f"ℹ️ {res.get('message')}",
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            f"❌ <b>Bekor qilib bo'lmadi:</b>\n"
            f"<code>{res.get('message')}</code>",
            parse_mode="HTML"
        )
        
    await process_shop_history(callback)

@dp.callback_query(F.data == "shop_ignore")
async def process_shop_ignore(callback: types.CallbackQuery):
    await callback.answer()


# ═════════════════════════════════════
#  Solver Logic
# ═════════════════════════════════════

SUPPORTED_TYPES = [
    'select_one', 'select-one',
    'multiple_choice', 'multiple-choice',
    'write_answer', 'write-answer',
    'extra_word', 'extra-word',
    'choose_answer', 'choose-answer',
    'matching_questions', 'matching-questions',
    'test',
    'construct', 'make_sentence', 'make-sentence',
    'matching_words', 'matching-words',
    'write_answer_spell', 'write-answer-spell',
    'matching_words_new', 'matching-words-new',
    'checkbox'
]

def solve_exercise_task(c, ex_type, ex_id, pct):
    try:
        if ex_type in ['select_one', 'select-one']:
            solve_multiple_choice(c, ex_id, 'select_one')
        elif ex_type in ['multiple_choice', 'multiple-choice']:
            res = solve_choose_answer(c, ex_id, ex_type)
            if res and res.get('status') == 'error':
                solve_multiple_choice(c, ex_id, ex_type)
        elif ex_type in ['choose_answer', 'choose-answer']:
            solve_choose_answer(c, ex_id, ex_type)
        elif ex_type in ['matching_questions', 'matching-questions']:
            solve_multiple_choice(c, ex_id, ex_type)
        elif ex_type == 'test':
            solve_multiple_choice(c, ex_id, ex_type)
        elif ex_type in ['write_answer_spell', 'write-answer-spell']:
            solve_write_answer_spell(c, ex_id, ex_type)
        elif ex_type in ['write_answer', 'write-answer']:
            solve_write_answer(c, ex_id, ex_type)
        elif ex_type in ['matching_words', 'matching-words']:
            solve_matching_words(c, ex_id, ex_type)
        elif ex_type in ['matching_words_new', 'matching-words-new']:
            solve_matching_words_new(c, ex_id, ex_type)
        elif ex_type in ['construct', 'make_sentence', 'make-sentence']:
            solve_construct(c, ex_id, ex_type)
        elif ex_type == 'checkbox':
            solve_checkbox(c, ex_id, ex_type)
    except Exception:
        pass

# ─────────────────────────────────────
#  Auto-solver scanner
# ─────────────────────────────────────
def run_auto_solver_sync(c: StudentClient, progress: dict):
    r_study = c.session.get('https://proper.lc-up.com/student/study', headers=c.headers)
    soup_study = BeautifulSoup(r_study.text, 'html.parser')

    units = []
    for tag in soup_study.find_all(attrs={'wire:initial-data': True}):
        data = json.loads(tag['wire:initial-data'])
        d = data.get('serverMemo', {}).get('data', {})
        for k, v in d.items():
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict) and 'percentage' in v[0]:
                for item in v:
                    if 'id' in item and item.get('percentage') != 100:
                        units.append(item['id'])

    tasks_to_run = []
    for unit in units:
        r = c.session.get(f'https://proper.lc-up.com/student/study/{unit}/lessons', headers=c.headers)
        soup = BeautifulSoup(r.text, 'html.parser')

        h = c.headers.copy()
        h['X-Livewire'] = 'true'

        lessons_list = []
        for tag in soup.find_all(attrs={'wire:initial-data': True}):
            data = json.loads(tag['wire:initial-data'])
            if 'lessons' in data.get('serverMemo', {}).get('data', {}):
                lessons_list = data['serverMemo']['data']['lessons']
                break

        for lesson in lessons_list:
            if lesson.get('percentage') == 100:
                continue

            for t2 in soup.find_all(attrs={'wire:initial-data': True}):
                d2 = json.loads(t2['wire:initial-data'])
                if d2.get('fingerprint', {}).get('name') == 'student-web.student-lesson-exercises-livewire':
                    comp_name = d2['fingerprint']['name']
                    fingerprint = d2['fingerprint']
                    serverMemo = d2['serverMemo']

                    r2 = c.session.post(
                        'https://proper.lc-up.com/livewire/message/' + comp_name,
                        json={
                            'fingerprint': fingerprint,
                            'serverMemo': serverMemo,
                            'updates': [{
                                'type': 'fireEvent',
                                'payload': {'id': 'load', 'event': 'loadExercises', 'params': [lesson['id']]}
                            }]
                        },
                        headers=h
                    )

                    html = r2.json().get('effects', {}).get('html', '')
                    esoup = BeautifulSoup(html, 'html.parser')
                    for a in esoup.find_all('div', onclick=True):
                        onclick = a['onclick']
                        if 'window.location.href' in onclick and 'exercises' in onclick:
                            href = onclick.split("'")[1]
                            pct = '0%'
                            for span in a.find_all('span'):
                                if '%' in span.text:
                                    pct = span.text.strip()

                            match = re.search(r'exercises/([^/]+)/(\d+)', href)
                            if match:
                                ex_type = match.group(1)
                                ex_id = match.group(2)

                                if pct == '100%':
                                    continue
                                if ex_type not in SUPPORTED_TYPES:
                                    continue

                                tasks_to_run.append((ex_type, ex_id, pct))

    if not tasks_to_run:
        progress['finished'] = True
        return 0

    progress['total'] = len(tasks_to_run)
    progress['stage'] = 'solving'

    def wrap_task(args):
        c_obj, type_str, id_str, p_str = args
        solve_exercise_task(c_obj, type_str, id_str, p_str)
        progress['done'] += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        executor.map(wrap_task, [(c, ex_type, ex_id, pct) for ex_type, ex_id, pct in tasks_to_run])

    progress['finished'] = True
    return len(tasks_to_run)

# ─────────────────────────────────────
#  🚀 Avtomatik yechish
# ─────────────────────────────────────
@dp.message(F.text == "🚀 Avtomatik yechish")
async def start_auto_solver(message: types.Message):
    client = await get_client(message.chat.id)
    if not client:
        await message.answer("⚠️ Avval /start orqali tizimga kiring!")
        return

    msg = await message.answer(
        "🔍 <b>Akkauntingiz tekshirilmoqda...</b>\n\n"
        "📖 Barcha kurslar va darslar skanlanmoqda.\n"
        "⏳ Bu biroz vaqt olishi mumkin...",
        parse_mode="HTML"
    )

    progress = {'stage': 'scanning', 'total': 0, 'done': 0, 'finished': False}

    task = asyncio.create_task(asyncio.to_thread(run_auto_solver_sync, client, progress))

    last_text = ""
    last_edit_time = 0
    last_done = -1

    while not progress['finished']:
        await asyncio.sleep(2.5)
        if progress['finished']:
            break

        now = time.time()
        if now - last_edit_time < 4.0:
            continue

        if progress['stage'] == 'scanning':
            text = (
                "🔍 <b>Akkauntingiz tekshirilmoqda...</b>\n\n"
                "📖 Barcha kurslar va darslar skanlanmoqda.\n"
                "⏳ Bu biroz vaqt olishi mumkin..."
            )
        else:
            total = progress['total']
            done = progress['done']
            if done == last_done:
                continue
            last_done = done
            pct = int((done / total) * 100) if total > 0 else 0

            bar_len = 15
            filled = int(bar_len * pct // 100)
            bar = '▓' * filled + '░' * (bar_len - filled)

            text = (
                f"⚡ <b>Mashqlar yechilmoqda...</b>\n\n"
                f"<code>[{bar}]</code> <b>{pct}%</b>\n\n"
                f"✅ Bajarildi:  <b>{done}</b> / <b>{total}</b>\n"
                f"⏳ Qoldi:      <b>{total - done}</b> ta"
            )

        if text != last_text:
            try:
                await msg.edit_text(text, parse_mode="HTML")
                last_text = text
                last_edit_time = time.time()
            except Exception:
                pass

    try:
        total_solved = await task
        if total_solved == 0:
            await msg.edit_text(
                "🎉 <b>Hammasi tayyor!</b>\n\n"
                "Barcha mashqlar allaqachon 100% bajarilgan\n"
                "yoki hozircha ochiq mashqlar yo'q.",
                parse_mode="HTML"
            )
        else:
            await msg.edit_text(
                f"🏆 <b>Avtomatik yechish yakunlandi!</b>\n\n"
                f"📊 Jami <b>{total_solved} ta</b> mashq topildi va yechildi.\n"
                f"✅ Barchasi <b>100%</b> ga yetkazildi!\n\n"
                f"💰 Tangalaringizni tekshirib ko'ring!",
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Error finishing auto solve: {e}")
        try:
            await msg.edit_text(f"❌ <b>Xatolik yuz berdi:</b>\n<code>{e}</code>", parse_mode="HTML")
        except Exception:
            pass

# ─────────────────────────────────────
#  Start polling
# ─────────────────────────────────────
async def main():
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
