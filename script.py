import asyncio
import sqlite3
import re
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)

from openai import OpenAI
from openpyxl import Workbook
from rapidfuzz import fuzz

# НАСТРОЙКИ

from aiohttp import web

async def handle(request):
    return web.Response(text="Bot is running")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_KEY = os.getenv("API_KEY")
ADMIN_ID = 8523339855  # 👈 ВСТАВЬ сюда ID (аккаунта в ТГ) который нужен

client = OpenAI(
    base_url="https://models.inference.ai.azure.com",
    api_key=API_KEY
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# =====================
# STATE
# =====================

user_histories = {}
user_contacts = {}
user_state = {}
user_last_service = {}
user_stage = {}
user_requirements = {}
user_last_offer = {}



# =====================
# MEMORY (PRO FIX)
# =====================

def add_memory(uid, role, text):
    if uid not in user_histories:
        user_histories[uid] = []

    user_histories[uid].append({
        "role": role,
        "content": text
    })

    # ограничение памяти
    user_histories[uid] = user_histories[uid][-10:]

# ADMIN CHECK


def is_admin(message: types.Message):
    return message.from_user.id == ADMIN_ID



# =====================
# DB
# =====================

DB_FILE = "requests.db"


def save_to_db(name, phone, service, visit_date, visit_time):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO requests
        (created_at, name, phone, service, visit_date, visit_time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        name,
        phone,
        service,
        visit_date,
        visit_time
    ))

    conn.commit()
    conn.close()


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            name TEXT,
            phone TEXT,
            service TEXT,
            visit_date TEXT,
            visit_time TEXT
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_date
        ON requests(created_at)
    """)

    conn.commit()
    conn.close()


# =====================
# ADMIN QUERIES (IMPORTANT)
# =====================

def get_requests(limit=10):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT created_at, name, phone, service, visit_date, visit_time
        FROM requests
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    data = cursor.fetchall()
    conn.close()
    return data


def get_requests_by_date(date_from, date_to):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT created_at, name, phone, service, visit_date, visit_time
        FROM requests
        WHERE created_at BETWEEN ? AND ?
        ORDER BY id DESC
    """, (
        date_from.strftime("%Y-%m-%d 00:00:00"),
        date_to.strftime("%Y-%m-%d 23:59:59")
    ))

    data = cursor.fetchall()
    conn.close()
    return data

# =====================
# EXPORT
# =====================

def export_to_excel(data):
    file = "export.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "CRM"

    ws.append(["Дата", "Имя", "Телефон", "Услуга", "Дата визита", "Время"])

    for row in data:
        ws.append(row)

    wb.save(file)
    return file


# =====================
# DATE/TIME PARSER
# =====================

def extract_datetime(text):
    text = text.lower()

    visit_date = None
    visit_time = None

    date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    if date_match:
        visit_date = date_match.group()

    time_match = re.search(r"(\d{1,2})[: ]?(\d{2})?", text)
    if time_match:
        h = time_match.group(1)
        m = time_match.group(2) or "00"
        visit_time = f"{h.zfill(2)}:{m}"

    if "завтра" in text and not visit_date:
        visit_date = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")

    return visit_date, visit_time

# =====================
# HYBRID KNOWLEDGE BASE
# =====================

knowledge_base = {
    "Работа в селе": {
        "text": "Госпрограмма: работа в селе с подъемными и льготным кредитом на жильё.",
        "aliases": ["село работа", "работа в селе", "программа с дипломом"]
    },

    "Подъемные выплаты": {
        "text": "Выплачиваются подъемные при переезде в село (сумма зависит от региона).",
        "aliases": ["подъемные", "выплаты", "деньги при переезде"]
    },

    "Кредит на жильё": {
        "text": "Доступен льготный кредит на жильё для специалистов в селе.",
        "aliases": ["жилье", "кредит", "дом в селе"]
    },

    "Работа учителем": {
        "text": "Можно устроиться учителем (например, информатики) в сельской школе.",
        "aliases": ["учитель", "информатика", "работа учителем"]
    }
}


# HYBRID SEARCH

def find_service(text):
    text = text.lower()

    best, best_score = None, 0

    for k, v in knowledge_base.items():

        if k in text:
            return v["text"]

        for a in v["aliases"]:
            if a in text:
                return v["text"]

        score = fuzz.partial_ratio(text, k)

        for a in v["aliases"]:
            score = max(score, fuzz.partial_ratio(text, a))

        if score > best_score:
            best_score = score
            best = v["text"]

    return best if best_score >= 65 else None

# КНОПКИ

kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Старт")],
        [KeyboardButton(text="🔄 Сбросить память")],
        [KeyboardButton(text="📦 Услуги")],
        [KeyboardButton(text="❓ Вопросы")],
        [KeyboardButton(text="📞 Оставить заявку")]
    ],
    resize_keyboard=True
)


def catalog():
    buttons = [
        [InlineKeyboardButton(text=k, callback_data=f"s:{k}")]
        for k in knowledge_base.keys()
    ]

    # добавляем кнопку "Главное меню" отдельной строкой
    buttons.append([
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да"), KeyboardButton(text="❌ Нет")],
            [KeyboardButton(text="📦 Каталог")], [KeyboardButton(text="🏠 Главное меню")]
        ],
        resize_keyboard=True
    )

# =====================
# SYSTEM PROMPT (CRITICAL FIX)
# =====================

SYSTEM_PROMPT = """
Ты — профессиональный консультант сервисного центра и компьютерного магазина.

ТВОЯ РОЛЬ:
- сначала выяснить потребности клиента
- задавать уточняющие вопросы
- НЕ предлагать услугу сразу
- только после понимания задачи — предлагать вариант

СТИЛЬ ДИАЛОГА:
1. уточняющие вопросы
2. уточнение бюджета и целей
3. предложение решения
4. переход к заявке

ПРАВИЛА:
- не торопись с продажей
- всегда сначала уточняй
- веди клиента как консультант в магазине
"""


# =====================
# INLINE CATALOG
# =====================

# def build_services_menu():
#     return InlineKeyboardMarkup(inline_keyboard=[
#         [InlineKeyboardButton(text=key, callback_data=f"service:{key}")]
#         for key in knowledge_base.keys()
#     ])

# START COMMAND


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_histories[message.from_user.id] = []

    await message.answer(
        "👋 Я AI-консультант сервисного центра.\n",
        reply_markup=kb
    )



# =====================
# BUTTONS
# =====================

@dp.message(lambda msg: msg.text == "🚀 Старт")
async def start_button(message: types.Message):
    user_histories[message.from_user.id] = []
    await message.answer("🚀 Новый диалог начат!", reply_markup=kb)


@dp.message(lambda msg: msg.text == "🔄 Сбросить память")
async def clear_memory(message: types.Message):
    user_histories[message.from_user.id] = []
    await message.answer("🧹 Память очищена")

@dp.message(lambda m: m.text == "📦 Услуги")
async def cat(m: types.Message):
    user_state[m.from_user.id] = "catalog"
    await m.answer("📦 Каталог услуг:", reply_markup=catalog())

@dp.message(lambda msg: msg.text == "🏠 Главное меню")
async def main_menu(m: types.Message):
    uid = m.from_user.id

    user_state[uid] = None
    user_contacts.pop(uid, None)
    user_histories[uid] = []

    await m.answer("🏠 Главное меню", reply_markup=kb)


# обработчик Home

@dp.callback_query(lambda c: c.data == "home")
async def home_handler(call: types.CallbackQuery):
    uid = call.from_user.id

    user_state.pop(uid, None)
    user_contacts.pop(uid, None)
    user_last_service.pop(uid, None)
    user_histories.pop(uid, None)

    await call.message.answer("🏠 Главное меню", reply_markup=kb)
    await call.answer()


# =====================
# INLINE SERVICE CARD
# =====================

@dp.callback_query(lambda c: c.data.startswith("s:"))
async def service(call: types.CallbackQuery):
    uid = call.from_user.id
    key = call.data.split(":")[1]

    user_state[uid] = "confirm"
    user_last_service[uid] = key

    # 👇 ВОТ ЗДЕСЬ inline кнопки
    kb_inline = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Оставить заявку", callback_data=f"o:{key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
    ])

    await call.message.answer(
        f"📦 {key}\n\n💡 {knowledge_base[key]['text']}\n\nПодходит ли вам услуга?",
        reply_markup=kb_inline
    )

    await call.answer()

# =====================
# ORDER FROM SERVICE
# =====================

@dp.callback_query(lambda c: c.data.startswith("o:"))
async def order(call):
    user_contacts[call.from_user.id] = {
        "step": "name",
        "service": call.data.split(":")[1]
    }

    await call.message.answer("Введите имя:")
    await call.answer()


# =====================
# BACK
# =====================


@dp.callback_query(lambda c: c.data == "back")
async def back(call):
    uid = call.from_user.id

    user_state[uid] = "catalog"

    await call.message.answer(
        "📦 Каталог услуг:",
        reply_markup=catalog()
    )

    await call.answer()

# ADMIN

@dp.message(Command("admin"))
async def admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return

    data = get_requests(10)

    if not data:
        await message.answer("📭 Нет заявок")
        return

    text = "📊 Последние заявки:\n\n"
    for i, row in enumerate(data, 1):
        text += f"{i}. {row[0]}\n{row[1]} | {row[2]}\n\n"

    await message.answer(text)


# EXPORT

@dp.message(Command("admin_export"))
async def export(message: types.Message):
    if not is_admin(message):
        await message.answer("⛔ Нет доступа")
        return

    data = get_requests(1000)

    if not data:
        await message.answer("📭 Нет данных")
        return

    file = export_to_excel(data)
    await message.answer_document(FSInputFile(file))


# FILTER

@dp.message(Command("admin_date"))
async def filter_date(message: types.Message):
    if not is_admin(message):
        await message.answer("⛔ Нет доступа")
        return

    try:
        args = message.text.split()
        d1 = datetime.strptime(args[1], "%d.%m.%Y")
        d2 = datetime.strptime(args[2], "%d.%m.%Y")

        data = get_requests_by_date(d1, d2)

        if not data:
            await message.answer("📭 Нет данных")
            return

        text = f"📅 Заявки с {args[1]} по {args[2]}:\n\n"
        for i, row in enumerate(data, 1):
            text += f"{i}. {row[0]}\n{row[1]} | {row[2]}\n\n"

        await message.answer(text)

    except:
        await message.answer("Формат: /admin_date 01.04.2026 05.04.2026")


# =====================
# CHAT + AI + заявки (PRO FIX)
# =====================
@dp.message()
async def chat(m: types.Message):
    uid = m.from_user.id
    text = (m.text or "").lower()

    # =====================
    # INIT SAFE STATE
    # =====================
    if uid not in user_histories:
        user_histories[uid] = []

    if uid not in user_state:
        user_state[uid] = None

    if uid not in user_stage:
        user_stage[uid] = "discover"

    if uid not in user_requirements:
        user_requirements[uid] = {
            "shown_services": []
        }

    if uid not in user_last_offer:
        user_last_offer[uid] = None

    # =====================
    # SYNC STATE → STAGE
    # =====================
    if user_state.get(uid) == "order":
        user_stage[uid] = "order"

    elif user_state.get(uid) == "confirm":
        user_stage[uid] = "qualify"

    # =====================
    # MEMORY INPUT
    # =====================
    add_memory(uid, "user", text)

    # =====================
    # GLOBAL RESET
    # =====================
    if text == "главное меню":
        user_state[uid] = None
        user_contacts.pop(uid, None)
        user_last_service.pop(uid, None)
        user_stage[uid] = "discover"

        await m.answer("🏠 Главное меню", reply_markup=kb)
        return

    # =====================
    # ORDER FLOW
    # =====================
    if uid in user_contacts:
        user_stage[uid] = "order"
        step = user_contacts[uid]["step"]

        if step == "name":
            user_contacts[uid]["name"] = m.text
            user_contacts[uid]["step"] = "phone"
            await m.answer("📱 Введите телефон:")
            return

        if step == "phone":
            user_contacts[uid]["phone"] = m.text
            user_contacts[uid]["step"] = "date"
            await m.answer("📅 Введите дату:")
            return

        if step == "date":
            user_contacts[uid]["date"] = m.text
            user_contacts[uid]["step"] = "time"
            await m.answer("⏰ Введите время:")
            return

        if step == "time":
            name = user_contacts[uid]["name"]
            phone = user_contacts[uid]["phone"]
            service = user_contacts[uid]["service"]
            date = user_contacts[uid]["date"]
            time = m.text

            save_to_db(name, phone, service, date, time)

            await m.answer(
                f"✅ Заявка создана! и передана специалистам\n\n"
                f"📋 Программа: {service}\n📍 Регион: {user_requirements[uid].get('region', '-')}"
            )

            user_contacts.pop(uid, None)
            user_state[uid] = None
            user_stage[uid] = "discover"
            return

    # =====================
    # CONFIRM FLOW
    # =====================
    if user_state.get(uid) == "confirm":

        if "да" in text:
            service = user_last_service.get(uid)

            if service:
                user_contacts[uid] = {
                    "step": "name",
                    "service": service
                }

                user_state[uid] = "order"
                user_stage[uid] = "order"

                await m.answer(f"📦 {service}\n\nВведите имя:")
            return

        if "нет" in text:
            user_state[uid] = "catalog"
            await m.answer("📦 Каталог:", reply_markup=catalog())
            return

        if "каталог" in text:
            user_state[uid] = "catalog"
            await m.answer("📦 Каталог:", reply_markup=catalog())
            return

    # =====================
    # SERVICE DETECTION (ANTI-REPEAT FIX)
    # =====================
    service_text = find_service(text)

    if service_text and user_state.get(uid) not in ["order", "catalog"]:

        # ❌ защита от повторов
        if service_text in user_requirements[uid]["shown_services"]:
            return

        key = next((k for k, v in knowledge_base.items() if v["text"] == service_text), None)

        user_state[uid] = "confirm"
        user_last_service[uid] = key

        user_requirements[uid]["shown_services"].append(service_text)
        user_last_offer[uid] = service_text

        add_memory(uid, "assistant", service_text)

        await m.answer(
            f"💡 {service_text}\n\nПодходит?",
            reply_markup=confirm_kb()
        )
        return

    # =====================
    # GOV PROGRAM FLOW
    # =====================
    if "диплом" in text or "работа" in text or "село" in text:

        if "education" not in user_requirements[uid]:
            user_requirements[uid]["education"] = m.text
            await m.answer("🎓 Какая у вас специальность?")
            return

        if "specialty" not in user_requirements[uid]:
            user_requirements[uid]["specialty"] = m.text
            await m.answer("📍 В каком регионе хотите работать?")
            return

        if "region" not in user_requirements[uid]:
            user_requirements[uid]["region"] = m.text
            await m.answer(
                "💡 Вам подходит программа:\n"
                "— подъемные выплаты\n"
                "— льготный кредит на жильё\n\n"
                "Хотите оформить заявку?"
            )
            return

    # =====================
    # PRO AI FALLBACK (FIXED LOGIC)
    # =====================
    stage = user_stage.get(uid, "discover")

    prompt = f"""
СТАДИЯ ДИАЛОГА: {stage}

УЖЕ ПОКАЗАННЫЕ УСЛУГИ:
{user_requirements[uid]["shown_services"]}

ПРАВИЛА:
- НЕ повторяй услуги
- сначала задай вопросы (бюджет, цель, устройство)
- не предлагай решение сразу
- если данных мало → спрашивай
- если достаточно → предложи НОВУЮ услугу

КОНТЕКСТ:
{user_requirements.get(uid, {})}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": prompt}
        ] + user_histories[uid]
    )

    answer = resp.choices[0].message.content

    add_memory(uid, "assistant", answer)

    await m.answer(answer)

# ЗАПУСК

async def main():
    init_db()
    print("Бот запущен 🚀")

    await asyncio.gather(
        dp.start_polling(bot),
        start_web()
    )

if __name__ == "__main__":
    asyncio.run(main())