# NEMESH CRM Bot 🤖

Telegram-бот для управління клієнтами в Trello.

## Що вміє

- ✅ Створювати картки нових клієнтів (ім'я, summary, контакти, сума, дата старту)
- ✅ Переміщати клієнтів між статусами
- ✅ Додавати коментарі до карток
- ✅ Нагадування: follow-up через 3 дні, чекін через 2 тижні, оплата через місяць
- ✅ Довільні нагадування ("нагадай через 2 тижні")
- ✅ Список активних клієнтів

## Команди

| Команда | Дія |
|---------|-----|
| /new_lead | Новий клієнт |
| /comment | Додати коментар |
| /move | Змінити статус |
| /remind | Нагадування |
| /clients | Активні клієнти |
| /cancel | Скасувати дію |

## Деплой на Railway

### 1. Завантаж код на GitHub
1. Створи новий репозиторій на github.com
2. Завантаж всі файли (bot.py, requirements.txt, Procfile)

### 2. Задеплой на Railway
1. Зайди на railway.app
2. "New Project" → "Deploy from GitHub repo"
3. Вибери свій репозиторій

### 3. Додай змінні середовища
В Railway → Variables додай:
```
TELEGRAM_TOKEN=твій_токен
TRELLO_KEY=твій_ключ
TRELLO_TOKEN=твій_трелло_токен
TRELLO_BOARD_ID=X9M3JzKk
OWNER_CHAT_ID=твій_telegram_id
```

### Як дізнатись свій OWNER_CHAT_ID?
Напиши боту @userinfobot в Telegram — він скаже твій ID.

## Локальний запуск (для тесту)
```bash
pip install -r requirements.txt
python bot.py
```
