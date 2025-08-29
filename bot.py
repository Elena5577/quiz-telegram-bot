import sys
print("Python version:", sys.version)
import os
import json
import random
import logging
import asyncio
import asyncpg
from typing import Dict, List, Tuple, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ================== ЛОГИ ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
QUESTIONS_FILE = "questions.json"

# категории
CATEGORIES = [
    ("История 📜", "history"),
    ("География 🌍", "geography"),
    ("Астрономия 🌌", "astronomy"),
    ("Биология 🧬", "biology"),
    ("Кино 🎬", "cinema"),
    ("Музыка 🎵", "music"),
    ("Литература 📚", "literature"),
    ("Наука 🔬", "science"),
    ("Искусство 🎨", "art"),
    ("Техника ⚙️", "technique"),
]

DIFFICULTY_POINTS = {"easy": 5, "medium": 10, "hard": 15}

# ================== ГЛОБАЛЬНЫЕ ==================
questions: List[dict] = []
db_pool: Optional[asyncpg.Pool] = None


# ================== БАЗА ==================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                score INT DEFAULT 0,
                combo INT DEFAULT 0,
                correct INT DEFAULT 0,
                wrong INT DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS used_questions (
                user_id BIGINT,
                question TEXT,
                PRIMARY KEY(user_id, question)
            );
            """
        )


async def ensure_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING;",
            user_id,
        )


async def db_get_progress(user_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT score, combo, correct, wrong FROM users WHERE user_id=$1", user_id
        )
        return row["score"], row["combo"], row["correct"], row["wrong"]


async def db_update_progress(user_id: int, score: int, combo: int, correct: int, wrong: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET score=$2, combo=$3, correct=$4, wrong=$5 WHERE user_id=$1",
            user_id,
            score,
            combo,
            correct,
            wrong,
        )


async def db_mark_used(user_id: int, question: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO used_questions(user_id, question) VALUES($1,$2) ON CONFLICT DO NOTHING",
            user_id,
            question,
        )


async def db_is_used(user_id: int, question: str) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_questions WHERE user_id=$1 AND question=$2", user_id, question
        )
        return row is not None


# ================== ВОПРОСЫ ==================
def load_questions():
    global questions
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    questions = data["questions"]


def get_question(user_id: int, category: str, difficulty: str) -> Optional[dict]:
    candidates = [
        q
        for q in questions
        if q["category"] == category and q["difficulty"] == difficulty
    ]
    random.shuffle(candidates)
    for q in candidates:
        # фильтруем уже использованные
        # !!! async нельзя, поэтому отметка делается потом в answer_cb
        return q
    return None


# ================== КЛАВИАТУРЫ ==================
def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = []
    for i in range(0, len(CATEGORIES), 2):
        row = [
            InlineKeyboardButton(CATEGORIES[i][0], callback_data=f"cat|{CATEGORIES[i][1]}"),
            InlineKeyboardButton(CATEGORIES[i + 1][0], callback_data=f"cat|{CATEGORIES[i+1][1]}"),
        ]
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def difficulty_kb(cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Лёгкий 🌱", callback_data=f"diff|{cat}|easy"),
                InlineKeyboardButton("Средний ⚖️", callback_data=f"diff|{cat}|medium"),
                InlineKeyboardButton("Сложный 🔥", callback_data=f"diff|{cat}|hard"),
            ],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
        ]
    )


def answers_kb(q: dict) -> InlineKeyboardMarkup:
    opts = list(q["options"])
    random.shuffle(opts)
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"ans|{q['category']}|{q['difficulty']}|{opt}")]
        for opt in opts
    ]
    buttons.append([InlineKeyboardButton("💡 Подсказка", callback_data="hint")])
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


# ================== ХЕНДЛЕРЫ ==================
async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await ensure_user(user_id)
    score, combo, tc, tw = await db_get_progress(user_id)
    text = (
        f"🏠 *Викторина*\n\n"
        f"Ваш счёт: *{score}* баллов\n"
        f"Комбо: *{combo}*\n"
        f"Правильных: *{tc}*, Неправильных: *{tw}*\n\n"
        f"Выберите категорию:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_menu_kb(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=main_menu_kb(), parse_mode="Markdown"
        )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(update, context)


async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await ensure_user(user_id)
    score, combo, tc, tw = await db_get_progress(user_id)
    await update.message.reply_text(
        f"📊 Ваш счёт: {score}\nКомбо: {combo}\nПравильных: {tc}, Неправильных: {tw}"
    )


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await send_menu(update, context)


async def category_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.split("|")[1]
    await q.edit_message_text(
        f"Выберите сложность для категории: {cat}", reply_markup=difficulty_kb(cat)
    )


async def difficulty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cat, diff = q.data.split("|")
    qst = get_question(update.effective_user.id, cat, diff)
    if not qst:
        await q.edit_message_text("❌ Вопросы закончились.", reply_markup=main_menu_kb())
        return
    await q.edit_message_text(
        f"❓ {qst['question']}", reply_markup=answers_kb(qst)
    )
    context.user_data["current_q"] = qst


async def answer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = q.data.split("|")
    chosen = data[3]
    qst = context.user_data.get("current_q")
    if not qst:
        await q.edit_message_text("Ошибка. Попробуйте снова.", reply_markup=main_menu_kb())
        return

    correct = qst["answer"]
    score, combo, tc, tw = await db_get_progress(user_id)

    if chosen == correct:
        base = DIFFICULTY_POINTS[qst["difficulty"]]
        combo += 1
        extra = 5 if combo % 3 == 0 else 0
        score += base + extra
        tc += 1
        await q.edit_message_text(
            f"✅ Правильно! (+{base}{' +5 комбо' if extra else ''})",
            reply_markup=main_menu_kb(),
        )
    else:
        combo = 0
        tw += 1
        await q.edit_message_text(
            f"❌ Неверно! Правильный ответ: {correct}", reply_markup=main_menu_kb()
        )

    await db_update_progress(user_id, score, combo, tc, tw)
    await db_mark_used(user_id, qst["question"])


async def hint_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    qst = context.user_data.get("current_q")
    if not qst:
        return
    options = list(qst["options"])
    options.remove(qst["answer"])
    to_remove = random.sample(options, 2)
    new_opts = [o for o in qst["options"] if o not in to_remove]
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"ans|{qst['category']}|{qst['difficulty']}|{opt}")]
        for opt in new_opts
    ]
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu")])
    await q.edit_message_text(f"❓ {qst['question']}", reply_markup=InlineKeyboardMarkup(buttons))


# ================== РЕГИСТРАЦИЯ ==================
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(category_cb, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(difficulty_cb, pattern=r"^diff\|"))
    app.add_handler(CallbackQueryHandler(answer_cb, pattern=r"^ans\|"))
    app.add_handler(CallbackQueryHandler(hint_cb, pattern="^hint$"))


# ================== MAIN ==================
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    register_handlers(app)
    return app


async def main():
    load_questions()
    await init_db()
    app = build_app()
    log.info("Бот запущен.")
    await app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
