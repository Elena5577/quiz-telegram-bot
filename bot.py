# -*- coding: utf-8 -*-
"""
Викторина для Telegram (PTB 20.7)
- 10 категорий (кнопки в 2 столбца)
- 3 уровня сложности: лёгкий/средний/сложный
- Таймер 30 секунд с посекундным обратным отсчётом (обновляем то же сообщение)
- Подсказка: убирает 2 неверных варианта и -10 очков (1 раз на вопрос)
- Очки: 5/10/15 за лёгкий/средний/сложный
- Комбо: каждые 3 правильных подряд +5 очков
- Сохранение прогресса в Postgres (очки, использованные вопросы, серийность)
- Без спама: после выбора всё происходит через edit_message_text / edit_reply_markup
- Требует env: BOT_TOKEN, DATABASE_URL
"""

import asyncio
import json
import os
import random
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import asyncpg
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ---------- Конфиг / файлы ----------
QUESTIONS_FILE = Path("questions.json")  # лежит рядом с bot.py в репозитории
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("Environment variable DATABASE_URL is not set")

# Категории: (текст кнопки, slug, русское имя в JSON)
CATEGORIES: List[Tuple[str, str, str]] = [
    ("История 📜", "history", "История"),
    ("География 🌍", "geography", "География"),
    ("Астрономия 🌌", "astronomy", "Астрономия"),
    ("Биология 🧬", "biology", "Биология"),
    ("Кино 🎬", "cinema", "Кино"),
    ("Музыка 🎵", "music", "Музыка"),
    ("Литература 📚", "literature", "Литература"),
    ("Наука 🔬", "science", "Наука"),
    ("Искусство 🎨", "art", "Искусство"),
    ("Техника ⚙️", "technique", "Техника"),
]

DIFF_LABEL = {
    "easy": ("Лёгкий", 5),
    "medium": ("Средний", 10),
    "hard": ("Сложный", 15),
}

# ---------- Загрузка вопросов ----------
# Ожидается структура:
# { "questions": [ { "difficulty": "easy|medium|hard", "question": "…",
#                    "options": ["…","…","…","…"], "answer": "…",
#                    "category": "Искусство" }, ... ] }
with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
    RAW = json.load(f)

# Индексируем: (slug, diff) -> список вопросов
QUESTIONS: Dict[Tuple[str, str], List[dict]] = {}
rus_to_slug = {rus: slug for _, slug, rus in CATEGORIES}

for q in RAW.get("questions", []):
    rus_cat = q.get("category", "").strip()
    diff = q.get("difficulty", "").strip().lower()
    slug = rus_to_slug.get(rus_cat)
    if slug and diff in DIFF_LABEL and isinstance(q.get("options"), list) and q.get("answer"):
        QUESTIONS.setdefault((slug, diff), []).append(q)

# ---------- Работа с БД ----------
async def db() -> asyncpg.Pool:
    # создаём пул один раз и кешируем на приложении
    if not getattr(db, "_pool", None):
        db._pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return db._pool  # type: ignore[attr-defined]


async def init_db():
    pool = await db()
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            score INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS used_questions (
            user_id BIGINT NOT NULL,
            qhash TEXT NOT NULL,
            PRIMARY KEY (user_id, qhash)
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id BIGINT PRIMARY KEY,
            category TEXT,
            difficulty TEXT,
            message_chat BIGINT,
            message_id BIGINT
        );
        """)

async def get_user_row(user_id: int) -> Tuple[int, int]:
    pool = await db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT score, streak FROM users WHERE user_id=$1", user_id)
        if row:
            return row["score"], row["streak"]
        await conn.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
        return 0, 0

async def set_score_and_streak(user_id: int, score: int, streak: int):
    pool = await db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(user_id, score, streak) VALUES($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET score=EXCLUDED.score, streak=EXCLUDED.streak",
            user_id, score, streak
        )

async def mark_used(user_id: int, qhash: str):
    pool = await db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO used_questions(user_id, qhash) VALUES($1,$2) ON CONFLICT DO NOTHING",
            user_id, qhash
        )

async def is_used(user_id: int, qhash: str) -> bool:
    pool = await db()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM used_questions WHERE user_id=$1 AND qhash=$2)",
            user_id, qhash
        )

async def save_session(user_id: int, category: Optional[str], difficulty: Optional[str],
                       chat_id: Optional[int], message_id: Optional[int]):
    pool = await db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions(user_id, category, difficulty, message_chat, message_id) "
            "VALUES($1,$2,$3,$4,$5) "
            "ON CONFLICT (user_id) DO UPDATE SET category=EXCLUDED.category, "
            "difficulty=EXCLUDED.difficulty, message_chat=EXCLUDED.message_chat, "
            "message_id=EXCLUDED.message_id",
            user_id, category, difficulty, chat_id, message_id
        )

async def clear_session_message(user_id: int):
    pool = await db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET message_chat=NULL, message_id=NULL WHERE user_id=$1",
            user_id
        )

# ---------- Временное состояние в памяти ----------
# тут храним активный вопрос, порядок опций, задача таймера, флаг подсказки
class RoundState:
    def __init__(self, category: str, difficulty: str, q: dict,
                 options_order: List[str], correct: str,
                 chat_id: int, message_id: int):
        self.category = category
        self.difficulty = difficulty
        self.question = q
        self.options_order = options_order[:]  # фиксируем, чтобы кнопки "не прыгали"
        self.correct = correct
        self.chat_id = chat_id
        self.message_id = message_id
        self.hint_used = False
        self.timer_task: Optional[asyncio.Task] = None
        self.time_left = 30

STATE: Dict[int, RoundState] = {}  # user_id -> RoundState

# ---------- Утилиты ----------
def qhash(q: dict) -> str:
    base = f"{q.get('category','')}|{q.get('difficulty','')}|{q.get('question','')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def build_menu_keyboard(score: int) -> InlineKeyboardMarkup:
    # 2 столбца категорий
    btns: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for text, slug, _ in CATEGORIES:
        row.append(InlineKeyboardButton(text, callback_data=f"cat|{slug}"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    # Служебная строка
    btns.append([InlineKeyboardButton("ℹ️ Правила/Очки", callback_data="info")])
    return InlineKeyboardMarkup(btns)

def build_diff_keyboard(cat_slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Лёгкий", callback_data=f"diff|{cat_slug}|easy"),
            InlineKeyboardButton("🟡 Средний", callback_data=f"diff|{cat_slug}|medium"),
            InlineKeyboardButton("🔴 Сложный", callback_data=f"diff|{cat_slug}|hard"),
        ],
        [InlineKeyboardButton("🏠 В меню", callback_data="menu")]
    ])

def build_question_keyboard(opts: List[str], enable_hint: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    # 2 на строку
    for i in range(0, len(opts), 2):
        pair = [InlineKeyboardButton(opts[i], callback_data=f"ans|{opts[i]}")]
        if i + 1 < len(opts):
            pair.append(InlineKeyboardButton(opts[i+1], callback_data=f"ans|{opts[i+1]}"))
        rows.append(pair)
    service: List[InlineKeyboardButton] = []
    if enable_hint:
        service.append(InlineKeyboardButton("🪄 Подсказка (−10)", callback_data="hint"))
    service.append(InlineKeyboardButton("🏠 В меню", callback_data="menu"))
    rows.append(service)
    return InlineKeyboardMarkup(rows)

def score_for_diff(diff: str) -> int:
    return DIFF_LABEL[diff][1]

def rus_cat_by_slug(slug: str) -> str:
    for text, s, rus in CATEGORIES:
        if s == slug:
            return rus
    return slug

# ---------- Таймер ----------
async def run_timer(user_id: int, app: Application):
    st = STATE.get(user_id)
    if not st:
        return
    try:
        while st.time_left > 0:
            await asyncio.sleep(1)
            st.time_left -= 1
            # обновляем заголовок вопроса (без перетасовки кнопок)
            header = f"⏳ Осталось: {st.time_left:02d} c\n\n"
            cat = rus_cat_by_slug(st.category)
            diff_title = DIFF_LABEL[st.difficulty][0]
            text = f"{header}Категория: {cat} · Сложность: {diff_title}\n\n❓ {st.question['question']}"
            try:
                await app.bot.edit_message_text(
                    chat_id=st.chat_id,
                    message_id=st.message_id,
                    text=text,
                )
                await app.bot.edit_message_reply_markup(
                    chat_id=st.chat_id,
                    message_id=st.message_id,
                    reply_markup=build_question_keyboard(st.options_order, not st.hint_used),
                )
            except Exception:
                # игнорируем редкие ошибки Too Many Requests / message not modified
                pass

        # время вышло — считаем как неверно
        await handle_answer_result(user_id, correct=False, app=app, reason="⏰ Время вышло")
    finally:
        # при любом завершении — сброс таймера
        st2 = STATE.get(user_id)
        if st2:
            st2.timer_task = None

# ---------- Выдача вопросов ----------
async def next_question(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        cat_slug: str, diff: str, reuse_message: Optional[Tuple[int, int]] = None):
    user_id = update.effective_user.id
    pool = await db()

    # подбираем неиспользованный вопрос
    candidates = QUESTIONS.get((cat_slug, diff), [])[:]
    random.shuffle(candidates)

    chosen: Optional[dict] = None
    for q in candidates:
        if not await is_used(user_id, qhash(q)):
            chosen = q
            break

    if not chosen:
        # вопросы закончились
        score, _ = await get_user_row(user_id)
        txt = (
            f"Категория: {rus_cat_by_slug(cat_slug)} · {DIFF_LABEL[diff][0]}\n\n"
            "🛑 Вопросы в этой подборке закончились!\n\n"
            f"Текущий счёт: {score} очков"
        )
        if reuse_message:
            chat_id, msg_id = reuse_message
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=txt,
                                                reply_markup=build_menu_keyboard(score))
            await clear_session_message(user_id)
        else:
            await update.effective_message.reply_text(txt, reply_markup=build_menu_keyboard(score))
        STATE.pop(user_id, None)
        return

    # фиксируем порядок опций один раз, чтобы кнопки "не прыгали"
    options = chosen["options"][:]
    random.shuffle(options)
    correct = chosen["answer"]

    header = f"⏳ Осталось: 30 c\n\nКатегория: {rus_cat_by_slug(cat_slug)} · {DIFF_LABEL[diff][0]}\n\n"
    text = header + f"❓ {chosen['question']}"

    if reuse_message:
        chat_id, msg_id = reuse_message
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=build_question_keyboard(options, True),
        )
        message_chat, message_id = chat_id, msg_id
    else:
        sent = await update.effective_message.reply_text(
            text, reply_markup=build_question_keyboard(options, True)
        )
        message_chat, message_id = sent.chat_id, sent.message_id

    # сохраняем состояние раунда в памяти
    st = RoundState(cat_slug, diff, chosen, options, correct, message_chat, message_id)
    STATE[user_id] = st

    # помечаем сессию и запускаем таймер
    await save_session(user_id, cat_slug, diff, message_chat, message_id)
    st.timer_task = asyncio.create_task(run_timer(user_id, context.application))

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    score, _ = await get_user_row(user.id)
    welcome = (
        f"Привет, {user.first_name}! 👋\n\n"
        "Это викторина по 10 категориям. Выбирай категорию и сложность.\n"
        "⏳ На ответ 30 секунд. Подсказка убирает 2 неправильных варианта и стоит 10 очков.\n"
        "Очки: 5/10/15 за лёгкий/средний/сложный. Каждые 3 правильных подряд — бонус +5 очков.\n\n"
        f"Текущий счёт: {score} очков"
    )
    await update.message.reply_text(welcome, reply_markup=build_menu_keyboard(score))


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    score, _ = await get_user_row(user_id)

    # отменяем таймер и стираем привязку сообщения
    st = STATE.pop(user_id, None)
    if st and st.timer_task and not st.timer_task.done():
        st.timer_task.cancel()
    await clear_session_message(user_id)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"🏠 Меню\nТекущий счёт: {score} очков",
        reply_markup=build_menu_keyboard(score)
    )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    score, streak = await get_user_row(user_id)
    text = (
        "ℹ️ Правила и очки\n\n"
        "• 30 секунд на ответ, таймер тикает в сообщении.\n"
        "• Подсказка убирает 2 неверных варианта и стоит 10 очков.\n"
        "• Очки за ответ: Лёгкий 5 · Средний 10 · Сложный 15.\n"
        "• Комбо: каждые 3 правильных подряд — +5 очков.\n\n"
        f"Сейчас: {score} очков · Серия правильных: {streak}"
    )
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text, reply_markup=build_menu_keyboard(score))


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _, slug = update.callback_query.data.split("|", 1)
    await update.callback_query.edit_message_text(
        f"Категория: {rus_cat_by_slug(slug)}\nВыбери сложность:",
        reply_markup=build_diff_keyboard(slug)
    )


async def difficulty_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _, slug, diff = update.callback_query.data.split("|", 2)
    # начинаем раунд, переиспользуя текущее сообщение
    chat_id = update.effective_message.chat_id
    msg_id = update.effective_message.message_id
    await next_question(update, context, slug, diff, reuse_message=(chat_id, msg_id))


async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = STATE.get(user_id)
    if not st:
        await update.callback_query.answer("Нет активного вопроса", show_alert=True)
        return

    if st.hint_used:
        await update.callback_query.answer("Подсказка уже использована", show_alert=True)
        return

    # списываем 10 очков
    score, streak = await get_user_row(user_id)
    score = score - 10
    await set_score_and_streak(user_id, score, streak)

    # скрываем две неверные кнопки (оставляем 2 варианта: правильный + один случайный неверный)
    incorrect = [o for o in st.options_order if o != st.correct]
    keep_wrong = random.choice(incorrect)
    new_opts = [st.correct, keep_wrong]
    random.shuffle(new_opts)
    st.options_order = new_opts
    st.hint_used = True

    await update.callback_query.answer("Подсказка: −10 очков")
    header = f"⏳ Осталось: {st.time_left:02d} c\n\nКатегория: {rus_cat_by_slug(st.category)} · {DIFF_LABEL[st.difficulty][0]}\n\n"
    text = header + f"❓ {st.question['question']}\n\n(Подсказка применена)"
    try:
        await update.callback_query.edit_message_text(text)
        await update.callback_query.edit_message_reply_markup(
            build_question_keyboard(st.options_order, enable_hint=False)
        )
    except Exception:
        pass  # на случай rate limit


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.effective_user.id
    st = STATE.get(user_id)
    if not st:
        # нет активного вопроса — просто в меню
        score, _ = await get_user_row(user_id)
        await update.callback_query.edit_message_text(
            f"Нет активного вопроса.\nСчёт: {score} очков",
            reply_markup=build_menu_keyboard(score)
        )
        return

    chosen = update.callback_query.data.split("|", 1)[1]
    correct = (chosen == st.correct)

    # Останавливаем таймер
    if st.timer_task and not st.timer_task.done():
        st.timer_task.cancel()
        st.timer_task = None

    await handle_answer_result(user_id, correct, context.application, chosen=chosen)


async def handle_answer_result(user_id: int, correct: bool, app: Application,
                               chosen: Optional[str] = None, reason: Optional[str] = None):
    """Начисление очков/комбо, отметка вопроса использованным, переход к следующему."""
    st = STATE.get(user_id)
    if not st:
        return

    # отметить вопрос как использованный
    await mark_used(user_id, qhash(st.question))

    # очки/серийность
    score, streak = await get_user_row(user_id)
    add = 0
    bonus_text = ""

    if correct:
        add = score_for_diff(st.difficulty)
        streak += 1
        # бонус за каждые 3 подряд
        if streak % 3 == 0:
            add += 5
            bonus_text = " (+5 комбо)"
    else:
        streak = 0

    score += add
    await set_score_and_streak(user_id, score, streak)

    # сообщение с результатом (обновляем то же сообщение)
    prefix = "✅ Правильно!" if correct else ("❌ Неверно." if not reason else reason + " — ответ неверный.")
    gained = f" +{add} очков" if add > 0 else ""
    answer_line = ""
    if not correct:
        answer_line = f"\nПравильный ответ: {st.correct}"

    txt = (
        f"{prefix}{gained}{bonus_text}\n"
        f"Счёт: {score} · Серия: {streak}{answer_line}\n\n"
        f"Категория: {rus_cat_by_slug(st.category)} · {DIFF_LABEL[st.difficulty][0]}"
    )

    try:
        await app.bot.edit_message_text(
            chat_id=st.chat_id,
            message_id=st.message_id,
            text=txt
        )
        await app.bot.edit_message_reply_markup(
            chat_id=st.chat_id,
            message_id=st.message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Следующий вопрос", callback_data=f"next|{st.category}|{st.difficulty}")],
                                               [InlineKeyboardButton("🏠 В меню", callback_data="menu")]])
        )
    except Exception:
        pass

    # очищаем состояние текущего вопроса, но оставляем выбранную категорию/сложность в сессии
    await save_session(user_id, st.category, st.difficulty, st.chat_id, st.message_id)
    STATE.pop(user_id, None)


async def next_same(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Следующий вопрос' в той же категории/сложности."""
    await update.callback_query.answer()
    _, cat_slug, diff = update.callback_query.data.split("|", 2)
    chat_id = update.effective_message.chat_id
    msg_id = update.effective_message.message_id
    await next_question(update, context, cat_slug, diff, reuse_message=(chat_id, msg_id))


# ---------- main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Инициализация БД перед стартом
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())

    # Команды
    app.add_handler(CommandHandler("start", start))

    # Кнопки меню/инфо
    app.add_handler(CallbackQueryHandler(menu, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(info, pattern=r"^info$"))

    # Категории -> сложности
    app.add_handler(CallbackQueryHandler(category_chosen, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(difficulty_chosen, pattern=r"^diff\|"))

    # Вопросы: подсказка/ответ/следующий
    app.add_handler(CallbackQueryHandler(hint, pattern=r"^hint$"))
    app.add_handler(CallbackQueryHandler(answer, pattern=r"^ans\|"))
    app.add_handler(CallbackQueryHandler(next_same, pattern=r"^next\|"))

    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
