import os
import asyncio
import logging
import asyncpg
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

db_pool: asyncpg.Pool = None
questions = {}
user_stats = {}

# ---------------------- Вопросы ----------------------
def load_questions():
    global questions
    questions = {
        "math": {
            "easy": [
                {"q": "2+2=?", "a": "4", "options": ["3", "4", "5", "6"]},
                {"q": "5-3=?", "a": "2", "options": ["1", "2", "3", "4"]},
            ],
            "hard": [
                {"q": "12*12=?", "a": "144", "options": ["144", "154", "124", "164"]},
            ],
        },
        "history": {
            "easy": [
                {"q": "Кто был первым президентом США?", "a": "Вашингтон", "options": ["Вашингтон", "Линкольн", "Джефферсон", "Адамс"]},
            ],
            "hard": [
                {"q": "В каком году началась Вторая мировая война?", "a": "1939", "options": ["1939", "1941", "1914", "1945"]},
            ],
        }
    }


# ---------------------- База ----------------------
async def init_db():
    global db_pool
    db_url = os.getenv("DATABASE_URL")
    db_pool = await asyncpg.create_pool(db_url)
    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            score INT DEFAULT 0,
            correct INT DEFAULT 0,
            wrong INT DEFAULT 0,
            combo INT DEFAULT 0,
            hints INT DEFAULT 5
        )""")


async def ensure_user(user_id: int):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        if not user:
            await conn.execute("INSERT INTO users (user_id) VALUES ($1)", user_id)


async def update_stats(user_id: int, score=0, correct=0, wrong=0, combo=None, hints=None):
    async with db_pool.acquire() as conn:
        if combo is None and hints is None:
            await conn.execute(
                "UPDATE users SET score=score+$1, correct=correct+$2, wrong=wrong+$3 WHERE user_id=$4",
                score, correct, wrong, user_id
            )
        else:
            await conn.execute(
                "UPDATE users SET score=score+$1, correct=correct+$2, wrong=wrong+$3, combo=$5, hints=$6 WHERE user_id=$4",
                score, correct, wrong, user_id, combo, hints
            )


async def get_stats(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


# ---------------------- UI ----------------------
def stats_text(user):
    return (
        f"🏆 Счёт: {user['score']}\n"
        f"🔥 Комбо: {user['combo']}\n"
        f"✔️ Верных: {user['correct']} ❌ Ошибок: {user['wrong']}\n"
        f"💡 Подсказок: {user['hints']}"
    )


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await ensure_user(user_id)
    user = await get_stats(user_id)
    keyboard = [
        [InlineKeyboardButton("📐 Математика", callback_data="cat:math")],
        [InlineKeyboardButton("📜 История", callback_data="cat:history")],
    ]
    await update.message.reply_text(
        f"👋 Привет, {update.effective_user.first_name}!\n"
        f"Выбери категорию 👇\n\n{stats_text(user)}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ---------------------- Игра ----------------------
async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE, category, difficulty):
    q = random.choice(questions[category][difficulty])
    context.user_data["current_q"] = q
    context.user_data["category"] = category
    context.user_data["difficulty"] = difficulty

    options = q["options"].copy()
    random.shuffle(options)
    buttons = [[InlineKeyboardButton(opt, callback_data=f"ans:{opt}")] for opt in options]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"❓ {q['q']}", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            f"❓ {q['q']}", reply_markup=InlineKeyboardMarkup(buttons)
        )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await ensure_user(user_id)
    user = await get_stats(user_id)

    chosen = query.data.split(":")[1]
    q = context.user_data.get("current_q")

    if not q:
        await query.answer("Вопроса нет!")
        return

    if chosen == q["a"]:
        # правильно
        new_combo = user["combo"] + 1
        bonus = 0
        hints = user["hints"]
        if new_combo % 3 == 0:
            bonus = 5
            hints += 1
        await update_stats(user_id, score=10 + bonus, correct=1, combo=new_combo, hints=hints, wrong=0)
        user = await get_stats(user_id)
        text = f"✅ Правильно! +10\n"
        if bonus:
            text += f"🔥 Комбо {new_combo}! +5 и +1 💡\n"
    else:
        # неправильно
        new_combo = 0
        await update_stats(user_id, score=-5, wrong=1, combo=new_combo, hints=user["hints"])
        user = await get_stats(user_id)
        text = f"❌ Неверно! -5\n"

    text += "\n" + stats_text(user)

    await query.edit_message_text(text)

    # следующий вопрос
    await ask_question(update, context, context.user_data["category"], context.user_data["difficulty"])


# ---------------------- Handlers ----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(update, context)


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    category = query.data.split(":")[1]
    keyboard = [
        [InlineKeyboardButton("😀 Лёгкий", callback_data=f"diff:{category}:easy")],
        [InlineKeyboardButton("🤯 Сложный", callback_data=f"diff:{category}:hard")],
    ]
    await query.edit_message_text("Выбери сложность:", reply_markup=InlineKeyboardMarkup(keyboard))


async def difficulty_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, category, diff = query.data.split(":")
    await ask_question(update, context, category, diff)


# ---------------------- MAIN ----------------------
def build_app(bot_token: str) -> Application:
    app = ApplicationBuilder().token(bot_token).build()
    register_handlers(app)
    return app


def main():
    bot_token = os.getenv("BOT_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    print("=== DEBUG STARTUP ===")
    print("BOT_TOKEN:", bot_token[:10] if bot_token else "❌ not found")
    print("DATABASE_URL:", db_url[:30] if db_url else "❌ not found")
    print("=====================")

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не найден")

    load_questions()

    # инициализируем БД (т.к. init_db() async → запускаем внутри event loop)
    import asyncio
    asyncio.run(init_db())

    app = build_app(bot_token)
    log.info("Бот запущен.")
    # ⬅️ здесь СИНХРОННО, без await и без asyncio.run()
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
