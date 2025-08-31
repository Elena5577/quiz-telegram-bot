import os
import asyncio
import logging
import random
import json
from typing import Dict, Any, List, Tuple, Optional

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("quiz-bot")

# ================== GLOBALS ==================
QUESTIONS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
DIFFICULTY_POINTS = {"easy": 5, "medium": 10, "hard": 15}

# 👇 Категории с эмодзи
CATEGORIES: List[Tuple[str, str]] = [
    ("🌍 География", "geo"),
    ("🧪 Наука", "sci"),
    ("📜 История", "hist"),
    ("🎬 Кино", "movie"),
    ("⚽ Спорт", "sport"),
    ("💻 IT", "it"),
]

# ================== DB (lazy) ==================
db_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> Optional[asyncpg.Pool]:
    """Создаём пул только внутри event loop PTB, чтобы не было 'attached to a different loop'."""
    global db_pool
    if db_pool is not None:
        return db_pool
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        log.warning("DATABASE_URL не задан — работаем без БД")
        return None
    try:
        db_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)
        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """)
        return db_pool
    except Exception as e:
        log.error("Не удалось создать пул БД: %s", e)
        db_pool = None
        return None

async def ensure_user(user_id: int) -> None:
    pool = await get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING;",
                user_id,
            )
    except Exception as e:
        log.warning("ensure_user: %s", e)

# ================== QUESTIONS ==================
def load_questions() -> None:
    """Пробуем загрузить из questions.json; если нет — подставляем мини-набор."""
    global QUESTIONS
    path = os.path.join(os.path.dirname(__file__), "questions.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            QUESTIONS = json.load(f)
            log.info("Загружено вопросов из questions.json")
            return
    # Fallback набор (демо)
    QUESTIONS = {
        "geo": {
            "easy": [
                {"q": "Столица Франции?", "options": ["Париж", "Берлин", "Рим", "Мадрид"], "answer": 0},
                {"q": "Самая длинная река?", "options": ["Амазонка", "Нил", "Янцзы", "Миссисипи"], "answer": 1},
            ],
            "medium": [
                {"q": "Гора высотой 8848 м?", "options": ["Килиманджаро", "Монблан", "Эверест", "Аконкагуа"], "answer": 2},
            ],
            "hard": [
                {"q": "Столица Австралии?", "options": ["Сидней", "Мельбурн", "Канберра", "Перт"], "answer": 2},
            ],
        },
        "sci": {
            "easy": [
                {"q": "Химический символ воды?", "options": ["H2O", "O2", "CO2", "NaCl"], "answer": 0},
            ],
            "medium": [
                {"q": "Частица без заряда?", "options": ["Протон", "Нейтрон", "Электрон", "Позитрон"], "answer": 1},
            ],
            "hard": [
                {"q": "Постоянная Планка ≈ ?", "options": ["6.63e-34 Дж·с", "3e8 м/с", "1.6e-19 Кл", "9.81 м/с²"], "answer": 0},
            ],
        },
        "hist": {"easy": [], "medium": [], "hard": []},
        "movie": {"easy": [], "medium": [], "hard": []},
        "sport": {"easy": [], "medium": [], "hard": []},
        "it": {"easy": [], "medium": [], "hard": []},
    }
    log.info("Используется встроенный набор вопросов (demo).")

# ================== UI HELPERS ==================
def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = []
    for i in range(0, len(CATEGORIES), 2):
        left = CATEGORIES[i]
        right = CATEGORIES[i+1]
        buttons.append([
            InlineKeyboardButton(left[0], callback_data=f"cat|{left[1]}"),
            InlineKeyboardButton(right[0], callback_data=f"cat|{right[1]}"),
        ])
    return InlineKeyboardMarkup(buttons)

def difficulty_kb(cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Лёгкая (+5)", callback_data=f"diff|{cat}|easy"),
                InlineKeyboardButton("Средняя (+10)", callback_data=f"diff|{cat}|medium"),
            ],
            [
                InlineKeyboardButton("Сложная (+15)", callback_data=f"diff|{cat}|hard"),
                InlineKeyboardButton("⬅️ Назад", callback_data="back|menu"),
            ],
        ]
    )

def question_kb(options: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for idx, opt in enumerate(options):
        rows.append([InlineKeyboardButton(opt, callback_data=f"ans|{idx}")])
    rows.append([
        InlineKeyboardButton("💡 Подсказка (-10)", callback_data="hint"),
        InlineKeyboardButton("🔁 Сменить тему", callback_data="back|menu"),
    ])
    return InlineKeyboardMarkup(rows)

def hud_text(state: Dict[str, Any]) -> str:
    last = state.get("last_result", "")
    if last:
        last = f"{last}\n"
    return (
        f"{last}"
        f"🏆 Счёт: {state.get('score', 0)}\n"
        f"🔥 Комбо: {state.get('combo', 0)}\n"
        f"✔️ Верных: {state.get('correct', 0)}  ❌ Ошибок: {state.get('wrong', 0)}\n"
        f"💡 Подсказок: {state.get('hints', 5)}"
    )

# ================== STATE HELPERS ==================
def get_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    ud = context.user_data
    if "inited" not in ud:
        ud.update({
            "inited": True,
            "score": 0,
            "combo": 0,
            "correct": 0,
            "wrong": 0,
            "hints": 5,  # старт
            "cat": None,
            "diff": None,
            "used": set(),  # (cat, diff, index)
            "q_msg_id": None,
            "hud_msg_id": None,
            "current": None,  # tuple(cat, diff, idx)
            "last_result": "",
        })
    return ud

def pick_question(cat: str, diff: str, used: set) -> Optional[Tuple[int, Dict[str, Any]]]:
    pool = QUESTIONS.get(cat, {}).get(diff, [])
    candidates = [ (i, q) for i, q in enumerate(pool) if (cat, diff, i) not in used ]
    if not candidates:
        return None
    return random.choice(candidates)

# ================== CORE SENDING ==================
async def update_hud(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(context)
    chat_id = update.effective_chat.id
    hud_id = state.get("hud_msg_id")

    text = hud_text(state)
    if hud_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=hud_id,
                text=text
            )
            return
        except Exception as e:
            log.debug("edit hud failed, will resend: %s", e)

    msg = await context.bot.send_message(chat_id=chat_id, text=text)
    state["hud_msg_id"] = msg.message_id

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(context)
    state["cat"] = None
    state["diff"] = None
    state["last_result"] = ""
    await update_hud(update, context)
    await update.effective_message.reply_text(
        "Выбери категорию:",
        reply_markup=main_menu_kb()
    )

async def send_difficulties(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: str) -> None:
    await update.effective_message.edit_text(
        f"Категория выбрана. Теперь выбери сложность:",
        reply_markup=difficulty_kb(cat)
    )

async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, *, force_new_message: bool=False) -> None:
    state = get_state(context)
    cat = state.get("cat")
    diff = state.get("diff")
    used = state.get("used")

    pick = pick_question(cat, diff, used)
    if not pick:
        state["last_result"] = "🎉 Вопросы в этой теме закончились. Выбери другую категорию."
        await update_hud(update, context)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Темы закончились. Выбери другую категорию:",
            reply_markup=main_menu_kb()
        )
        return

    idx, q = pick
    state["current"] = (cat, diff, idx)

    text = f"🎯 Тема: {cat} • Сложность: {diff}\n\n❓ {q['q']}"
    kb = question_kb(q["options"])

    chat_id = update.effective_chat.id
    q_msg_id = state.get("q_msg_id")

    if q_msg_id and not force_new_message:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=q_msg_id,
                text=text,
                reply_markup=kb
            )
            return
        except Exception as e:
            log.debug("edit question failed, will send new: %s", e)

    sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    state["q_msg_id"] = sent.message_id

# ================== HANDLERS ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await ensure_user(user_id)

    # reset session stats
    ud = context.user_data
    ud.clear()
    state = get_state(context)  # re-init

    # приветствие без автозапуска вопроса
    await update.message.reply_text(
        "Привет! Это квиз-бот. Выбирай категорию и сложность, отвечай на вопросы и набирай очки.\n"
        "За 3 правильных подряд — бонус: +5 баллов и +1 подсказка."
    )
    await update_hud(update, context)
    await update.message.reply_text("Выбери категорию:", reply_markup=main_menu_kb())

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    state = get_state(context)

    if data.startswith("back|menu"):
        await send_menu(update, context)
        return

    if data.startswith("cat|"):
        _, cat = data.split("|", 1)
        state["cat"] = cat
        await send_difficulties(update, context, cat)
        return

    if data.startswith("diff|"):
        _, cat, diff = data.split("|", 2)
        state["cat"] = cat
        state["diff"] = diff
        await ask_next_question(update, context)
        return

    if data == "hint":
        if state.get("hints", 0) <= 0:
            state["last_result"] = "😕 Подсказок больше нет."
            await update_hud(update, context)
            return
        cur = state.get("current")
        if not cur:
            return
        cat, diff, idx = cur
        q = QUESTIONS[cat][diff][idx]
        ans_idx = q["answer"]
        first = q["options"][ans_idx][0]
        state["hints"] -= 1
        state["score"] -= 10
        state["last_result"] = f"💡 Подсказка: ответ начинается на «{first}». (-10 очков)"
        await update_hud(update, context)
        return

    if data.startswith("ans|"):
        _, chosen_str = data.split("|", 1)
        chosen = int(chosen_str)

        cur = state.get("current")
        if not cur:
            return
        cat, diff, idx = cur
        q = QUESTIONS[cat][diff][idx]
        correct_idx = q["answer"]
        points = DIFFICULTY_POINTS.get(diff, 5)

        used = state["used"]
        used.add((cat, diff, idx))

        if chosen == correct_idx:
            state["score"] += points
            state["combo"] += 1
            state["correct"] += 1
            combo_bonus_text = ""
            if state["combo"] % 3 == 0:
                state["score"] += 5
                state["hints"] += 1
                combo_bonus_text = " (+5 баллов, +1 подсказка)"
            state["last_result"] = f"✅ Правильно! +{points}\n🔥 Комбо: {state['combo']}{combo_bonus_text}"
        else:
            state["combo"] = 0
            state["wrong"] += 1
            right = q["options"][correct_idx]
            state["last_result"] = f"❌ Неверно. Правильный ответ: {right}"

        await update_hud(update, context)
        await ask_next_question(update, context)

# ================== BUILD & RUN ==================
def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

def build_app(bot_token: str) -> Application:
    app = Application.builder().token(bot_token).build()
    register_handlers(app)
    return app

def main():
    bot_token = os.getenv("BOT_TOKEN")
    db_url = os.getenv("DATABASE_URL")

    print("=== DEBUG STARTUP ===")
    print("BOT_TOKEN:", (bot_token[:10] + "…") if bot_token else "❌ not found")
    print("DATABASE_URL:", (db_url[:30] + "…") if db_url else "❌ not found")
    print("=====================")

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не найден")

    load_questions()

    # Создаём и назначаем event loop до run_polling (важно для Python 3.12)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = build_app(bot_token)
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
