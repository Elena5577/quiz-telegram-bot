import os
import logging
import random
import asyncio
import asyncpg
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DB_POOL = None
QUESTIONS = {}

# =================== DB ===================
async def init_db():
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                score INT DEFAULT 0,
                combo INT DEFAULT 0,
                correct INT DEFAULT 0,
                wrong INT DEFAULT 0,
                hints INT DEFAULT 5
            )
        """)

async def get_user(user_id: int):
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        if not row:
            await conn.execute("INSERT INTO users(user_id) VALUES($1)", user_id)
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return dict(row)

async def update_user(user_id: int, **fields):
    async with DB_POOL.acquire() as conn:
        sets = ", ".join([f"{k}=${i+2}" for i, k in enumerate(fields.keys())])
        values = list(fields.values())
        await conn.execute(f"UPDATE users SET {sets} WHERE user_id=$1", user_id, *values)

# =================== QUESTIONS ===================
def load_questions():
    global QUESTIONS
    QUESTIONS = {
        "Лёгкие": [
            ("Столица Франции?", ["Париж", "Берлин", "Рим", "Мадрид"], "Париж"),
            ("2+2?", ["3", "4", "5", "6"], "4"),
        ],
        "Средние": [
            ("Кто написал 'Евгения Онегина'?", ["Толстой", "Пушкин", "Гоголь", "Лермонтов"], "Пушкин"),
        ],
        "Сложные": [
            ("Год основания Рима?", ["753 до н.э.", "476 н.э.", "100 до н.э.", "1200 н.э."], "753 до н.э."),
        ],
    }

# =================== GAME ===================
async def send_stats_message(query, user, text):
    stats = (
        f"{text}\n"
        f"🏆 Счёт: {user['score']}\n"
        f"🔥 Комбо: {user['combo']}\n"
        f"✔️ Верных: {user['correct']} ❌ Ошибок: {user['wrong']}\n"
        f"💡 Подсказок: {user['hints']}"
    )
    try:
        await query.edit_message_text(stats, reply_markup=query.message.reply_markup)
    except:
        await query.message.reply_text(stats, reply_markup=query.message.reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🎯 Играть", callback_data="menu_categories")]]
    await update.message.reply_text("👋 Привет! Добро пожаловать в викторину!", reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("📚 Лёгкие", callback_data="cat:Лёгкие")],
                [InlineKeyboardButton("🧩 Средние", callback_data="cat:Средние")],
                [InlineKeyboardButton("🧠 Сложные", callback_data="cat:Сложные")]]
    await query.edit_message_text("Выберите категорию:", reply_markup=InlineKeyboardMarkup(keyboard))

async def choose_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    category = query.data.split(":")[1]
    context.user_data["category"] = category
    await query.answer()
    keyboard = [[InlineKeyboardButton("⭐ Лёгкий", callback_data="diff:5")],
                [InlineKeyboardButton("⭐⭐ Средний", callback_data="diff:10")],
                [InlineKeyboardButton("⭐⭐⭐ Сложный", callback_data="diff:15")]]
    await query.edit_message_text("Выберите сложность:", reply_markup=InlineKeyboardMarkup(keyboard))

async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    diff = int(query.data.split(":")[1])
    context.user_data["difficulty"] = diff
    category = context.user_data["category"]
    question, options, answer = random.choice(QUESTIONS[category])
    context.user_data["answer"] = answer
    buttons = [[InlineKeyboardButton(opt, callback_data=f"ans:{opt}")] for opt in options]
    await query.edit_message_text(question, reply_markup=InlineKeyboardMarkup(buttons))

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    choice = query.data.split(":")[1]
    user = await get_user(query.from_user.id)
    answer = context.user_data.get("answer")
    diff = context.user_data.get("difficulty", 5)

    if choice == answer:
        user["score"] += diff
        user["combo"] += 1
        user["correct"] += 1
        text = f"✅ Правильно! +{diff}"
        if user["combo"] % 3 == 0:
            user["score"] += 5
            user["hints"] += 1
            text += "\n🔥 Комбо! +5 баллов, +1 подсказка"
    else:
        user["wrong"] += 1
        user["combo"] = 0
        text = f"❌ Неправильно! Верный ответ: {answer}"

    await update_user(query.from_user.id, **user)
    await send_stats_message(query, user, text)

    # следующий вопрос
    category = context.user_data["category"]
    question, options, answer = random.choice(QUESTIONS[category])
    context.user_data["answer"] = answer
    buttons = [[InlineKeyboardButton(opt, callback_data=f"ans:{opt}")] for opt in options]
    await query.message.reply_text(question, reply_markup=InlineKeyboardMarkup(buttons))

# =================== BUILD ===================
def build_app(bot_token: str) -> Application:
    app = ApplicationBuilder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_categories, pattern="^menu_categories$"))
    app.add_handler(CallbackQueryHandler(choose_difficulty, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(send_question, pattern="^diff:"))
    app.add_handler(CallbackQueryHandler(handle_answer, pattern="^ans:"))
    return app


async def main():
    bot_token = os.getenv("BOT_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    print("=== DEBUG STARTUP ===")
    print("BOT_TOKEN:", bot_token[:10] if bot_token else "❌ not found")
    print("DATABASE_URL:", db_url[:30] if db_url else "❌ not found")
    print("=====================")

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не найден")

    load_questions()
    await init_db()
    app = build_app(bot_token)
    log.info("Бот запущен.")
    # Асинхронный запуск
    await app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
