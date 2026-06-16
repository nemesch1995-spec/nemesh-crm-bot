import logging
import os
import requests
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
) = range(11)

# Колонки дошки
COLUMNS = {
    "новий лід": None,
    "перемовини": None,
    "в роботі": None,
    "пауза в роботі": None,
    "відмова": None,
    "не ліквід": None,
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
        name = lst["name"].lower()
        for col in COLUMNS:
            if col in name:
                COLUMNS[col] = lst["id"]
                break

def get_list_id(name: str):
    name = name.lower()
    for col, lid in COLUMNS.items():
        if col in name or name in col:
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
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Надиктуй summary розмови — про що говорили, що зрозумів, на чому зупинились:"
    )
    return WAIT_SUMMARY

async def got_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["summary"] = update.message.text.strip()
    await update.message.reply_text(
        "Контактні дані — нік в Telegram, Instagram, сайт (що є, через кому):"
    )
    return WAIT_CONTACTS

async def got_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contacts"] = update.message.text.strip()
    await update.message.reply_text("Сума (в євро):")
    return WAIT_SUM

async def got_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sum"] = update.message.text.strip()
    await update.message.reply_text(
        "Дата старту роботи? (формат: 18.06.2025)\n"
        "Якщо ще невідома — напиши 'пропустити'"
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

    if text.lower() != "пропустити":
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

    await update.message.reply_text(
        f"✅ Картку *{name}* створено в 'Новий лід'!\n\n"
        f"📅 Нагадування поставлено:\n"
        f"• Через 3 дні — follow-up якщо немає відповіді\n"
        + (f"• {(start_date + timedelta(days=14)).strftime('%d.%m')} — чекін через 2 тижні\n"
           f"• {(start_date + timedelta(days=30)).strftime('%d.%m')} — нагадування про оплату" if start_date else ""),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ───────────────────────────────────────────────
# КОМЕНТАР
# ───────────────────────────────────────────────

async def comment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ім'я або нік клієнта:")
    return WAIT_COMMENT_NAME

async def comment_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = find_card(update.message.text.strip())
    if not card:
        await update.message.reply_text("❌ Картку не знайдено. Перевір ім'я.")
        return ConversationHandler.END
    context.user_data["card_id"] = card["id"]
    context.user_data["card_name"] = card["name"]
    await update.message.reply_text(f"Знайшов *{card['name']}*. Що додаємо?", parse_mode="Markdown")
    return WAIT_COMMENT_TEXT

async def comment_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_comment(context.user_data["card_id"], update.message.text.strip())
    await update.message.reply_text(f"✅ Коментар додано до *{context.user_data['card_name']}*", parse_mode="Markdown")
    return ConversationHandler.END

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
            WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            WAIT_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_summary)],
            WAIT_CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_contacts)],
            WAIT_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_sum)],
            WAIT_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_start_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    comment_conv = ConversationHandler(
        entry_points=[CommandHandler("comment", comment_start)],
        states={
            WAIT_COMMENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_got_name)],
            WAIT_COMMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_got_text)],
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clients", list_clients))
    app.add_handler(new_lead_conv)
    app.add_handler(comment_conv)
    app.add_handler(move_conv)
    app.add_handler(remind_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_handler))

    scheduler.start()
    app.run_polling()

if __name__ == "__main__":
    main()
