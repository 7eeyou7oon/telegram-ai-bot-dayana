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
if os.getenv("RENDER"):
    API_KEY = os.getenv("API_KEY")
else:
    API_KEY = "*****"

ADMIN_ID =  8523339855 # 👈 ВСТАВЬ сюда ID (аккаунта в ТГ) который нужен

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


def save_to_db(name, phone, service, region, visit_date, visit_time):
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
        region TEXT,
        visit_date TEXT,
        visit_time TEXT
        )
    """)
    cursor.execute("PRAGMA table_info(requests)")
    columns = [col[1] for col in cursor.fetchall()]

    if "region" not in columns:
        cursor.execute("ALTER TABLE requests ADD COLUMN region TEXT")

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
        SELECT created_at, name, phone, service, region, visit_date, visit_time
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
        SELECT created_at, name, phone, service, region, visit_date, visit_time
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

    ws.append(["Создано", "Имя", "Телефон", "Услуга", "Регион", "Дата", "Время"])

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
        [KeyboardButton(text="🚀 Начать")],
        [KeyboardButton(text="📋 Программа")],
        [KeyboardButton(text="💰 Выплаты")],
        [KeyboardButton(text="🏠 Жильё")],
        [KeyboardButton(text="📞 Подать заявку")],
        [KeyboardButton(text="🔄 Сбросить")]
    ],
    resize_keyboard=True
)



def catalog():

    buttons = [
        [InlineKeyboardButton(text="🏡 Работа в селе", callback_data="s:Работа в селе")],
        [InlineKeyboardButton(text="💰 Подъемные выплаты", callback_data="s:Подъемные выплаты")],
        [InlineKeyboardButton(text="🏠 Кредит на жильё", callback_data="s:Кредит на жильё")],
        [InlineKeyboardButton(text="👩‍🏫 Работа учителем", callback_data="s:Работа учителем")],

        # доп. кнопки
        [InlineKeyboardButton(text="📞 Оставить заявку", callback_data="o:general")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да"), KeyboardButton(text="❌ Нет")],
            [KeyboardButton(text="📋 Программа")],
            [KeyboardButton(text="🏠 Главное меню")]
        ],
        resize_keyboard=True
    )

# =====================
# SYSTEM PROMPT (CRITICAL FIX)
# =====================

SYSTEM_PROMPT = """
Ты — консультант государственной программы «С дипломом в село».

ТВОЯ РОЛЬ:
- объяснять условия программы
- уточнять образование пользователя
- уточнять специальность
- уточнять регион
- помогать оформить заявку

ПРАВИЛА:
- отвечай просто и понятно
- не придумывай несуществующие условия
- помогай пользователю понять подходит ли ему программа
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
        "👋 Добро пожаловать!\n\n"
        "Я помогу вам узнать о программе работы в селе и оформить заявку.",
        reply_markup=kb
    )

# =====================
# BUTTONS
# =====================

@dp.callback_query(lambda c: c.data == "home")
async def home(call: types.CallbackQuery):
    uid = call.from_user.id

    user_state[uid] = None
    user_contacts.pop(uid, None)
    user_last_service.pop(uid, None)
    user_stage[uid] = "discover"
    user_histories.pop(uid, None)

    await call.message.answer("🏠 Главное меню", reply_markup=kb)
    await call.answer()


@dp.message(lambda m: m.text == "📋 Программа")
async def program_info(m: types.Message):
    await m.answer(
        "📋 Государственная программа:\n\n"
        "Вы можете поехать работать в село по специальности.\n"
        "Доступны подъемные выплаты и жильё."
    )


@dp.message(lambda m: m.text == "💰 Выплаты")
async def payments(m: types.Message):
    await m.answer(
        "💰 Подъемные выплаты:\n\n"
        "Выплачиваются при переезде.\n"
        "Сумма зависит от региона."
    )


@dp.message(lambda m: m.text == "🏠 Жильё")
async def housing(m: types.Message):
    await m.answer(
        "🏠 Жильё:\n\n"
        "Предоставляется льготный кредит на покупку дома в селе."
    )


@dp.message(lambda m: m.text == "📞 Подать заявку")
async def start_application(m: types.Message):
    user_contacts[m.from_user.id] = {
        "step": "name",
        "service": "Работа в селе"
    }
    await m.answer("Введите ваше имя:")


@dp.message(lambda m: m.text == "🔄 Сбросить")
async def reset(m: types.Message):
    uid = m.from_user.id

    user_histories[uid] = []
    user_contacts.pop(uid, None)
    user_state[uid] = None
    user_requirements[uid] = {}
    user_last_offer[uid] = None

    await m.answer("🔄 Данные сброшены", reply_markup=kb)


@dp.message(lambda m: "работа" in m.text.lower() or "село" in m.text.lower())
async def start_gov_flow(m: types.Message):
    uid = m.from_user.id

    user_state[uid] = "gov_form"
    user_requirements[uid] = {}

    await m.answer("🎓 У вас есть диплом? Какая специальность?")

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
        f"📌 {key}\n\n💡 {knowledge_base[key]['text']}\n\nХотите оформить заявку?",
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
    clean_text = text.replace("🏠", "").strip()

    # =====================
    # GLOBAL RESET (ЛУЧШЕ ВСЕХ ПЕРВЫМ)
    # =====================
    if "главное меню" in clean_text:
        user_state[uid] = None
        user_contacts.pop(uid, None)
        user_last_service.pop(uid, None)
        user_stage[uid] = "discover"
        user_histories.pop(uid, None)

        await m.answer("🏠 Главное меню", reply_markup=kb)
        return

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
    clean_text = (m.text or "").lower().replace("🏠", "").strip()
    if "главное меню" in clean_text:
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
        step = user_contacts[uid]["step"]

        # ИМЯ
        if step == "name":
            user_contacts[uid]["name"] = m.text.strip()
            user_contacts[uid]["step"] = "phone"
            await m.answer("📱 Введите телефон (пример: +77001234567):")
            return

        # ТЕЛЕФОН (валидация)
        if step == "phone":
            phone = re.sub(r"\D", "", m.text)

            if len(phone) < 10:
                await m.answer("❌ Неверный телефон. Введите корректный номер:")
                return

            user_contacts[uid]["phone"] = phone
            user_contacts[uid]["step"] = "region"
            await m.answer("📍 Укажите регион (например: Аксу):")
            return

        # РЕГИОН
        if step == "region":
            user_contacts[uid]["region"] = m.text.strip()
            user_contacts[uid]["step"] = "date"
            await m.answer("📅 Введите дату (ДД.ММ.ГГГГ):")
            return

        # ДАТА (валидация)
        if step == "date":
            try:
                datetime.strptime(m.text, "%d.%m.%Y")
                user_contacts[uid]["date"] = m.text
                user_contacts[uid]["step"] = "time"
                await m.answer("⏰ Введите время (например: 14:30):")
            except:
                await m.answer("❌ Неверный формат даты. Пример: 25.04.2026")
            return

        # ВРЕМЯ (валидация)
        if step == "time":
            if not re.match(r"^\d{1,2}:\d{2}$", m.text):
                await m.answer("❌ Неверный формат времени. Пример: 14:30")
                return

            user_contacts[uid]["time"] = m.text
            user_contacts[uid]["step"] = "confirm"

            data = user_contacts[uid]

            await m.answer(
                f"📋 Проверьте данные:\n\n"
                f"👤 Имя: {data['name']}\n"
                f"📱 Телефон: {data['phone']}\n"
                f"📍 Регион: {data['region']}\n"
                f"📅 Дата: {data['date']}\n"
                f"⏰ Время: {data['time']}\n\n"
                f"Подтвердить?",
                reply_markup=confirm_kb()
            )
            return

        # ПОДТВЕРЖДЕНИЕ
        if step == "confirm":

            if "да" in m.text.lower():
                data = user_contacts[uid]

                save_to_db(
                    data["name"],
                    data["phone"],
                    data["service"],
                    data["region"],
                    data["date"],
                    data["time"]
                )

                await m.answer(
                    "✅ Заявка успешно создана!\n"
                    "С вами свяжутся в ближайшее время."
                )

                user_contacts.pop(uid)
                return

            elif "нет" in m.text.lower():
                user_contacts.pop(uid)
                await m.answer("❌ Заявка отменена")
                return

    # =====================
    # CONFIRM FLOW
    # =====================
    if user_state.get(uid) == "confirm":

        if "да" in text:
            service = user_last_service.get(uid)

            user_contacts[uid] = {
                "step": "name",
                "service": service
            }

            user_state[uid] = "order"
            user_stage[uid] = "order"

            await m.answer("📦 Отлично! Давайте оформим заявку.\n\nВведите имя:")
            return

        if "нет" in text:
            user_state[uid] = "catalog"
            await m.answer("📦 Каталог:", reply_markup=catalog())
            return

        if "каталог" in text:
            user_state[uid] = "catalog"
            await m.answer("📦 Каталог:", reply_markup=catalog())
            return

        if user_state.get(uid) in ["order", "confirm"]:
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
    # GOV PROGRAM FLOW (FIXED)
    # =====================
    if user_state.get(uid) == "gov_form":

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
                "— льготный кредит\n\n"
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
