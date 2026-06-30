import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────
# НАСТРОЙКИ
# ──────────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "askeza.db")
DEFAULT_TARGET_DAYS = 50
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Europe/Moscow")
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "21"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

NAME, DAYS = range(2)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Мои цели", "✅ Отметиться сегодня"],
        ["➕ Новая цель", "⏸ Пауза на день"],
        ["📈 Статистика", "📅 Вчера"],
        ["🗑 Удалить цель", "📦 Архив"],
        ["💾 Бэкап", "🕐 Часовой пояс"],
    ],
    resize_keyboard=True,
)

TIMEZONE_CHOICES = [
    ("🕐 Москва (UTC+3)", "Europe/Moscow"),
    ("🕐 Киев (UTC+2)", "Europe/Kyiv"),
    ("🕐 Минск (UTC+3)", "Europe/Minsk"),
    ("🕐 UTC", "UTC"),
]

MILESTONES = (25, 50, 75)  # проценты для milestone_message

# ──────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ
# ──────────────────────────────────────────────────────────────────────────


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, column: str, definition: str):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            target_days INTEGER NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0,
            last_checkin_date TEXT,
            start_date TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL,
            pause_used_week TEXT,
            last_reminder_date TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pauses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            goal_id INTEGER NOT NULL,
            pause_date TEXT NOT NULL,
            UNIQUE(goal_id, pause_date)
        )
        """
    )
    _ensure_column(conn, "goals", "archived", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (user_id, timezone, pause_used_week, last_reminder_date) VALUES (?, ?, NULL, NULL)",
            (user_id, DEFAULT_TIMEZONE),
        )
        conn.commit()
    conn.close()


def get_user_timezone(user_id: int) -> ZoneInfo:
    ensure_user(user_id)
    conn = get_conn()
    row = conn.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    tz_name = row["timezone"] if row else DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def set_user_timezone(user_id: int, tz_name: str):
    ensure_user(user_id)
    conn = get_conn()
    conn.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (tz_name, user_id))
    conn.commit()
    conn.close()


def user_today(user_id: int) -> date:
    return datetime.now(get_user_timezone(user_id)).date()


def iso_week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def pause_used_this_week(user_id: int, today: date) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT pause_used_week FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row and row["pause_used_week"] == iso_week_key(today)


def get_pause_dates_for_goal(goal_id: int) -> set[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT pause_date FROM pauses WHERE goal_id = ?", (goal_id,)
    ).fetchall()
    conn.close()
    return {r["pause_date"] for r in rows}


def add_pause(user_id: int, goal_id: int, pause_date: date) -> tuple[bool, str]:
    week = iso_week_key(pause_date)
    conn = get_conn()
    row = conn.execute(
        "SELECT pause_used_week FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row and row["pause_used_week"] == week:
        conn.close()
        return False, "Паузу на эту неделю ты уже использовал (1 раз в неделю)."

    try:
        conn.execute(
            "INSERT INTO pauses (user_id, goal_id, pause_date) VALUES (?, ?, ?)",
            (user_id, goal_id, pause_date.isoformat()),
        )
        conn.execute(
            "UPDATE users SET pause_used_week = ? WHERE user_id = ?",
            (week, user_id),
        )
        conn.commit()
        return True, "OK"
    except sqlite3.IntegrityError:
        return False, "На эту дату пауза для этой цели уже стоит."
    finally:
        conn.close()


def add_goal(user_id: int, name: str, target_days: int):
    ensure_user(user_id)
    today = user_today(user_id).isoformat()
    conn = get_conn()
    conn.execute(
        """INSERT INTO goals (user_id, name, target_days, streak, last_checkin_date,
           start_date, completed, archived, created_at)
           VALUES (?, ?, ?, 0, NULL, ?, 0, 0, ?)""",
        (user_id, name, target_days, today, today),
    )
    conn.commit()
    conn.close()


def get_goal_raw(goal_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_goal_for_user(goal_id: int, user_id: int) -> dict | None:
    goal = get_goal_raw(goal_id)
    if goal is None or goal["user_id"] != user_id:
        return None
    return reset_if_missed(goal, user_id)


def get_goals(user_id: int, include_archived: bool = False) -> tuple[list[dict], list[dict]]:
    """Возвращает (цели, события_сброса)."""
    conn = get_conn()
    if include_archived:
        rows = conn.execute(
            "SELECT * FROM goals WHERE user_id = ? AND archived = 1 ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM goals WHERE user_id = ? AND archived = 0
               ORDER BY completed ASC, id ASC""",
            (user_id,),
        ).fetchall()
    conn.close()

    goals = []
    resets = []
    for row in rows:
        goal, reset_info = reset_if_missed(dict(row), user_id, notify=True)
        goals.append(goal)
        if reset_info:
            resets.append(reset_info)
    return goals, resets


def delete_goal_for_user(goal_id: int, user_id: int) -> bool:
    goal = get_goal_raw(goal_id)
    if goal is None or goal["user_id"] != user_id:
        return False
    conn = get_conn()
    conn.execute("DELETE FROM pauses WHERE goal_id = ?", (goal_id,))
    conn.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
    conn.commit()
    conn.close()
    return True


def archive_goal(goal_id: int, user_id: int) -> bool:
    goal = get_goal_raw(goal_id)
    if goal is None or goal["user_id"] != user_id or not goal["completed"]:
        return False
    conn = get_conn()
    conn.execute(
        "UPDATE goals SET archived = 1 WHERE id = ? AND user_id = ?",
        (goal_id, user_id),
    )
    conn.commit()
    conn.close()
    return True


def reset_if_missed(goal: dict, user_id: int, notify: bool = False) -> tuple[dict, dict | None]:
    """
    Сброс если пропущен день без паузы.
    Возвращает (goal, reset_info|None) для уведомления.
    """
    if goal["completed"] or goal["archived"]:
        return goal, None
    if goal["last_checkin_date"] is None:
        return goal, None

    today = user_today(user_id)
    last = datetime.strptime(goal["last_checkin_date"], "%Y-%m-%d").date()
    if today <= last:
        return goal, None

    pause_dates = get_pause_dates_for_goal(goal["id"])
    missed_day = None
    d = last + timedelta(days=1)
    while d < today:
        if d.isoformat() not in pause_dates:
            missed_day = d
            break
        d += timedelta(days=1)

    if missed_day is None:
        return goal, None

    old_streak = goal["streak"]
    conn = get_conn()
    conn.execute(
        """UPDATE goals SET streak = 0, last_checkin_date = NULL, start_date = ?
           WHERE id = ?""",
        (today.isoformat(), goal["id"]),
    )
    conn.commit()
    conn.close()

    goal["streak"] = 0
    goal["last_checkin_date"] = None
    goal["start_date"] = today.isoformat()

    reset_info = None
    if notify and old_streak > 0:
        reset_info = {
            "name": goal["name"],
            "old_streak": old_streak,
            "missed_day": missed_day.isoformat(),
        }
    return goal, reset_info


def checkin_goal(goal_id: int, user_id: int) -> tuple[dict | None, bool, str | None]:
    """Возвращает (goal, just_completed, milestone_text)."""
    goal = get_goal_for_user(goal_id, user_id)
    if goal is None or goal["completed"]:
        return goal, False, None

    today = user_today(user_id).isoformat()
    if goal["last_checkin_date"] == today:
        return goal, False, None

    old_streak = goal["streak"]
    new_streak = old_streak + 1
    just_completed = new_streak >= goal["target_days"]

    conn = get_conn()
    conn.execute(
        "UPDATE goals SET streak = ?, last_checkin_date = ?, completed = ? WHERE id = ?",
        (new_streak, today, 1 if just_completed else 0, goal_id),
    )
    conn.commit()
    conn.close()

    goal["streak"] = new_streak
    goal["last_checkin_date"] = today
    goal["completed"] = 1 if just_completed else 0

    milestone = milestone_message(old_streak, new_streak, goal["target_days"])
    return goal, just_completed, milestone


def milestone_message(old_streak: int, new_streak: int, target: int) -> str | None:
    if target < 4:
        return None
    for pct, emoji, label in [
        (25, "🌱", "25%"),
        (50, "🔥", "50%"),
        (75, "💪", "75%"),
    ]:
        threshold = max(1, round(target * pct / 100))
        if old_streak < threshold <= new_streak:
            return f"{emoji} «{label} пути пройдено!» День {new_streak} из {target}."
    return None


def count_checkins_on(user_id: int, day: date) -> int:
    conn = get_conn()
    n = conn.execute(
        """SELECT COUNT(*) FROM goals
           WHERE user_id = ? AND archived = 0 AND last_checkin_date = ?""",
        (user_id, day.isoformat()),
    ).fetchone()[0]
    conn.close()
    return n


def get_all_user_ids() -> list[int]:
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def mark_reminder_sent(user_id: int, day: date):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET last_reminder_date = ? WHERE user_id = ?",
        (day.isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def get_last_reminder_date(user_id: int) -> date | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT last_reminder_date FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if row and row["last_reminder_date"]:
        return datetime.strptime(row["last_reminder_date"], "%Y-%m-%d").date()
    return None


# ──────────────────────────────────────────────────────────────────────────
# ОТОБРАЖЕНИЕ
# ──────────────────────────────────────────────────────────────────────────


def progress_bar(streak: int, target: int, length: int = 10) -> str:
    filled = round(length * min(streak, target) / target)
    bar = "▓" * filled + "░" * (length - filled)
    percent = round(min(streak, target) / target * 100)
    return f"{bar} {percent}%"


def format_goal_line(goal: dict) -> str:
    if goal["completed"]:
        suffix = " (в архиве)" if goal["archived"] else ""
        return f"🏆 {goal['name']} — ЗАВЕРШЕНО ({goal['target_days']} дней){suffix}"
    day_text = f"день {goal['streak']} из {goal['target_days']}"
    return f"• {goal['name']} — {day_text}\n  {progress_bar(goal['streak'], goal['target_days'])}"


async def notify_resets(update: Update, resets: list[dict]):
    for r in resets:
        await update.message.reply_text(
            f"⚠️ <b>Сброс серии</b>\n"
            f"Цель «{r['name']}» — пропущен {r['missed_day']}.\n"
            f"Серия {r['old_streak']} → 0. Начинаем заново.",
            parse_mode="HTML",
        )


async def notify_resets_chat(context, chat_id: int, resets: list[dict]):
    for r in resets:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ <b>Сброс серии</b>\n"
                f"Цель «{r['name']}» — пропущен {r['missed_day']}.\n"
                f"Серия {r['old_streak']} → 0."
            ),
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────────────────────────────────
# ОБРАБОТЧИКИ
# ──────────────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    tz = get_user_timezone(user_id)
    await update.message.reply_text(
        "Привет! Это твой личный трекер аскез.\n\n"
        "• Отмечайся каждый день\n"
        "• Пропустил день — серия обнуляется\n"
        "• ⏸ Пауза — 1 раз в неделю, день без отметки\n"
        "• Напоминание в 21:00 по твоему часовому поясу\n\n"
        f"Часовой пояс: <b>{tz.key}</b> (🕐 Часовой пояс — сменить)\n"
        "/help — справка",
        reply_markup=MAIN_KEYBOARD,
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Команды и кнопки</b>\n\n"
        "➕ Новая цель — название + дней (Enter = 50)\n"
        "✅ Отметиться — один раз в день на цель\n"
        "⏸ Пауза — сегодня можно не отмечать (1×/нед)\n"
        "📊 Мои цели — активные цели\n"
        "📦 Архив — завершённые цели\n"
        "📅 Вчера — сколько отметок было вчера\n"
        "📈 Статистика — сводка\n"
        "💾 Бэкап — скачать askeza.db\n"
        "🕐 Часовой пояс — для «сегодня» и напоминаний\n"
        "🗑 Удалить — удалить цель\n\n"
        "/cancel — отмена при добавлении цели",
        parse_mode="HTML",
    )


async def show_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    goals, resets = get_goals(user_id)
    await notify_resets(update, resets)
    active = [g for g in goals if not g["completed"]]
    done = [g for g in goals if g["completed"] and not g["archived"]]
    if not active and not done:
        await update.message.reply_text(
            "Пока нет целей. Нажми «➕ Новая цель»."
        )
        return
    parts = []
    if active:
        parts.append("📋 <b>Активные</b>\n\n" + "\n\n".join(format_goal_line(g) for g in active))
    if done:
        parts.append("🏁 <b>Завершённые</b> (можно в 📦 Архив)\n\n" + "\n\n".join(format_goal_line(g) for g in done))
    await update.message.reply_text("\n\n".join(parts), parse_mode="HTML")


async def show_checkin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    goals, resets = get_goals(user_id)
    await notify_resets(update, resets)
    today = user_today(user_id).isoformat()
    pending = [
        g for g in goals
        if not g["completed"] and not g["archived"] and g["last_checkin_date"] != today
    ]
    if not pending:
        await update.message.reply_text(
            "Нет активных целей для отметки или всё уже отмечено сегодня 🎉"
        )
        return
    buttons = [
        [InlineKeyboardButton(f"✅ {g['name'][:40]}", callback_data=f"checkin_{g['id']}")]
        for g in pending
    ]
    await update.message.reply_text(
        "Что выполнено сегодня?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def handle_checkin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    goal_id = int(query.data.split("_")[1])

    goal_before = get_goal_for_user(goal_id, user_id)
    if goal_before is None:
        await query.edit_message_text("Цель не найдена или не твоя.")
        return
    if goal_before["last_checkin_date"] == user_today(user_id).isoformat():
        await query.edit_message_text("Уже отмечено сегодня ✅")
        return

    goal, just_completed, milestone = checkin_goal(goal_id, user_id)
    if goal is None:
        await query.edit_message_text("Не удалось отметить.")
        return

    if just_completed:
        text = (
            f"🏆 Поздравляю! «{goal['name']}» — все {goal['target_days']} дней!\n"
            f"Можешь отправить в 📦 Архив."
        )
    else:
        text = (
            f"✅ {goal['name']}\n"
            f"День {goal['streak']} из {goal['target_days']}\n"
            f"{progress_bar(goal['streak'], goal['target_days'])}"
        )
        if milestone:
            text += f"\n\n{milestone}"
    await query.edit_message_text(text)


async def show_pause_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = user_today(user_id)
    if pause_used_this_week(user_id, today):
        await update.message.reply_text(
            "⏸ Паузу на эту неделю ты уже использовал.\nСледующая — с понедельника (новая ISO-неделя)."
        )
        return

    goals, resets = get_goals(user_id)
    await notify_resets(update, resets)
    active = [g for g in goals if not g["completed"] and not g["archived"]]
    if not active:
        await update.message.reply_text("Нет активных целей для паузы.")
        return

    buttons = [
        [InlineKeyboardButton(g["name"][:40], callback_data=f"pause_{g['id']}")]
        for g in active
    ]
    await update.message.reply_text(
        f"⏸ Пауза на <b>{today.isoformat()}</b> — сегодня можно не отмечать.\n"
        "Выбери цель (1 раз в неделю):",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def handle_pause_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    goal_id = int(query.data.split("_")[1])
    goal = get_goal_for_user(goal_id, user_id)
    if goal is None:
        await query.edit_message_text("Цель не найдена.")
        return

    today = user_today(user_id)
    ok, msg = add_pause(user_id, goal_id, today)
    if ok:
        await query.edit_message_text(
            f"⏸ Пауза на сегодня для «{goal['name']}».\n"
            "Отметка сегодня не обязательна — серия не сбросится."
        )
    else:
        await query.edit_message_text(f"⏸ {msg}")


async def show_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    yesterday = user_today(user_id) - timedelta(days=1)
    goals, _ = get_goals(user_id)
    active_count = len([g for g in goals if not g["completed"] and not g["archived"]])
    done = count_checkins_on(user_id, yesterday)
    await update.message.reply_text(
        f"📅 <b>Вчера ({yesterday.isoformat()})</b>\n\n"
        f"Отмечено целей: {done} из {active_count}",
        parse_mode="HTML",
    )


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    goals, resets = get_goals(user_id)
    await notify_resets(update, resets)
    if not goals:
        await update.message.reply_text("Пока нет данных.")
        return

    active = [g for g in goals if not g["completed"] and not g["archived"]]
    completed = [g for g in goals if g["completed"]]
    archived = len([g for g in goals if g["archived"]])
    longest = max((g["streak"] for g in active), default=0)
    tz = get_user_timezone(user_id)

    await update.message.reply_text(
        f"📈 <b>Статистика</b>\n\n"
        f"Активных: {len(active)}\n"
        f"Завершённых: {len(completed)}\n"
        f"В архиве: {archived}\n"
        f"Лучшая серия: {longest} дн.\n"
        f"Часовой пояс: {tz.key}",
        parse_mode="HTML",
    )


async def show_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    active_goals, _ = get_goals(user_id)
    archived_goals, _ = get_goals(user_id, include_archived=True)
    archived = [g for g in archived_goals if g["archived"]]
    completed_visible = [g for g in active_goals if g["completed"] and not g["archived"]]

    if completed_visible:
        buttons = [
            [InlineKeyboardButton(f"📦 {g['name'][:35]}", callback_data=f"arch_{g['id']}")]
            for g in completed_visible
        ]
        await update.message.reply_text(
            "Завершённые — отправить в архив?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    if archived:
        text = "📦 <b>Архив</b>\n\n" + "\n".join(format_goal_line(g) for g in archived)
        await update.message.reply_text(text, parse_mode="HTML")
    elif not completed_visible:
        await update.message.reply_text("Архив пуст. Завершённые цели можно сюда отправить.")


async def handle_archive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    goal_id = int(query.data.split("_")[1])
    if archive_goal(goal_id, user_id):
        goal = get_goal_raw(goal_id)
        await query.edit_message_text(f"📦 «{goal['name']}» — в архиве.")
    else:
        await query.edit_message_text("Не удалось архивировать.")


async def send_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.isfile(DB_PATH):
        await update.message.reply_text("База данных не найдена.")
        return
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        shutil.copy2(DB_PATH, tmp.name)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"askeza_backup_{user_today(update.effective_user.id).isoformat()}.db",
                caption="💾 Бэкап базы. Храни в безопасном месте.",
            )
    finally:
        os.unlink(tmp_path)


async def timezone_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tz = get_user_timezone(user_id)
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"tz_{name}")]
        for label, name in TIMEZONE_CHOICES
    ]
    await update.message.reply_text(
        f"Текущий: <b>{tz.key}</b>\nВыбери часовой пояс:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def handle_tz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tz_name = query.data.removeprefix("tz_")
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        await query.edit_message_text("Неизвестный часовой пояс.")
        return
    set_user_timezone(query.from_user.id, tz_name)
    await query.edit_message_text(f"🕐 Часовой пояс: {tz_name}")


# ── Новая цель ──


async def new_goal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как назвать цель? Например: «Отжиматься 50 раз»."
    )
    return NAME


async def new_goal_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_goal_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"На сколько дней? Число 1–1000 или Enter / «{DEFAULT_TARGET_DAYS}» по умолчанию."
    )
    return DAYS


async def new_goal_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "" or text == str(DEFAULT_TARGET_DAYS):
        days = DEFAULT_TARGET_DAYS
    else:
        try:
            days = int(text)
            if days <= 0 or days > 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Число 1–1000 или пусто для 50.")
            return DAYS

    name = context.user_data.pop("new_goal_name")
    add_goal(update.effective_user.id, name, days)
    await update.message.reply_text(
        f"✅ «{name}» — {days} дней. Удачи! 💪",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def new_goal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_goal_name", None)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ── Удаление ──


async def delete_goal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    goals, resets = get_goals(user_id)
    await notify_resets(update, resets)
    if not goals:
        await update.message.reply_text("Удалять нечего.")
        return
    buttons = [
        [InlineKeyboardButton(g["name"][:40], callback_data=f"del_{g['id']}")]
        for g in goals if not g["archived"]
    ]
    await update.message.reply_text(
        "Какую цель удалить?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def delete_goal_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    goal_id = int(query.data.split("_")[1])
    goal = get_goal_for_user(goal_id, user_id)
    if goal is None:
        await query.edit_message_text("Цель не найдена.")
        return
    buttons = [
        [
            InlineKeyboardButton("Да, удалить", callback_data=f"delconfirm_{goal_id}"),
            InlineKeyboardButton("Отмена", callback_data="delcancel"),
        ]
    ]
    await query.edit_message_text(
        f"Удалить «{goal['name']}»? Прогресс будет потерян.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def delete_goal_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    goal_id = int(query.data.split("_")[1])
    goal = get_goal_for_user(goal_id, user_id)
    if goal is None:
        await query.edit_message_text("Цель не найдена или не твоя.")
        return
    delete_goal_for_user(goal_id, user_id)
    await query.edit_message_text(f"🗑 Удалено: {goal['name']}")


async def delete_goal_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Отменено.")


# ── Напоминания ──


async def evening_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(ZoneInfo("UTC"))
    for user_id in get_all_user_ids():
        tz = get_user_timezone(user_id)
        local = now_utc.astimezone(tz)
        if local.hour != REMINDER_HOUR or local.minute != 0:
            continue

        today = local.date()
        if get_last_reminder_date(user_id) == today:
            continue

        goals, resets = get_goals(user_id)
        if resets:
            await notify_resets_chat(context, user_id, resets)

        today_s = today.isoformat()
        pending = [
            g["name"]
            for g in goals
            if not g["completed"] and not g["archived"] and g["last_checkin_date"] != today_s
        ]
        if not pending:
            mark_reminder_sent(user_id, today)
            continue

        lines = "\n".join(f"• {n}" for n in pending)
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🔔 <b>Напоминание ({REMINDER_HOUR}:00)</b>\n\n"
                f"Сегодня ещё не отмечено:\n{lines}\n\n"
                "Нажми ✅ Отметиться сегодня"
            ),
            parse_mode="HTML",
        )
        mark_reminder_sent(user_id, today)


# ── Запуск ──


def main():
    if TOKEN in ("", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН", "YOUR_BOT_TOKEN"):
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN в .env или export")

    init_db()
    app = Application.builder().token(TOKEN).build()

    if app.job_queue:
        app.job_queue.run_repeating(evening_reminder_job, interval=60, first=10)

    new_goal_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Новая цель$"), new_goal_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_goal_name)],
            DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_goal_days)],
        },
        fallbacks=[CommandHandler("cancel", new_goal_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(new_goal_conv)
    app.add_handler(MessageHandler(filters.Regex("^📊 Мои цели$"), show_goals))
    app.add_handler(MessageHandler(filters.Regex("^✅ Отметиться сегодня$"), show_checkin_menu))
    app.add_handler(MessageHandler(filters.Regex("^⏸ Пауза на день$"), show_pause_menu))
    app.add_handler(MessageHandler(filters.Regex("^📈 Статистика$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex("^📅 Вчера$"), show_yesterday))
    app.add_handler(MessageHandler(filters.Regex("^📦 Архив$"), show_archive))
    app.add_handler(MessageHandler(filters.Regex("^💾 Бэкап$"), send_backup))
    app.add_handler(MessageHandler(filters.Regex("^🕐 Часовой пояс$"), timezone_menu))
    app.add_handler(MessageHandler(filters.Regex("^🗑 Удалить цель$"), delete_goal_menu))

    app.add_handler(CallbackQueryHandler(handle_checkin_callback, pattern=r"^checkin_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_pause_callback, pattern=r"^pause_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_archive_callback, pattern=r"^arch_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_tz_callback, pattern=r"^tz_"))
    app.add_handler(CallbackQueryHandler(delete_goal_confirm, pattern=r"^del_\d+$"))
    app.add_handler(CallbackQueryHandler(delete_goal_execute, pattern=r"^delconfirm_\d+$"))
    app.add_handler(CallbackQueryHandler(delete_goal_cancel_cb, pattern=r"^delcancel$"))

    logger.info("Бот запущен (напоминания в %s:00)", REMINDER_HOUR)
    app.run_polling()


if __name__ == "__main__":
    main()
