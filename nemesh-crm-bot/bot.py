import logging
import os
import requests
import tempfile
from openai import OpenAI
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_BOARD_ID = os.getenv("TRELLO_BOARD_ID", "X9M3JzKk")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")  # твій Telegram ID
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

TRELLO_API = "https://api.trello.com/1"

# Користувацькі поля
CUSTOM_FIELD_DATE = "6a315fbc2805833132e855f7"
CUSTOM_FIELD_SUM = "6a315fcb13392d98b2f5927d"

# Стани розмови
(
    WAIT_NAME, WAIT_SUMMARY, WAIT_CONTACTS, WAIT_SUM, WAIT_START_DATE,
    WAIT_COMMENT_NAME, WAIT_COMMENT_TEXT,
    WAIT_MOVE_NAME, WAIT_MOVE_STATUS,
    WAIT_REMIND_NAME, WAIT_REMIND_TIME,
    WAIT_COMMENT_REMIND,
    WAIT_SETDATE_CLIENT, WAIT_SETDATE_DATE,
    WAIT_NEWLEAD_REMIND, WAIT_NEWLEAD_REMIND_DATE,
) = range(16)

# Колонки дошки
COLUMNS = {
    "новий лід": "6a315f500cf27f0f4e7be45a",
    "перемовини": "6a315f6bff39a1ef901f07ae",
    "в роботі": "6a315f710dfcc8d8f1625bf1",
    "пауза в роботі": "6a315f7ade44f22387f91208",
    "відмова": "6a315f80792f2a4d1eb5b52b",
    "не ліквід": "6a315f85700fc99b8e599abe",
}

scheduler = AsyncIOScheduler()

# ───────────────────────────────────────────────
# TRELLO HELPERS
# ───────────────────────────────────────────────

def trello_params(**kwargs):
    return {"key": TRELLO_KEY, "token": TRELLO_TOKEN, **kwargs}

def load_lists():
    r = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/lists", params=trello_params())
    for lst in r.json():
        name = lst["name"].lower().replace("і", "i")
        for col in COLUMNS:
            col_norm = col.lower().replace("і", "i")
            if col_norm in name:
                COLUMNS[col] = lst["id"]
                break

def get_list_id(name: str):
    name = name.lower().replace("і", "i")
    for col, lid in COLUMNS.items():
        col_norm = col.lower().replace("і", "i")
        if col_norm in name or name in col_norm:
            return lid
    return None

def find_card(name: str):
    r = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/cards", params=trello_params())
    cards = r.json()
    name_lower = name.lower()
    for card in cards:
        if name_lower in card["name"].lower():
            return card
    return None

def create_card(name: str, desc: str, list_id: str, due: str = None):
    data = trello_params(name=name, desc=desc, idList=list_id)
    if due:
        data["due"] = due
    r = requests.post(f"{TRELLO_API}/cards", params=data)
    return r.json()

def add_comment(card_id: str, text: str):
    requests.post(f"{TRELLO_API}/cards/{card_id}/actions/comments", params=trello_params(text=text))

def move_card(card_id: str, list_id: str):
    requests.put(f"{TRELLO_API}/cards/{card_id}", params=trello_params(idList=list_id))

def set_due_date(card_id: str, due: str):
    requests.put(f"{TRELLO_API}/cards/{card_id}", params=trello_params(due=due))

def set_custom_field_date(card_id: str, date_iso: str):
    requests.put(
        f"{TRELLO_API}/card/{card_id}/customField/{CUSTOM_FIELD_DATE}/item",
        json={"value": {"date": date_iso}},
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN}
    )

def set_custom_field_sum(card_id: str, amount: str):
    requests.put(
        f"{TRELLO_API}/card/{card_id}/customField/{CUSTOM_FIELD_SUM}/item",
        json={"value": {"text": amount}},
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN}
    )

# ───────────────────────────────────────────────
# /start
# ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я твій CRM-бот для Trello.\n\n"
        "Що вмію:\n"
        "🟢 /new_lead — новий клієнт\n"
        "💬 /comment — додати коментар\n"
        "🔀 /move — змінити статус\n"
        "⏰ /remind — нагадування\n"
        "📋 /clients — список активних клієнтів\n\n"
        "• /setdate — встановити дату старту\n\n"
        "Або просто пиши текстом — я зрозумію!"
    )

# ───────────────────────────────────────────────
# НОВИЙ ЛІД
# ───────────────────────────────────────────────

async def new_lead_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Як звати клієнта? (ім'я або нік)")
    return WAIT_NAME

async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_NAME
    else:
        text = update.message.text.strip()
    context.user_data["name"] = text
    await update.message.reply_text(
        "Надиктуй summary розмови — про що говорили, що зрозумів, на чому зупинились:"
    )
    return WAIT_SUMMARY

async def got_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_SUMMARY
    else:
        text = update.message.text.strip()
    context.user_data["summary"] = text
    await update.message.reply_text(
        "Контактні дані — нік в Telegram, Instagram, сайт (що є, через кому):"
    )
    return WAIT_CONTACTS

async def got_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_CONTACTS
    else:
        text = update.message.text.strip()
    context.user_data["contacts"] = text
    await update.message.reply_text("Сума (в євро):")
    return WAIT_SUM

async def got_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_SUM
    else:
        text = update.message.text.strip()
    context.user_data["sum"] = text
    keyboard = ReplyKeyboardMarkup([["⏭ Пропустити"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Дата старту роботи? (формат: 18.06.2025)",
        reply_markup=keyboard
    )
    return WAIT_START_DATE

async def got_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    name = context.user_data["name"]
    summary = context.user_data["summary"]
    contacts = context.user_data["contacts"]
    amount = context.user_data["sum"]

    start_date = None
    due_iso = None

    if text.lower() not in ["пропустити", "⏭ пропустити", "пропустити", "skip"]:
        try:
            start_date = datetime.strptime(text, "%d.%m.%Y")
            due_iso = start_date.isoformat() + "Z"
        except ValueError:
            pass

    desc = f"📞 Контакти:\n{contacts}"

    list_id = COLUMNS.get("новий лід")
    if not list_id:
        await update.message.reply_text("❌ Не знайшов колонку 'Новий лід' на дошці.")
        return ConversationHandler.END

    card = create_card(name, desc, list_id)
    set_custom_field_sum(card["id"], amount)
    if start_date:
        set_custom_field_date(card["id"], start_date.strftime("%Y-%m-%dT00:00:00.000Z"))
    add_comment(card["id"], f"📋 Summary:\n{summary}")

    # Планування нагадувань
    chat_id = update.effective_chat.id

    # Нагадування через 3 дні якщо не відповів
    remind_time = datetime.now() + timedelta(days=3)
    scheduler.add_job(
        send_reminder,
        "date",
        run_date=remind_time,
        args=[context.application, chat_id, f"⚠️ Лід {name} — 3 дні без відповіді. Час нагадати про себе!"],
        id=f"followup_{card['id']}",
        replace_existing=True
    )

    # Нагадування про оплату через місяць після старту
    if start_date:
        payment_date = start_date + timedelta(days=30)
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=payment_date,
            args=[context.application, chat_id, f"💰 {name} — час виставити рахунок за наступний місяць!"],
            id=f"payment_{card['id']}",
            replace_existing=True
        )

        # Чекін через 2 тижні
        checkin_date = start_date + timedelta(days=14)
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=checkin_date,
            args=[context.application, chat_id, f"🔔 {name} — 2 тижні роботи. Напиши клієнту як справи з рекламою!"],
            id=f"checkin_{card['id']}",
            replace_existing=True
        )

    keyboard = ReplyKeyboardMarkup(
        [["⏰ Нагадати через 3 дні", "📅 Вказати дату"], ["✅ Не треба"]],
        one_time_keyboard=True, resize_keyboard=True
    )
    reminders_text = ""
    if start_date:
        reminders_text = (
            f"\n\n📅 Авто-нагадування:\n"
            f"• {(start_date + timedelta(days=14)).strftime('%d.%m')} — чекін\n"
            f"• {(start_date + timedelta(days=30)).strftime('%d.%m')} — оплата"
        )
    context.user_data["card_id_new"] = card["id"]
    context.user_data["card_name_new"] = name
    await update.message.reply_text(
        f"\u2705 Картку {name} створено!{reminders_text}\n\nДодаткове нагадування?",
        reply_markup=keyboard
    )
    return WAIT_NEWLEAD_REMIND

async def newlead_got_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    name = context.user_data.get("card_name_new", "")
    card_id = context.user_data.get("card_id_new", "")

    if "не треба" in text.lower() or "✅" in text:
        await update.message.reply_text("👍", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    elif "3 дні" in text.lower():
        remind_time = datetime.now() + timedelta(days=3)
        scheduler.add_job(
            send_reminder, "date", run_date=remind_time,
            args=[context.application, chat_id, f"⏰ Нагадування: {name}"],
            id=f"custom_remind_{card_id}", replace_existing=True
        )
        await update.message.reply_text(
            f"✅ Нагадаю через 3 дні про {name}",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "Вкажи дату нагадування (формат: 25.06.2025):",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAIT_NEWLEAD_REMIND_DATE

async def newlead_got_remind_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    name = context.user_data.get("card_name_new", "")
    card_id = context.user_data.get("card_id_new", "")
    try:
        remind_time = datetime.strptime(text, "%d.%m.%Y")
        scheduler.add_job(
            send_reminder, "date", run_date=remind_time,
            args=[context.application, chat_id, f"⏰ Нагадування: {name}"],
            id=f"custom_remind_{card_id}", replace_existing=True
        )
        await update.message.reply_text(f"✅ Нагадаю {text} про {name}")
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Спробуй: 25.06.2025")
        return WAIT_NEWLEAD_REMIND_DATE
    return ConversationHandler.END


# ───────────────────────────────────────────────
# КОМЕНТАР
# ───────────────────────────────────────────────

async def comment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ім'я або нік клієнта:")
    return WAIT_COMMENT_NAME

async def comment_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_COMMENT_NAME
    else:
        text = update.message.text.strip()
    card = find_card(text)
    if not card:
        await update.message.reply_text("❌ Картку не знайдено. Перевір ім'я.")
        return ConversationHandler.END
    context.user_data["card_id"] = card["id"]
    context.user_data["card_name"] = card["name"]
    await update.message.reply_text(f"Знайшов *{card['name']}*. Що додаємо?", parse_mode="Markdown")
    return WAIT_COMMENT_TEXT

async def comment_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_COMMENT_TEXT
    else:
        text = update.message.text.strip()
    add_comment(context.user_data["card_id"], text)
    keyboard = ReplyKeyboardMarkup([["⏰ Так, нагадати", "✅ Ні, все"]], one_time_keyboard=True, resize_keyboard=True)
    name = context.user_data["card_name"]
    await update.message.reply_text(
        f"\u2705 Коментар додано до *{name}*\n\nПоставити нагадування по цьому клієнту?",
        parse_mode="Markdown"
    )
    return WAIT_COMMENT_REMIND

async def comment_got_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if "ні" in text or "все" in text:
        await update.message.reply_text("👍", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await update.message.reply_text(
        "Коли нагадати?\n• через 3 дні\n• через 2 тижні\n• 25.06.2025",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAIT_REMIND_TIME

# ───────────────────────────────────────────────
# ЗМІНА СТАТУСУ
# ───────────────────────────────────────────────

STATUS_KEYBOARD = [["Новий лід", "Перемовини"], ["В роботі", "Пауза в роботі"], ["Відмова", "Не ліквід"]]

async def move_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ім'я або нік клієнта:")
    return WAIT_MOVE_NAME

async def move_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = find_card(update.message.text.strip())
    if not card:
        await update.message.reply_text("❌ Картку не знайдено.")
        return ConversationHandler.END
    context.user_data["card_id"] = card["id"]
    context.user_data["card_name"] = card["name"]
    await update.message.reply_text(
        f"Куди переміщаємо *{card['name']}*?",
        reply_markup=ReplyKeyboardMarkup(STATUS_KEYBOARD, one_time_keyboard=True),
        parse_mode="Markdown"
    )
    return WAIT_MOVE_STATUS

async def move_got_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = update.message.text.strip()
    list_id = get_list_id(status)
    if not list_id:
        await update.message.reply_text("❌ Не знайшов таку колонку.")
        return ConversationHandler.END
    move_card(context.user_data["card_id"], list_id)
    await update.message.reply_text(
        f"✅ *{context.user_data['card_name']}* переміщено в '{status}'",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ───────────────────────────────────────────────
# НАГАДУВАННЯ
# ───────────────────────────────────────────────

async def remind_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ім'я клієнта або про що нагадати:")
    return WAIT_REMIND_NAME

async def remind_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["remind_about"] = update.message.text.strip()
    await update.message.reply_text(
        "Коли нагадати? Приклади:\n"
        "• через 3 дні\n"
        "• через 1 тиждень\n"
        "• через 1 місяць\n"
        "• 25.06.2025"
    )
    return WAIT_REMIND_TIME

async def remind_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    about = context.user_data["remind_about"]
    chat_id = update.effective_chat.id
    remind_time = None

    if "дні" in text or "день" in text or "днів" in text:
        days = int(''.join(filter(str.isdigit, text)) or 1)
        remind_time = datetime.now() + timedelta(days=days)
    elif "тиждень" in text or "тижні" in text or "тижнів" in text:
        weeks = int(''.join(filter(str.isdigit, text)) or 1)
        remind_time = datetime.now() + timedelta(weeks=weeks)
    elif "місяць" in text or "місяці" in text:
        remind_time = datetime.now() + timedelta(days=30)
    else:
        try:
            remind_time = datetime.strptime(text, "%d.%m.%Y")
        except ValueError:
            await update.message.reply_text("❌ Не зрозумів формат. Спробуй: 'через 3 дні' або '25.06.2025'")
            return WAIT_REMIND_TIME

    scheduler.add_job(
        send_reminder,
        "date",
        run_date=remind_time,
        args=[context.application, chat_id, f"⏰ Нагадування: {about}"],
        id=f"remind_{about}_{remind_time.timestamp()}",
        replace_existing=True
    )

    await update.message.reply_text(
        f"✅ Нагадаю про *{about}* {remind_time.strftime('%d.%m.%Y о %H:%M')}",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ───────────────────────────────────────────────
# СПИСОК АКТИВНИХ КЛІЄНТІВ
# ───────────────────────────────────────────────

async def list_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    list_id = COLUMNS.get("в роботі")
    if not list_id:
        await update.message.reply_text("❌ Не знайшов колонку 'В роботі'")
        return

    r = requests.get(f"{TRELLO_API}/lists/{list_id}/cards", params=trello_params())
    cards = r.json()

    if not cards:
        await update.message.reply_text("Наразі немає активних клієнтів в 'В роботі'")
        return

    text = "📋 *Активні клієнти:*\n\n"
    for card in cards:
        due = card.get("due", "")
        due_str = ""
        if due:
            due_date = datetime.fromisoformat(due.replace("Z", ""))
            due_str = f" | Старт: {due_date.strftime('%d.%m.%Y')}"
        text += f"• {card['name']}{due_str}\n"

    await update.message.reply_text(text, parse_mode="Markdown")



async def transcribe_voice(update: Update) -> str | None:
    """Розпізнає голосове і повертає текст"""
    if not OPENAI_API_KEY:
        await update.message.reply_text("❌ OpenAI API ключ не налаштований")
        return None
    await update.message.reply_text("🎙 Розпізнаю...")
    try:
        file = await update.get_bot().get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language="uk"
            )
        return transcript.text
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Помилка розпізнавання")
        return None

# ───────────────────────────────────────────────
# ГОЛОСОВІ ПОВІДОМЛЕННЯ
# ───────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Конвертує голосове поза розмовою"""
    # Якщо є активна розмова — не перехоплюємо
    if context.user_data:
        return

    if not OPENAI_API_KEY:
        await update.message.reply_text("❌ OpenAI API ключ не налаштований")
        return

    await update.message.reply_text("🎙 Розпізнаю голосове...")

    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name

        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="uk"
            )

        text = transcript.text
        await update.message.reply_text(f"📝 Розпізнано: {text}")
        update.message.text = text
        await smart_handler(update, context)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Помилка розпізнавання. Спробуй ще раз.")


# ───────────────────────────────────────────────
# ВСТАНОВИТИ ДАТУ СТАРТУ
# ───────────────────────────────────────────────

async def setdate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отримуємо список клієнтів з колонок "В роботі" і "Новий лід" і "Перемовини"
    r = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/cards", params=trello_params())
    cards = r.json()

    active_lists = []
    for lid in [COLUMNS.get("в роботі")]:
        if lid:
            active_lists.append(lid)

    active_cards = [c for c in cards if c.get("idList") in active_lists]

    if not active_cards:
        await update.message.reply_text("Немає активних клієнтів.")
        return ConversationHandler.END

    context.user_data["setdate_cards"] = {c["name"]: c["id"] for c in active_cards}

    # Кнопки по 2 в ряд
    names = [c["name"] for c in active_cards]
    keyboard = [names[i:i+2] for i in range(0, len(names), 2)]
    keyboard.append(["❌ Скасувати"])

    await update.message.reply_text(
        "Вибери клієнта:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return WAIT_SETDATE_CLIENT

async def setdate_got_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    cards = context.user_data.get("setdate_cards", {})
    card_id = cards.get(text)

    if not card_id:
        # Спробуємо знайти часткове співпадіння
        for name, cid in cards.items():
            if text.lower() in name.lower():
                card_id = cid
                text = name
                break

    if not card_id:
        await update.message.reply_text("❌ Клієнта не знайдено. Спробуй ще.")
        return WAIT_SETDATE_CLIENT

    context.user_data["setdate_card_id"] = card_id
    context.user_data["setdate_card_name"] = text

    await update.message.reply_text(
        f"Дата старту для *{text}*? (формат: 18.06.2025)",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return WAIT_SETDATE_DATE

async def setdate_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    card_id = context.user_data["setdate_card_id"]
    name = context.user_data["setdate_card_name"]
    chat_id = update.effective_chat.id

    try:
        start_date = datetime.strptime(text, "%d.%m.%Y")
        set_custom_field_date(card_id, start_date.strftime("%Y-%m-%dT00:00:00.000Z"))

        # Нагадування про оплату через місяць
        payment_date = start_date + timedelta(days=30)
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=payment_date,
            args=[context.application, chat_id, f"💰 {name} — час виставити рахунок за наступний місяць!"],
            id=f"payment_{card_id}",
            replace_existing=True
        )

        # Чекін через 2 тижні
        checkin_date = start_date + timedelta(days=14)
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=checkin_date,
            args=[context.application, chat_id, f"🔔 {name} — 2 тижні роботи. Напиши клієнту як справи з рекламою!"],
            id=f"checkin_{card_id}",
            replace_existing=True
        )

        msg = (
            f"✅ Дата старту {text} встановлена для {name}\n\n"
            f"Нагадування:\n"
            f"• {checkin_date.strftime('%d.%m')} — чекін через 2 тижні\n"
            f"• {payment_date.strftime('%d.%m')} — нагадування про оплату"
        )
        await update.message.reply_text(msg)
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Спробуй: 18.06.2025")
        return WAIT_SETDATE_DATE

    return ConversationHandler.END

# ───────────────────────────────────────────────
# РОЗПІЗНАВАННЯ ТЕКСТУ (без команд)
# ───────────────────────────────────────────────

async def smart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if any(w in text for w in ["новий лід", "новий клієнт", "нова заявка"]):
        return await new_lead_start(update, context)
    elif any(w in text for w in ["коментар", "нотатка", "додай"]):
        return await comment_start(update, context)
    elif any(w in text for w in ["перемісти", "змін статус", "статус"]):
        return await move_start(update, context)
    elif any(w in text for w in ["нагадай", "нагадування", "нагади"]):
        return await remind_start(update, context)
    else:
        await update.message.reply_text(
            "Не зрозумів 🤔 Спробуй:\n"
            "• /new_lead — новий клієнт\n"
            "• /comment — коментар\n"
            "• /move — змінити статус\n"
            "• /remind — нагадування\n"
            "• /clients — активні клієнти"
        )

# ───────────────────────────────────────────────
# НАГАДУВАННЯ (функція)
# ───────────────────────────────────────────────

async def send_reminder(app, chat_id, text):
    await app.bot.send_message(chat_id=chat_id, text=text)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ───────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────

def main():
    load_lists()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    new_lead_conv = ConversationHandler(
        entry_points=[CommandHandler("new_lead", new_lead_start)],
        states={
            WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name), MessageHandler(filters.VOICE, got_name)],
            WAIT_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_summary), MessageHandler(filters.VOICE, got_summary)],
            WAIT_CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_contacts), MessageHandler(filters.VOICE, got_contacts)],
            WAIT_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_sum), MessageHandler(filters.VOICE, got_sum)],
            WAIT_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_start_date), MessageHandler(filters.VOICE, got_start_date)],
            WAIT_NEWLEAD_REMIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, newlead_got_remind)],
            WAIT_NEWLEAD_REMIND_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, newlead_got_remind_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    comment_conv = ConversationHandler(
        entry_points=[CommandHandler("comment", comment_start)],
        states={
            WAIT_COMMENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_got_name), MessageHandler(filters.VOICE, comment_got_name)],
            WAIT_COMMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_got_text), MessageHandler(filters.VOICE, comment_got_text)],
            WAIT_COMMENT_REMIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_got_remind)],
            WAIT_REMIND_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, remind_got_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    move_conv = ConversationHandler(
        entry_points=[CommandHandler("move", move_start)],
        states={
            WAIT_MOVE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, move_got_name)],
            WAIT_MOVE_STATUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, move_got_status)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    remind_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", remind_start)],
        states={
            WAIT_REMIND_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, remind_got_name)],
            WAIT_REMIND_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, remind_got_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    setdate_conv = ConversationHandler(
        entry_points=[CommandHandler("setdate", setdate_start)],
        states={
            WAIT_SETDATE_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setdate_got_client)],
            WAIT_SETDATE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setdate_got_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clients", list_clients))
    app.add_handler(new_lead_conv)
    app.add_handler(setdate_conv)
    app.add_handler(comment_conv)
    app.add_handler(move_conv)
    app.add_handler(remind_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    scheduler.start()
    app.run_polling()

if __name__ == "__main__":
    main()
