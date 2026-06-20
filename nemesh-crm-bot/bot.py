import logging
import os
import re
import json
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
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN = os.getenv("TODOIST_TOKEN")
TODOIST_API = "https://api.todoist.com/api/v1"

TRELLO_API = "https://api.trello.com/1"

CUSTOM_FIELD_DATE = "6a315fbc2805833132e855f7"
CUSTOM_FIELD_SUM = "6a315fcb13392d98b2f5927d"

(
    WAIT_NAME, WAIT_SUMMARY, WAIT_CONTACTS, WAIT_SUM, WAIT_START_DATE,
    WAIT_COMMENT_NAME, WAIT_COMMENT_TEXT,
    WAIT_MOVE_NAME, WAIT_MOVE_STATUS,
    WAIT_REMIND_NAME, WAIT_REMIND_TIME,
    WAIT_COMMENT_REMIND,
    WAIT_SETDATE_CLIENT, WAIT_SETDATE_DATE,
    WAIT_NEWLEAD_REMIND, WAIT_NEWLEAD_REMIND_DATE,
    WAIT_STATS_QUERY,
    WAIT_TASK_VOICE,
    WAIT_CALL_CLIENT, WAIT_CALL_STATUS_PICK, WAIT_CALL_CLIENT_PICK, WAIT_CALL_LINK,
) = range(22)

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

def normalize(s: str) -> str:
    return (s.lower()
              .replace("і", "i").replace("ї", "i")
              .replace("'", "").replace("'", "")
              .strip())

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

def get_all_cards_with_lists():
    """Повертає всі картки з назвою колонки"""
    r = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/cards", params=trello_params())
    cards = r.json()

    id_to_col = {v: k for k, v in COLUMNS.items()}

    result = []
    for card in cards:
        col_name = id_to_col.get(card.get("idList"), "невідомо")
        result.append({
            "name": card["name"],
            "status": col_name,
            "due": card.get("due", ""),
            "desc": card.get("desc", ""),
            "url": card.get("url", ""),
        })
    return result

def get_cards_in_list(list_id: str):
    """Повертає картки конкретної колонки {name: id}"""
    r = requests.get(f"{TRELLO_API}/lists/{list_id}/cards", params=trello_params())
    cards = r.json()
    return {c["name"]: c["id"] for c in cards}

def get_card_comments(card_id: str) -> list:
    """Тягне всі коментарі картки"""
    r = requests.get(
        f"{TRELLO_API}/cards/{card_id}/actions",
        params=trello_params(filter="commentCard")
    )
    comments = []
    for action in r.json():
        text = action.get("data", {}).get("text", "")
        date = action.get("date", "")[:10]
        if text:
            comments.append(f"[{date}] {text}")
    return comments

# ───────────────────────────────────────────────
# GPT HELPERS
# ───────────────────────────────────────────────

def gpt_analyze_summary(summary_text: str) -> dict:
    if not OPENAI_API_KEY:
        return {}

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""Ти — CRM-асистент маркетингового агентства. 
Проаналізуй summary розмови з потенційним клієнтом і витягни ключові деталі.

Summary:
{summary_text}

Відповідай ТІЛЬКИ у форматі:
🎯 Ціль: [що хоче досягти]
💰 Бюджет: [бюджет або "не озвучено"]
😤 Болі: [основні проблеми/болі клієнта]
🏪 Ніша: [сфера бізнесу]
➡️ Наступний крок: [що обговорили далі]

Якщо якесь поле не згадується — пиши "не вказано". Відповідай українською."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.3
        )
        text = response.choices[0].message.content.strip()
        return {"formatted": text}
    except Exception as e:
        logger.error(f"GPT analyze error: {e}")
        return {}


def gpt_analyze_call(transcript_text: str, call_date: str) -> str:
    """
    Аналізує ПОВНУ транскрипцію дзвінка (не коротке summary).
    Більше шуму/small talk у вхідному тексті - промпт явно просить фільтрувати.
    Формат виводу узгоджений з gpt_analyze_summary, але з заголовком дзвінка.
    """
    if not OPENAI_API_KEY:
        return ""

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""Ти — CRM-асистент маркетингового агентства NEMESH.
Перед тобою повна транскрипція дзвінка власника агентства з клієнтом. 
У ній багато "шуму": привітання, small talk, паузи, повтори, технічні збої зв'язку.
Твоя задача — відфільтрувати все зайве і витягнути тільки суттєву ділову інформацію.

Транскрипція дзвінка:
{transcript_text}

Відповідай ТІЛЬКИ у форматі (без жодних вступних фраз від себе):
🎯 Ціль: [що хоче досягти клієнт]
💰 Бюджет: [конкретна сума, ЯКЩО вона ЧІТКО і ОДНОЗНАЧНО прозвучала в розмові - інакше пиши "не озвучено". НІКОЛИ не вигадуй і не округлюй суму сам]
😤 Болі: [основні проблеми/болі клієнта]
🏪 Ніша: [сфера бізнесу]
✅ Домовленості: [про що конкретно домовились]
📅 Дати/терміни: [конкретні дати чи терміни, якщо прозвучали]
➡️ Наступний крок: [що обговорили далі, хто що робить]

Якщо якесь поле не згадується — пиши "не вказано". Відповідай українською, лаконічно, без води."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.2
        )
        analysis = response.choices[0].message.content.strip()
        return f"📞 Дзвінок {call_date}\n\n{analysis}"
    except Exception as e:
        logger.error(f"GPT call analyze error: {e}")
        return ""


def gpt_stats_answer(query: str, cards: list) -> str:
    if not OPENAI_API_KEY:
        return "❌ OpenAI API ключ не налаштований"

    client = OpenAI(api_key=OPENAI_API_KEY)

    cards_text = ""
    for c in cards:
        due_str = ""
        if c["due"]:
            try:
                due_date = datetime.fromisoformat(c["due"].replace("Z", ""))
                due_str = f", старт: {due_date.strftime('%d.%m.%Y')}"
            except:
                pass
        cards_text += f"- {c['name']} | статус: {c['status']}{due_str}\n"

    if not cards_text:
        cards_text = "Карток не знайдено"

    prompt = f"""Ти — CRM-аналітик маркетингового агентства NEMESH.

Ось поточний стан проектів (дані з Trello):
{cards_text}

Запит від власника агентства: "{query}"

Відповідай чітко, структуровано, українською. 
Якщо запит про кількість — давай цифри і список.
Якщо запит про аналіз — давай короткі висновки з рекомендаціями.
Будь лаконічним, як розумний асистент, а не як звіт."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.4
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"GPT stats error: {e}")
        return "❌ Помилка при аналізі. Спробуй ще раз."


def gpt_client_answer(card: dict, comments: list) -> str:
    if not OPENAI_API_KEY:
        return "❌ OpenAI API ключ не налаштований"

    client = OpenAI(api_key=OPENAI_API_KEY)

    id_to_col = {v: k for k, v in COLUMNS.items()}
    status = id_to_col.get(card.get("idList"), "невідомо")

    comments_text = "\n".join(comments) if comments else "Коментарів немає"

    prompt = f"""Ти — CRM-асистент маркетингового агентства NEMESH.

Власник агентства питає про клієнта. Ось дані:

Клієнт: {card['name']}
Статус: {status}
Опис картки: {card.get('desc', 'немає')}

Коментарі та нотатки:
{comments_text}

Дай чіткий короткий зріз: що відомо про клієнта, на якому етапі він зараз, що було останнє і що варто зробити далі. Відповідай українською, лаконічно."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"GPT client answer error: {e}")
        return "❌ Помилка при аналізі"


def gpt_parse_task(text: str) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY)

    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Сьогодні {today}. Ти — асистент з планування. 
Проаналізуй текст і витягни задачу для Todoist.

Текст: "{text}"

Відповідай ТІЛЬКИ у форматі JSON (без markdown):
{{
  "title": "назва задачі",
  "due_date": "YYYY-MM-DD або null якщо дата не вказана",
  "due_time": "HH:MM або null якщо час не вказаний",
  "priority": 4,
  "description": "додаткові деталі якщо є або пустий рядок"
}}

Пріоритет: 4=звичайний, 3=середній, 2=високий, 1=терміновий.
Визнач пріоритет за словами: терміново/важливо/критично → 1-2, звичайні задачі → 3-4.
Дати: сьогодні={today}, завтра=наступний день, "в п'ятницю"=найближча п'ятниця тощо."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1
        )
        result = json.loads(response.choices[0].message.content.strip())
        return result
    except Exception as e:
        logger.error(f"GPT parse task error: {e}")
        return {"title": text, "due_date": None, "due_time": None, "priority": 4, "description": ""}

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
        "📋 /clients — список активних клієнтів\n"
        "📊 /stats — аналітика по проектах\n"
        "📞 /call_summary — додати запис дзвінка клієнту\n\n"
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
        "Надиктуй summary розмови — про що говорили, що зрозумів, на чому зупинились.\n\n"
        "Або кинь посилання на Google Drive із повним записом дзвінка — я сам зроблю транскрипцію і аналіз:"
    )
    return WAIT_SUMMARY

async def got_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text: return WAIT_SUMMARY
        gpt_analysis_text = None
    else:
        text = update.message.text.strip()
        drive_link = extract_drive_link(text)
        if drive_link:
            # Повний запис дзвінка замість короткого summary
            result = await process_call_recording(update, drive_link)
            if result is None:
                return WAIT_SUMMARY
            text, gpt_analysis_text = result
        else:
            gpt_analysis_text = None

    context.user_data["summary"] = text

    if gpt_analysis_text:
        # вже проаналізовано через gpt_analyze_call (повний дзвінок)
        context.user_data["gpt_analysis"] = gpt_analysis_text
        await update.message.reply_text(
            f"📊 Витягнув ключові деталі з запису:\n\n{gpt_analysis_text}\n\n"
            f"Якщо щось не так — просто продовжуємо далі, це збережеться в картці."
        )
    else:
        await update.message.reply_text("🤖 Аналізую розмову...")
        analysis = gpt_analyze_summary(text)
        if analysis.get("formatted"):
            context.user_data["gpt_analysis"] = analysis["formatted"]
            await update.message.reply_text(
                f"📊 Витягнув ключові деталі:\n\n{analysis['formatted']}\n\n"
                f"Якщо щось не так — просто продовжуємо далі, це збережеться в картці."
            )
        else:
            context.user_data["gpt_analysis"] = ""

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
    gpt_analysis = context.user_data.get("gpt_analysis", "")

    start_date = None
    due_iso = None

    if text.lower() not in ["пропустити", "⏭ пропустити", "skip"]:
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

    comment_text = f"📋 Summary:\n{summary}"
    if gpt_analysis:
        comment_text += f"\n\n{gpt_analysis}"
    add_comment(card["id"], comment_text)

    chat_id = update.effective_chat.id

    remind_time = datetime.now() + timedelta(days=3)
    scheduler.add_job(
        send_reminder,
        "date",
        run_date=remind_time,
        args=[context.application, chat_id, f"⚠️ Лід {name} — 3 дні без відповіді. Час нагадати про себе!"],
        id=f"followup_{card['id']}",
        replace_existing=True
    )

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
        f"✅ Картку {name} створено!{reminders_text}\n\nДодаткове нагадування?",
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
        f"✅ Коментар додано до *{name}*\n\nПоставити нагадування по цьому клієнту?",
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
# СПИСОК АКТИВНИХ КЛІЄНТІВ / DEBUG
# ───────────────────────────────────────────────

async def debug_lists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/lists", params=trello_params())
    lists = r.json()
    text = "🔍 *Колонки на дошці:*\n\n"
    for lst in lists:
        matched = ""
        for col, col_id in COLUMNS.items():
            if col_id == lst["id"]:
                matched = f" ✅ `{col}`"
        text += f"• `{lst['name']}` {matched}\n  `{lst['id']}`\n\n"
    r2 = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/cards", params=trello_params())
    cards = r2.json()
    text += f"*Всього карток на дошці: {len(cards)}*"
    await update.message.reply_text(text, parse_mode="Markdown")

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
# /stats — AI-АНАЛІТИКА ПО ПРОЕКТАХ
# ───────────────────────────────────────────────

STATS_QUICK = [
    ["📊 Скільки на перемовинах?", "🔥 Хто в роботі?"],
    ["💤 Хто на паузі?", "📈 Загальний огляд"],
    ["✍️ Свій запит"],
]

async def stats_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Аналітика проектів*\n\nОбери запит або напиши свій:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(STATS_QUICK, one_time_keyboard=True, resize_keyboard=True)
    )
    return WAIT_STATS_QUERY

async def stats_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "перемовин" in text.lower():
        query = "Скільки проектів зараз на етапі перемовин? Перелічи їх."
    elif "роботі" in text.lower() or "роботи" in text.lower():
        query = "Покажи всіх клієнтів які зараз в роботі з датами старту якщо є."
    elif "паузі" in text.lower() or "пауз" in text.lower():
        query = "Які проекти зараз на паузі і як давно вони там?"
    elif "загальний" in text.lower() or "огляд" in text.lower():
        query = "Зроби загальний огляд всіх проектів по статусах. Скільки в кожній колонці, на що звернути увагу."
    elif "свій" in text.lower() or "запит" in text.lower():
        await update.message.reply_text(
            "Пиши запит — наприклад:\n"
            "• «які ліди без відповіді вже тиждень?»\n"
            "• «скільки грошей потенційно на перемовинах?»\n"
            "• «хто може закритись цього місяця?»",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAIT_STATS_QUERY
    else:
        query = text

    await update.message.reply_text("🤖 Аналізую...", reply_markup=ReplyKeyboardRemove())

    cards = get_all_cards_with_lists()
    answer = gpt_stats_answer(query, cards)

    await update.message.reply_text(answer)
    return ConversationHandler.END

# ───────────────────────────────────────────────
# ГОЛОСОВІ ПОВІДОМЛЕННЯ
# ───────────────────────────────────────────────

async def transcribe_voice(update: Update) -> str | None:
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
        os.remove(tmp_path)
        return transcript.text
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Помилка розпізнавання")
        return None

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        os.remove(tmp_path)

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
    r = requests.get(f"{TRELLO_API}/boards/{TRELLO_BOARD_ID}/cards", params=trello_params())
    cards = r.json()

    work_list_id = COLUMNS.get("в роботі")
    if not work_list_id:
        await update.message.reply_text("❌ Не знайшов колонку 'В роботі'")
        return ConversationHandler.END

    active_cards = [c for c in cards if c.get("idList") == work_list_id]

    if not active_cards:
        await update.message.reply_text("Немає активних клієнтів.")
        return ConversationHandler.END

    context.user_data["setdate_cards"] = {c["name"]: c["id"] for c in active_cards}

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

        payment_date = start_date + timedelta(days=30)
        scheduler.add_job(
            send_reminder,
            "date",
            run_date=payment_date,
            args=[context.application, chat_id, f"💰 {name} — час виставити рахунок за наступний місяць!"],
            id=f"payment_{card_id}",
            replace_existing=True
        )

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
# РОЗПІЗНАВАННЯ ТЕКСТУ (без команд) + AI-зріз по клієнту
# ───────────────────────────────────────────────

async def client_info(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    card = find_card(name)
    if not card:
        await update.message.reply_text(f"❌ Не знайшов клієнта «{name}»")
        return

    await update.message.reply_text(f"🔍 Знайшов *{card['name']}*, аналізую...", parse_mode="Markdown")

    comments = get_card_comments(card["id"])
    answer = gpt_client_answer(card, comments)

    await update.message.reply_text(answer)


# ───────────────────────────────────────────────
# TODOIST — ПЛАНУВАННЯ ЗАДАЧ ГОЛОСОМ
# ───────────────────────────────────────────────

def todoist_create_task(title: str, due_date: str = None, due_time: str = None,
                         priority: int = 4, description: str = "") -> dict:
    if not TODOIST_TOKEN:
        return None

    headers = {
        "Authorization": f"Bearer {TODOIST_TOKEN}",
        "Content-Type": "application/json"
    }

    data = {
        "content": title,
        "priority": priority,
    }

    if description:
        data["description"] = description

    if due_date:
        if due_time:
            data["due_datetime"] = f"{due_date}T{due_time}:00"
        else:
            data["due_date"] = due_date

    r = requests.post(f"{TODOIST_API}/tasks", json=data, headers=headers)
    logger.info(f"Todoist response: {r.status_code} | {r.text[:300]}")
    if r.status_code in (200, 201, 204):
        try:
            return r.json()
        except Exception:
            return {"id": "ok"}
    else:
        logger.error(f"Todoist error: {r.status_code} {r.text}")
        return None

async def task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎙 Надиктуй або напиши задачу.\n\n"
        "Наприклад:\n"
        "• Зателефонувати Валерію завтра о 15:00\n"
        "• Терміново підготувати КП до п'ятниці\n"
        "• Оплатити рахунок до 25 червня"
    )
    return WAIT_TASK_VOICE

async def task_got_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        text = await transcribe_voice(update)
        if not text:
            return WAIT_TASK_VOICE
    else:
        text = update.message.text.strip()

    await update.message.reply_text("🤖 Аналізую задачу...")

    parsed = gpt_parse_task(text)

    title = parsed.get("title", text)
    due_date = parsed.get("due_date")
    due_time = parsed.get("due_time")
    priority = parsed.get("priority", 4)
    description = parsed.get("description", "")

    task = todoist_create_task(title, due_date, due_time, priority, description)

    if task:
        priority_labels = {1: "🔴 Терміново", 2: "🟠 Високий", 3: "🟡 Середній", 4: "⚪ Звичайний"}
        date_str = ""
        if due_date:
            try:
                d = datetime.strptime(due_date, "%Y-%m-%d")
                date_str = d.strftime("%d.%m.%Y")
                if due_time:
                    date_str += f" о {due_time}"
            except:
                date_str = due_date

        msg = "✅ Задачу додано в Todoist!\n\n"
        msg += f"📌 *{title}*\n"
        if date_str:
            msg += f"📅 Дедлайн: {date_str}\n"
        msg += priority_labels.get(priority, "⚪ Звичайний")
        if description:
            msg += f"\n💬 {description}"

        if due_date and due_time:
            try:
                remind_dt = datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M")
                remind_dt = remind_dt - timedelta(minutes=30)
                chat_id = update.effective_chat.id
                scheduler.add_job(
                    send_reminder, "date",
                    run_date=remind_dt,
                    args=[context.application, chat_id, f"⏰ Нагадування: {title}"],
                    id=f"todoist_remind_{task['id']}",
                    replace_existing=True
                )
                msg += "\n\n🔔 Нагадаю за 30 хв до дедлайну"
            except Exception as e:
                logger.error(f"Reminder error: {e}")

        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Не вдалось створити задачу в Todoist. Перевір токен.")

    return ConversationHandler.END

async def smart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    text_lower = text.lower()

    client_patterns = [
        r"що по (.+?)[\?\!\.]*$",
        r"розкажи про (.+?)[\?\!\.]*$",
        r"інфо по (.+?)[\?\!\.]*$",
        r"як там (.+?)[\?\!\.]*$",
        r"статус (.+?)[\?\!\.]*$",
    ]
    for pattern in client_patterns:
        match = re.search(pattern, text_lower)
        if match:
            client_name = match.group(1).strip()
            return await client_info(update, context, client_name)

    if any(w in text_lower for w in ["новий лід", "новий клієнт", "нова заявка"]):
        return await new_lead_start(update, context)
    elif any(w in text_lower for w in ["коментар", "нотатка", "додай"]):
        return await comment_start(update, context)
    elif any(w in text_lower for w in ["перемісти", "змін статус"]):
        return await move_start(update, context)
    elif any(w in text_lower for w in ["нагадай", "нагадування", "нагади"]):
        return await remind_start(update, context)
    elif any(w in text_lower for w in ["статистика", "аналіз", "скільки", "перемовини", "проекти"]):
        return await stats_start(update, context)
    elif any(w in text_lower for w in ["задач", "план", "зустріч", "нагадай в todoist", "додай в todoist"]):
        return await task_start(update, context)
    elif any(w in text_lower for w in ["дзвінок", "зідзвон", "запис розмови"]):
        return await call_summary_start(update, context)
    else:
        await update.message.reply_text(
            "Не зрозумів 🤔 Спробуй:\n"
            "• /new_lead — новий клієнт\n"
            "• /comment — коментар\n"
            "• /move — змінити статус\n"
            "• /remind — нагадування\n"
            "• /clients — активні клієнти\n"
            "• /stats — аналітика\n"
            "• /call_summary — запис дзвінка\n\n"
            "Або питай про клієнта: _«що по Валерію?»_",
            parse_mode="Markdown"
        )

# ───────────────────────────────────────────────
# НАГАДУВАННЯ (функція)
# ───────────────────────────────────────────────

async def send_reminder(app, chat_id, text):
    await app.bot.send_message(chat_id=chat_id, text=text)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ═════════════════════════════════════════════════════════════
# ОБРОБКА ЗАПИСІВ ДЗВІНКІВ (Google Drive → транскрипція → GPT)
# ═════════════════════════════════════════════════════════════

DRIVE_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:drive|docs)\.google\.com/\S+"
)

MAX_WHISPER_CHUNK_BYTES = 24 * 1024 * 1024  # трохи менше за ліміт 25MB Whisper

def extract_drive_link(text: str) -> str | None:
    """Шукає Google Drive посилання в тексті. Повертає None якщо не знайдено."""
    match = DRIVE_LINK_PATTERN.search(text)
    if match:
        return match.group(0)
    return None

def extract_drive_file_id(url: str) -> str | None:
    """
    Витягує file_id з різних форматів Google Drive посилань:
    - https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    - https://drive.google.com/open?id=FILE_ID
    - https://docs.google.com/.../d/FILE_ID/...
    """
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def download_drive_file(url: str, dest_path: str) -> bool:
    """
    Качає файл з Google Drive за публічним посиланням ("у кого є посилання").
    Обробляє підтвердження для великих файлів (Drive показує сторінку-застереження
    "файл завеликий для перевірки на віруси" і вимагає підтвердження).
    """
    file_id = extract_drive_file_id(url)
    if not file_id:
        return False

    session = requests.Session()
    base_url = "https://drive.google.com/uc?export=download"

    response = session.get(base_url, params={"id": file_id}, stream=True)

    confirm_token = None
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            confirm_token = value
            break

    if confirm_token:
        response = session.get(
            base_url,
            params={"id": file_id, "confirm": confirm_token},
            stream=True
        )

    if response.headers.get("Content-Type", "").startswith("text/html"):
        m = re.search(r'confirm=([0-9A-Za-z_]+)', response.text)
        if m:
            response = session.get(
                base_url,
                params={"id": file_id, "confirm": m.group(1)},
                stream=True
            )

    if response.status_code != 200:
        logger.error(f"Drive download failed: {response.status_code}")
        return False

    content_type = response.headers.get("Content-Type", "unknown")
    logger.info(f"Drive download: Content-Type={content_type}")

    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    downloaded_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
    logger.info(f"Drive download: saved {downloaded_size} bytes to {dest_path}")

    # Якщо файл підозріло малий і Content-Type текстовий — це майже напевно
    # HTML-сторінка підтвердження Google, а не реальний файл
    if downloaded_size < 100_000 and "text" in content_type.lower():
        with open(dest_path, "r", errors="ignore") as f:
            preview = f.read(300)
        logger.error(f"Drive download looks like HTML, not a media file. Preview: {preview}")
        return False

    return True


def convert_to_audio(input_path: str, output_path: str) -> bool:
    """Конвертує відео/будь-який медіафайл у компактний аудіофайл (mp3) через ffmpeg."""
    import subprocess
    try:
        in_size = os.path.getsize(input_path) if os.path.exists(input_path) else -1
        logger.info(f"convert_to_audio: input={input_path} size={in_size} bytes")

        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-vn",
                "-acodec", "libmp3lame",
                "-ar", "16000",
                "-ac", "1",
                "-b:a", "64k",
                output_path
            ],
            capture_output=True,
            timeout=1800,
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode(errors="ignore")
            # Банер версії ffmpeg займає перші рядки — нам потрібен ХВІСТ, де реальна причина
            logger.error(f"ffmpeg returncode={result.returncode}")
            logger.error(f"ffmpeg stderr (last 1500 chars): {stderr_text[-1500:]}")
            return False

        out_size = os.path.getsize(output_path) if os.path.exists(output_path) else -1
        logger.info(f"convert_to_audio: output={output_path} size={out_size} bytes")
        return True
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        return False


def split_audio_into_chunks(audio_path: str, max_bytes: int = MAX_WHISPER_CHUNK_BYTES) -> list:
    """Ріже аудіофайл на шматки по тривалості, орієнтуючись на максимальний розмір у байтах."""
    from pydub import AudioSegment

    audio = AudioSegment.from_file(audio_path)
    total_bytes = os.path.getsize(audio_path)
    duration_ms = len(audio)

    if total_bytes <= max_bytes:
        return [audio_path]

    num_chunks = (total_bytes // max_bytes) + 1
    chunk_duration_ms = duration_ms // num_chunks

    chunk_paths = []
    for i in range(num_chunks):
        start = i * chunk_duration_ms
        end = min((i + 1) * chunk_duration_ms, duration_ms)
        chunk = audio[start:end]
        chunk_path = f"{audio_path}_chunk{i}.mp3"
        chunk.export(chunk_path, format="mp3", bitrate="64k")
        chunk_paths.append(chunk_path)

    return chunk_paths


def transcribe_long_audio(audio_path: str) -> str:
    """Транскрибує аудіофайл будь-якого розміру: ріже на чанки якщо треба, склеює текст."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    chunk_paths = split_audio_into_chunks(audio_path)
    full_text = []

    for chunk_path in chunk_paths:
        with open(chunk_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=f, language="uk"
            )
        full_text.append(transcript.text)
        if chunk_path != audio_path:
            try:
                os.remove(chunk_path)
            except OSError:
                pass

    return "\n".join(full_text)


async def process_call_recording(update: Update, drive_link: str):
    """
    Повний цикл: скачує файл з Drive → конвертує в аудіо → транскрибує → GPT-аналіз.
    Повертає (transcript_text, formatted_analysis) або None при помилці.
    Шле проміжні статуси користувачу.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_path = os.path.join(tmp_dir, "raw_download")
        audio_path = os.path.join(tmp_dir, "audio.mp3")

        await update.message.reply_text("📥 Качаю запис з Google Drive…")

        ok = download_drive_file(drive_link, raw_path)
        if not ok or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            await update.message.reply_text(
                "❌ Не вдалось скачати файл з Drive.\n"
                "Перевір, що посилання відкрите за принципом «у кого є посилання — може переглянути», "
                "і що це пряме посилання на файл (не на папку)."
            )
            return None

        await update.message.reply_text("🎬 Конвертую запис в аудіо…")
        ok = convert_to_audio(raw_path, audio_path)
        if not ok:
            await update.message.reply_text("❌ Не вдалось конвертувати файл. Перевір формат запису.")
            return None

        await update.message.reply_text("🎙 Транскрибую розмову (це може зайняти кілька хвилин)…")
        try:
            transcript_text = transcribe_long_audio(audio_path)
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            await update.message.reply_text("❌ Помилка під час транскрипції.")
            return None

        if not transcript_text.strip():
            await update.message.reply_text("❌ Не вдалось розпізнати текст у записі.")
            return None

        await update.message.reply_text("🤖 Аналізую розмову…")
        call_date = datetime.now().strftime("%d.%m.%Y")
        analysis = gpt_analyze_call(transcript_text, call_date)

        if not analysis:
            await update.message.reply_text("❌ Помилка при аналізі розмови.")
            return None

        return transcript_text, analysis


# ───────────────────────────────────────────────
# /call_summary — додати запис дзвінка до ІСНУЮЧОГО клієнта
# ───────────────────────────────────────────────

async def call_summary_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = ReplyKeyboardMarkup(
        [["📋 Обрати зі списку"]],
        one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        "Чий це дзвінок? Введи ім'я клієнта або обери зі списку:",
        reply_markup=keyboard
    )
    return WAIT_CALL_CLIENT

async def call_got_client_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "обрати" in text.lower() or "список" in text.lower():
        keyboard = ReplyKeyboardMarkup(
            STATUS_KEYBOARD, one_time_keyboard=True, resize_keyboard=True
        )
        await update.message.reply_text(
            "З якого статусу клієнт?",
            reply_markup=keyboard
        )
        return WAIT_CALL_STATUS_PICK

    card = find_card(text)
    if not card:
        await update.message.reply_text(
            "❌ Картку не знайдено. Перевір ім'я, або спочатку додай клієнта через /new_lead."
        )
        return ConversationHandler.END

    context.user_data["call_card_id"] = card["id"]
    context.user_data["call_card_name"] = card["name"]
    await update.message.reply_text(
        f"Знайшов *{card['name']}*.\n\nСкинь посилання на Google Drive із записом дзвінка:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAIT_CALL_LINK

async def call_got_status_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = update.message.text.strip()
    list_id = get_list_id(status)
    if not list_id:
        await update.message.reply_text("❌ Не знайшов таку колонку. Спробуй ще раз.")
        return WAIT_CALL_STATUS_PICK

    cards = get_cards_in_list(list_id)
    if not cards:
        await update.message.reply_text(
            f"У колонці «{status}» немає карток. Обери інший статус.",
        )
        return WAIT_CALL_STATUS_PICK

    context.user_data["call_status_cards"] = cards
    names = list(cards.keys())
    keyboard = [names[i:i+2] for i in range(0, len(names), 2)]
    keyboard.append(["❌ Скасувати"])

    await update.message.reply_text(
        "Обери клієнта:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return WAIT_CALL_CLIENT_PICK

async def call_got_client_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Скасувати":
        await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    cards = context.user_data.get("call_status_cards", {})
    card_id = cards.get(text)

    if not card_id:
        await update.message.reply_text("❌ Не знайшов такого клієнта в списку. Спробуй ще раз.")
        return WAIT_CALL_CLIENT_PICK

    context.user_data["call_card_id"] = card_id
    context.user_data["call_card_name"] = text
    await update.message.reply_text(
        f"Обрав *{text}*.\n\nСкинь посилання на Google Drive із записом дзвінка:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAIT_CALL_LINK

async def call_got_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    drive_link = extract_drive_link(text)

    if not drive_link:
        await update.message.reply_text(
            "❌ Не бачу посилання на Google Drive у повідомленні. Скинь лінк ще раз."
        )
        return WAIT_CALL_LINK

    card_id = context.user_data.get("call_card_id")
    card_name = context.user_data.get("call_card_name", "")

    result = await process_call_recording(update, drive_link)
    if result is None:
        return ConversationHandler.END

    transcript_text, analysis = result

    add_comment(card_id, analysis)

    await update.message.reply_text(
        f"✅ Додав запис дзвінка в картку *{card_name}*\n\n{analysis}",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


# ───────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────

def main():
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

    stats_conv = ConversationHandler(
        entry_points=[CommandHandler("stats", stats_start)],
        states={
            WAIT_STATS_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, stats_got_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    task_conv = ConversationHandler(
        entry_points=[CommandHandler("task", task_start)],
        states={
            WAIT_TASK_VOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_got_input),
                MessageHandler(filters.VOICE, task_got_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    call_summary_conv = ConversationHandler(
        entry_points=[CommandHandler("call_summary", call_summary_start)],
        states={
            WAIT_CALL_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_got_client_text)],
            WAIT_CALL_STATUS_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_got_status_pick)],
            WAIT_CALL_CLIENT_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_got_client_pick)],
            WAIT_CALL_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_got_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clients", list_clients))
    app.add_handler(CommandHandler("debug", debug_lists))
    app.add_handler(task_conv)
    app.add_handler(call_summary_conv)
    app.add_handler(stats_conv)
    app.add_handler(new_lead_conv)
    app.add_handler(setdate_conv)
    app.add_handler(comment_conv)
    app.add_handler(move_conv)
    app.add_handler(remind_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    scheduler.start()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
