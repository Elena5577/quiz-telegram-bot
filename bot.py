import os
import asyncio
import logging
import asyncpg
import random
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ==========================
# Логирование
# ==========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_POOL = None

# ==========================
# Инициализация базы данных
# ==========================
async def init_db():
    """Создаёт пул соединений и необходимые таблицы."""
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(dsn=os.getenv("DATABASE_URL"))

    async with DB_POOL.acquire() as conn:
        # Таблица пользователей
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
        # Таблица вопросов
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            category TEXT,
            difficulty TEXT,
            question TEXT,
            options TEXT[],
            answer TEXT
        )
        """)

async def ensure_user(user_id: int):
    """Проверяет, есть ли пользователь в БД, и создаёт при необходимости."""
    async with DB_POOL.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        if not user:
            await conn.execute(
                "INSERT INTO users (user_id, score, combo, correct, wrong, hints) VALUES ($1,0,0,0,0,5)",
                user_id
            )

# ==========================
# HUD (информация для пользователя)
# ==========================
async def get_hud(user_id: int):
    async with DB_POOL.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return (f"🏆 Счёт: {user['score']}\n"
            f"🔥 Комбо: {user['combo']}\n"
            f"✅ Верных: {user['correct']} ❌ Ошибок: {user['wrong']}\n"
            f"🔑 Подсказки: {user['hints']}")

# ==========================
# Игровой процесс
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await ensure_user(user_id)
    hud = await get_hud(user_id)

    keyboard = [
        [InlineKeyboardButton("🌍 География", callback_data="cat:geo"),
         InlineKeyboardButton("🔬 Наука", callback_data="cat:science")],
        [InlineKeyboardButton("📖 История", callback_data="cat:history"),
         InlineKeyboardButton("🎬 Кино", callback_data="cat:cinema")],
        [InlineKeyboardButton("⚽ Спорт", callback_data="cat:sport"),
         InlineKeyboardButton("💻 IT", callback_data="cat:it")],
        [InlineKeyboardButton("🎨 Искусство", callback_data="cat:art"),
         InlineKeyboardButton("🎵 Музыка", callback_data="cat:music")],
        [InlineKeyboardButton("📚 Литература", callback_data="cat:literature"),
         InlineKeyboardButton("❓ Разное", callback_data="cat:other")],
    ]
    # обработка как для message (пришёл /start)
    if update.message:
        await update.message.reply_text(f"{hud}\n\nВыбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # на всякий случай, если update пришёл не как message
        try:
            await context.bot.send_message(chat_id=update.effective_user.id, text=f"{hud}\n\nВыбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass

async def choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.split(":")[1]

    keyboard = [
        [InlineKeyboardButton("Легкая (+5)", callback_data=f"diff:{cat}:easy")],
        [InlineKeyboardButton("Средняя (+10)", callback_data=f"diff:{cat}:medium")],
        [InlineKeyboardButton("Сложная (+15)", callback_data=f"diff:{cat}:hard")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
    ]
    hud = await get_hud(query.from_user.id)
    await query.edit_message_text(f"{hud}\n\nКатегория выбрана. Теперь выбери сложность:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))

async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE, category, difficulty):
    user_id = update.effective_user.id
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM questions WHERE category=$1 AND difficulty=$2 ORDER BY random() LIMIT 1",
            category, difficulty
        )
    if not row:
        hud = await get_hud(user_id)
        await update.callback_query.edit_message_text(f"{hud}\n\nВопросы в этой категории закончились. Выбери другую.")
        return

    options = row["options"]
    random.shuffle(options)

    buttons = [[InlineKeyboardButton(opt, callback_data=f"ans:{row['id']}:{opt}")]
               for opt in options]
    buttons.append([InlineKeyboardButton("Подсказка 🔑 (-10)", callback_data=f"hint:{row['id']}")])

    hud = await get_hud(user_id)
    await update.callback_query.edit_message_text(
        f"{hud}\n\n❓ {row['question']}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def choose_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, cat, diff = query.data.split(":")
    await send_question(update, context, cat, diff)

# ==========================
# Обработка ответа
# ==========================
async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, qid, ans = query.data.split(":", 2)

    async with DB_POOL.acquire() as conn:
        q = await conn.fetchrow("SELECT * FROM questions WHERE id=$1", int(qid))
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

        if ans == q["answer"]:
            points = {"easy": 5, "medium": 10, "hard": 15}[q["difficulty"]]
            new_score = user["score"] + points
            new_combo = user["combo"] + 1
            new_correct = user["correct"] + 1
            new_hints = user["hints"]

            if new_combo % 3 == 0:
                new_hints += 1  # бонус подсказка за комбо

            await conn.execute("""UPDATE users SET score=$1, combo=$2, correct=$3, hints=$4 WHERE user_id=$5""",
                               new_score, new_combo, new_correct, new_hints, user_id)
            text = f"✅ Верно! +{points} очков."
        else:
            new_score = max(0, user["score"] - 5)
            await conn.execute("""UPDATE users SET score=$1, combo=0, wrong=wrong+1 WHERE user_id=$2""",
                               new_score, user_id)
            text = f"❌ Неверно! -5 очков."

    hud = await get_hud(user_id)
    await query.edit_message_text(f"{hud}\n\n{text}\n\nВыбери категорию:",
                                  reply_markup=await main_menu())

# ==========================
# Подсказка
# ==========================
async def use_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, qid = query.data.split(":")

    async with DB_POOL.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        q = await conn.fetchrow("SELECT * FROM questions WHERE id=$1", int(qid))

        if user["hints"] <= 0 or user["score"] < 10:
            hud = await get_hud(user_id)
            await query.edit_message_text(f"{hud}\n\n❗ Недостаточно подсказок или очков.")
            return

        wrong_opts = [opt for opt in q["options"] if opt != q["answer"]]
        keep_wrong = random.sample(wrong_opts, 1)
        new_options = [q["answer"]] + keep_wrong
        random.shuffle(new_options)

        await conn.execute("UPDATE users SET hints=hints-1, score=score-10 WHERE user_id=$1", user_id)

    buttons = [[InlineKeyboardButton(opt, callback_data=f"ans:{q['id']}:{opt}")]
               for opt in new_options]

    hud = await get_hud(user_id)
    await query.edit_message_text(f"{hud}\n\n❓ {q['question']}",
                                  reply_markup=InlineKeyboardMarkup(buttons))

# ==========================
# Главное меню
# ==========================
async def main_menu():
    keyboard = [
        [InlineKeyboardButton("🌍 География", callback_data="cat:geo"),
         InlineKeyboardButton("🔬 Наука", callback_data="cat:science")],
        [InlineKeyboardButton("📖 История", callback_data="cat:history"),
         InlineKeyboardButton("🎬 Кино", callback_data="cat:cinema")],
        [InlineKeyboardButton("⚽ Спорт", callback_data="cat:sport"),
         InlineKeyboardButton("💻 IT", callback_data="cat:it")],
        [InlineKeyboardButton("🎨 Искусство", callback_data="cat:art"),
         InlineKeyboardButton("🎵 Музыка", callback_data="cat:music")],
        [InlineKeyboardButton("📚 Литература", callback_data="cat:literature"),
         InlineKeyboardButton("❓ Разное", callback_data="cat:other")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ==========================
# Регистрация хендлеров
# ==========================
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(choose_category, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(choose_difficulty, pattern="^diff:"))
    app.add_handler(CallbackQueryHandler(answer, pattern="^ans:"))
    app.add_handler(CallbackQueryHandler(use_hint, pattern="^hint:"))
    app.add_handler(CallbackQueryHandler(start, pattern="^menu$"))

def build_app(bot_token: str) -> Application:
    app = ApplicationBuilder().token(bot_token).build()
    register_handlers(app)
    return app

# ==========================
# AIOHTTP webhook handler
# ==========================
def make_aiohttp_app(telegram_app: Application, bot_token: str):
    async def handle(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")

        try:
            update = Update.de_json(data, telegram_app.bot)
            # помещаем апдейт в очередь приложения
            await telegram_app.update_queue.put(update)
        except Exception as e:
            logger.exception("Failed to enqueue update: %s", e)
            return web.Response(status=500, text="error")
        return web.Response(text="OK")

    aio_app = web.Application()
    # webhook path
    aio_app.router.add_post(f"/webhook/{bot_token}", handle)
    # health check (Render может сканировать)
    async def health(request):
        return web.Response(text="OK")
    aio_app.router.add_get("/", health)
    return aio_app

# ==========================
# Асинхронный старт бота (webhook для Render)
# ==========================
async def async_main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")
    RENDER_URL = os.getenv("RENDER_URL")  # https://your-app.onrender.com
    PORT = int(os.getenv("PORT", "8080"))  # Render даёт PORT

    print("=== DEBUG STARTUP ===")
    print("BOT_TOKEN:", (BOT_TOKEN[:10] + "…") if BOT_TOKEN else "❌ not found")
    print("DATABASE_URL:", (DATABASE_URL[:30] + "…") if DATABASE_URL else "❌ not found")
    print("RENDER_URL:", RENDER_URL if RENDER_URL else "❌ not found")
    print("PORT:", PORT)
    print("=====================")

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден")
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — если нужен, задайте в окружении")

    # init DB
    await init_db()

    # build telegram app and handlers
    telegram_app = build_app(BOT_TOKEN)

    # initialize & start application (so update_queue exists)
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Telegram application initialized and started.")

    # prepare aiohttp server
    aio_app = make_aiohttp_app(telegram_app, BOT_TOKEN)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("aiohttp server started on port %s", PORT)

    # set webhook at Telegram
    if RENDER_URL:
        webhook_url = f"{RENDER_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    else:
        # fallback: try to read external url env that some providers set
        external = os.getenv("EXTERNAL_URL") or os.getenv("RENDER_EXTERNAL_URL")
        if external:
            webhook_url = f"{external.rstrip('/')}/webhook/{BOT_TOKEN}"
        else:
            raise RuntimeError("RENDER_URL (или EXTERNAL_URL) не задан — нужен публичный URL для webhook")

    # delete previous webhook (safe) and set new one
    try:
        await telegram_app.bot.delete_webhook()
    except Exception:
        pass

    await telegram_app.bot.set_webhook(webhook_url)
    logger.info("Webhook set to %s", webhook_url)

    # держим процесс живым; корректно реагируем на KeyboardInterrupt/terminate
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Received exit signal, shutting down...")
    finally:
        # очистка
        try:
            await telegram_app.bot.delete_webhook()
        except Exception:
            pass
        await runner.cleanup()
        await telegram_app.stop()
        await telegram_app.shutdown()
        # закрываем DB pool
        if DB_POOL:
            await DB_POOL.close()

if __name__ == "__main__":
    asyncio.run(async_main())
