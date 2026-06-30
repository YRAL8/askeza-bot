# Бот-трекер аскез

Telegram-бот для целей на N дней. Пропуск дня → сброс серии (кроме ⏸ паузы).

## Установка

```bash
cd ~/projects/askeza-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env_template .env
nano .env   # TELEGRAM_BOT_TOKEN
```

## Запуск

```bash
source venv/bin/activate
python3 askeza_bot.py
```

## Возможности

- ➕ Новая цель (Enter = 50 дней)
- ✅ Ежедневная отметка
- ⏸ Пауза 1×/нед — день без отметки
- ⚠️ Уведомление при сбросе серии
- 🔔 Напоминание в 21:00 (твой часовой пояс)
- 🕐 Часовой пояс (Москва, Киев, UTC…)
- 📅 Вчера — сколько отметок
- 📦 Архив завершённых целей
- 💾 Бэкап `askeza.db`
- 🌱🔥💪 Мотивация на 25/50/75%
- /help — справка

## Настройки в `.env`

```
TELEGRAM_BOT_TOKEN=...
DEFAULT_TIMEZONE=Europe/Moscow
REMINDER_HOUR=21
```

## База

`askeza.db` — не удаляй. Бэкап через кнопку 💾.
