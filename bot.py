# bot.py
import os
import json
import asyncio
import logging
import random
import hashlib
from typing import Dict, List, Optional, Tuple

import asyncpg
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Message,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ===================== ЛОГИ =====================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("quiz-bot")

# ===================== КОНФИГ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions.json")

# очки за сложность
POINTS = {"easy": 5, "medium": 10, "hard": 15}
# русские подписи сложностей
RUS_DIFF = {"easy": "Лёгкий", "medium": "Средний", "hard": "Сложный"}

# 10 категорий — две колонки по 5
CATEGORIES: List[Tuple[str, str]] = [
    ("История 📜", "История"),
    ("География 🌍", "География"),
    ("Астрономия 🌌", "Астрономия"),
    ("Биология 🧬", "Биология"),
    ("Кино 🎬", "Кино"),
    ("Музыка 🎵", "Музыка"),
    ("Литература 📚", "Литература"),
    ("Наука 🔬", "Наука"),
    ("Искусство 🎨", "Искусство"),
    ("Техника ⚙️", "Техника"),
]

# Глобальный пул БД
db_pool: Optional[asyncpg.pool.Pool] = None

# ===================== МОДЕЛЬ ВОПРОСОВ =====================
class Question:
    def __init__(self, category: str, difficulty: str, question: str, options: List[str], answer: str):
        self.category = category
        self.difficulty = difficulty
        self.question = question
        self.options = options
        self.answer = answer
        # детерминированный id (для анти-повторов)
        h = hashlib.sha256(
            f"{category}|{difficulty}|{question}|{answer}".encode("utf-8")
        ).hexdigest()
        self.qid = h

def load_questions(path: str) -> List[Question]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    qs: List[Question] = []
    for item in raw.get("questions", []):
        cat = item.get("category", "").strip()
        diff = item.get("difficulty", "").strip().lower()  # "easy"/"medium"/"hard"
        qtext = item.get("question", "").strip()
        options = list(item.get("options", []))
        answer = item.get("answer", "").strip()
        if not (cat and diff in ("easy", "medium", "hard") and qtext and options and answer):
            continue
        if answer not in options:
            # если вдруг в json опечатка — пропускаем
            continue
        qs.append(Question(cat, diff, qtext, options, answer))
    log.info("Загружено вопросов: %s", len(qs))
    return qs

QUESTIONS: List[Question] = load_questions(QUESTIONS_FILE)

# Быстрые индексы: (cat, diff) -> список вопросов
QUEST_IDX: Dict[Tuple[str, str], List[Question]] = {}
for q in QUESTIONS:
    QUEST_IDX.setdefault((q.category, q.difficulty), []).append(q)

# ===================== БД =====================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  score INTEGER NOT NULL DEFAULT 0,
  combo INTEGER NOT NULL DEFAULT 0,
  total_correct INTEGER NOT NULL DEFAULT 0,
  total_wrong INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS used_questions (
  user_id BIGINT NOT NULL,
  qid TEXT NOT NULL,
  PRIMARY KEY (user_id, qid)
);
"""

async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL не задан — БОТ НЕ СМОЖЕТ СОХРАНЯТЬ ПРОГРЕСС!")
        return
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        for stmt in CREATE_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(s + ";")
    log.info("База инициализирована")

async def ensure_user(user_id: int):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        rec = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", user_id)
        if not rec:
            await conn.execute("INSERT INTO users(user_id) VALUES($1)", user_id)

async def db_get_score(user_id: int) -> int:
    if not db_pool:
        return 0
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT score FROM users WHERE user_id=$1", user_id)
        return int(row["score"]) if row else 0

async def db_get_progress(user_id: int) -> Tuple[int, int, int, int]:
    """score, combo, total_correct, total_wrong"""
    if not db_pool:
        return 0, 0, 0, 0
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT score, combo, total_correct, total_wrong FROM users WHERE user_id=$1",
            user_id,
        )
        if not row:
            return 0, 0, 0, 0
        return int(row["score"]), int(row["combo"]), int(row["total_correct"]), int(row["total_wrong"])

async def db_add_points(user_id: int, delta: int):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET score = score + $1 WHERE user_id=$2", delta, user_id)

async def db_set_combo(user_id: int, value: int):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET combo=$1 WHERE user_id=$2", value, user_id)

async def db_inc_correct(user_id: int):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET total_correct = total_correct + 1 WHERE user_id=$1", user_id)

async def db_inc_wrong(user_id: int):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET total_wrong = total_wrong + 1 WHERE user_id=$1", user_id)

async def db_mark_used(user_id: int, qid: str):
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO used_questions(user_id, qid) VALUES($1, $2) ON CONFLICT DO NOTHING",
            user_id, qid
        )

async def db_is_used(user_id: int, qid: str) -> bool:
    if not db_pool:
        return False
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_questions WHERE user_id=$1 AND qid=$2",
            user_id, qid
        )
        return bool(row)

# ===================== УТИЛИТЫ UI =====================
def chunk_buttons(buttons: List[InlineKeyboardButton], per_row: int) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i+per_row] for i in range(0, len(buttons), per_row)]

def main_menu_kb(score: int) -> InlineKeyboardMarkup:
    # две колонки по 5
    cat_buttons = [InlineKeyboardButton(title, callback_data=f"cat|{cat}")
                   for title, cat in CATEGORIES]
    rows = chunk_buttons(cat_buttons, 2)
    # нижние кнопки
    rows.append([
        InlineKeyboardButton("📊 Прогресс", callback_data="progress"),
        InlineKeyboardButton("❓ Правила", callback_data="rules"),
    ])
    return InlineKeyboardMarkup(rows)

def diff_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Лёгкий", callback_data="diff|easy"),
            InlineKeyboardButton("Средний", callback_data="diff|medium"),
            InlineKeyboardButton("Сложный", callback_data="diff|hard"),
        ],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
    ])

def playing_kb(options: List[str], disabled: Optional[List[int]]=None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    disabled = disabled or []
    for idx, text in enumerate(options):
        if idx in disabled:
            rows.append([InlineKeyboardButton(f"🚫 {text}", callback_data="noop")])
        else:
            rows.append([InlineKeyboardButton(text, callback_data=f"ans|{idx}")])
    # нижний ряд
    rows.append([
        InlineKeyboardButton("🧠 Подсказка (-10)", callback_data="hint"),
        InlineKeyboardButton("⬅️ В меню", callback_data="menu"),
    ])
    return InlineKeyboardMarkup(rows)

# ===================== СОСТОЯНИЕ ИГРОКА (в памяти) =====================
# user_data поля:
#   "cat": выбранная категория (рус)
#   "diff": "easy"/"medium"/"hard"
#   "current": dict(
#       qid, question, options (shuffled), correct_idx, hinted(bool), disabled_idx [..],
#       msg_id (сообщение с вопросом), chat_id, timer_task(asyncio.Task), expires_at
#   )

def reset_current(context: ContextTypes.DEFAULT_TYPE):
    if "current" in context.user_data:
        cur = context.user_data["current"]
        task: Optional[asyncio.Task] = cur.get("timer_task")
        if task and not task.done():
            task.cancel()
    context.user_data.pop("current", None)

# ===================== ЛОГИКА =====================
async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню: показывает текущий счёт и категории."""
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
    kb = main_menu_kb(score)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )
    # при входе в меню — убираем активный таймер
    reset_current(context)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(update, context)

async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await ensure_user(user_id)
    score, combo, tc, tw = await db_get_progress(user_id)
    await update.message.reply_text(
        f"📊 Ваш счёт: {score}\nКомбо: {combo}\nПравильных: {tc}, Неправильных: {tw}"
    )

async def rules_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = (
        "📜 *Правила*\n\n"
        "• Выберите категорию и сложность (Лёгкий/Средний/Сложный).\n"
        "• На ответ — 30 секунд. Обратный отсчёт идёт в том же сообщении.\n"
        "• Баллы: 5 / 10 / 15 за лёгкий/средний/сложный.\n"
        "• Подсказка скрывает 2 неверных варианта и отнимает 10 баллов (1 раз на вопрос).\n"
        "• Комбо: каждые 3 правильных подряд дают +5 дополнительных баллов.\n"
        "• Вопросы не повторяются.\n"
        "• Кнопка «В меню» — в любой момент."
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
    ]))

async def progress_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(update, context)

async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await send_menu(update, context)

async def category_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cat = q.data.split("|", 1)
    context.user_data["cat"] = cat
    context.user_data.pop("diff", None)
    reset_current(context)
    await q.edit_message_text(
        text=f"Категория: *{cat}*\nВыберите сложность:",
        parse_mode="Markdown",
        reply_markup=diff_kb()
    )

async def difficulty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, diff = q.data.split("|", 1)
    context.user_data["diff"] = diff
    # сразу задаём первый вопрос
    await ask_new_question(update, context)

def build_question_text(cur: dict) -> str:
    remain = max(0, int(cur["expires_at"] - asyncio.get_event_loop().time()))
    timer = f"⏳ {remain:02d}s"
    return f"*{timer}*\n\n{cur['question']}"

async def ask_new_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрать новый вопрос (той же категории/сложности) и показать."""
    qobj = update.callback_query
    user_id = update.effective_user.id
    cat = context.user_data.get("cat")
    diff = context.user_data.get("diff")

    if not cat or not diff:
        # чего-то не выбрано — в меню
        await send_menu(update, context)
        return

    await ensure_user(user_id)

    pool = QUEST_IDX.get((cat, diff), [])
    # отфильтровать уже использованные
    available = []
    for q in pool:
        used = await db_is_used(user_id, q.qid)
        if not used:
            available.append(q)

    if not available:
        msg = (
            f"❗ Вопросы закончились в категории *{cat}*, сложность *{RUS_DIFF[diff]}*.\n"
            f"Выберите другую сложность или категорию."
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 Лёгкий", callback_data="diff|easy"),
                InlineKeyboardButton("🟠 Средний", callback_data="diff|medium"),
                InlineKeyboardButton("🔴 Сложный", callback_data="diff|hard"),
            ],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
        ])
        if qobj:
            await qobj.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        else:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        reset_current(context)
        return

    q = random.choice(available)

    # подготовить варианты: один раз перемешать и запомнить порядок
    options = list(q.options)
    random.shuffle(options)
    correct_idx = options.index(q.answer)

    # если уже висел таймер — отменим
    reset_current(context)

    # отправить (или отредактировать) сообщение с вопросом
    text = build_question_text({
        "question": q.question,
        "expires_at": asyncio.get_event_loop().time() + 30
    })
    reply_markup = playing_kb(options)

    if qobj:
        msg: Message = await qobj.edit_message_text(
            text=text, parse_mode="Markdown", reply_markup=reply_markup
        )
    else:
        msg: Message = await update.message.reply_text(
            text=text, parse_mode="Markdown", reply_markup=reply_markup
        )

    # сохранить состояние текущего вопроса
    now = asyncio.get_event_loop().time()
    cur = {
        "qid": q.qid,
        "question": q.question,
        "options": options,
        "correct_idx": correct_idx,
        "hinted": False,
        "disabled_idx": [],
        "msg_id": msg.message_id,
        "chat_id": msg.chat_id,
        "expires_at": now + 30,
        "cat": cat,
        "diff": diff,
    }
    context.user_data["current"] = cur

    # стартовать таймер (обновляет один и тот же месседж без спама)
    task = asyncio.create_task(timer_task(context.application, context, cur))
    cur["timer_task"] = task

async def timer_task(app: Application, context: ContextTypes.DEFAULT_TYPE, cur: dict):
    """Обратный отсчёт в одном сообщении. По истечении — фиксируем неверный ответ и спрашиваем следующий."""
    try:
        while True:
            remain = int(cur["expires_at"] - asyncio.get_event_loop().time())
            if remain <= 0:
                # время вышло
                await on_time_out(app, context, cur)
                return
            # обновить сообщение текста (не трогаем клавиатуру)
            try:
                await app.bot.edit_message_text(
                    chat_id=cur["chat_id"],
                    message_id=cur["msg_id"],
                    text=build_question_text(cur),
                    parse_mode="Markdown",
                    reply_markup=playing_kb(cur["options"], cur.get("disabled_idx", []))
                )
            except Exception:
                # если вдруг «Message is not modified» или гонки — молча пропустим
                pass
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        # таймер отменён — нормально
        return

async def on_time_out(app: Application, context: ContextTypes.DEFAULT_TYPE, cur: dict):
    """Когда время истекает: штрафа нет, просто засчитываем неправильно и идём дальше."""
    user_id = context.user_data.get("user_id_cache")
    if not user_id and context._user_id:
        user_id = context._user_id
    if user_id:
        await db_inc_wrong(user_id)
        await db_set_combo(user_id, 0)

    # показать «время вышло»
    text = f"*⏰ Время вышло!*\n\n{cur['question']}"
    try:
        await context.bot.edit_message_text(
            chat_id=cur["chat_id"],
            message_id=cur["msg_id"],
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➡️ Следующий вопрос", callback_data="next")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
            ])
        )
    except Exception:
        pass

    # убираем текущий (таймер уже сам завершается)
    context.user_data.pop("current", None)

async def answer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cur = context.user_data.get("current")
    if not cur:
        # уже нет активного — вероятно, время вышло
        await q.answer("Вопрос уже закрыт.", show_alert=False)
        return

    # остановить таймер
    task: Optional[asyncio.Task] = cur.get("timer_task")
    if task and not task.done():
        task.cancel()

    # какая кнопка нажата?
    _, idx_s = q.data.split("|", 1)
    try:
        idx = int(idx_s)
    except ValueError:
        return

    user_id = update.effective_user.id
    context.user_data["user_id_cache"] = user_id
    await ensure_user(user_id)

    correct = (idx == cur["correct_idx"])
    diff = cur["diff"]

    if correct:
        base = POINTS[diff]
        await db_add_points(user_id, base)
        await db_inc_correct(user_id)

        # инкремент комбо
        score, combo, _, _ = await db_get_progress(user_id)
        combo += 1
        await db_set_combo(user_id, combo)

        combo_bonus = 0
        extra_note = ""
        if combo % 3 == 0:
            combo_bonus = 5
            await db_add_points(user_id, combo_bonus)
            extra_note = f" + комбо +{combo_bonus}"

        await db_mark_used(user_id, cur["qid"])
        # Показываем «Правильно +X»
        text = f"✅ Правильно! +{base}{extra_note}\n\n{cur['question']}"
        await q.edit_message_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➡️ Следующий вопрос", callback_data="next")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
            ])
        )

    else:
        await db_inc_wrong(user_id)
        await db_set_combo(user_id, 0)
        await db_mark_used(user_id, cur["qid"])
        # Неправильно
        text = f"❌ Неправильно.\n\n{cur['question']}"
        await q.edit_message_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➡️ Следующий вопрос", callback_data="next")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
            ])
        )

    # очистить текущее состояние
    context.user_data.pop("current", None)

async def next_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # просто задаём следующий в той же паре (категория/сложность)
    await ask_new_question(update, context)

async def hint_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cur = context.user_data.get("current")
    if not cur:
        await q.answer("Подсказка недоступна.", show_alert=False)
        return
    if cur["hinted"]:
        await q.answer("Подсказка уже использована.", show_alert=False)
        return

    # отметить подсказку
    cur["hinted"] = True

    # снять 10 баллов
    user_id = update.effective_user.id
    await ensure_user(user_id)
    await db_add_points(user_id, -10)

    # выбрать 2 неверных и «заблокировать» их
    wrong_idx = [i for i in range(len(cur["options"])) if i != cur["correct_idx"]]
    to_disable = random.sample(wrong_idx, k=min(2, len(wrong_idx)))
    cur["disabled_idx"] = sorted(set(cur.get("disabled_idx", []) + to_disable))

    # перерисовать то же сообщение (без спама)
    remain = max(0, int(cur["expires_at"] - asyncio.get_event_loop().time()))
    text = build_question_text(cur)
    await q.edit_message_text(
        text=text,
        parse_mode="Markdown",
        reply_markup=playing_kb(cur["options"], cur["disabled_idx"])
    )

async def noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # нажатие по заблокированной кнопке — просто быстрый answer
    await update.callback_query.answer()

# ===================== РЕГИСТРАЦИЯ ХЕНДЛЕРОВ =====================
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("score", score_cmd))

    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(progress_cb, pattern=r"^progress$"))
    app.add_handler(CallbackQueryHandler(rules_cb, pattern=r"^rules$"))

    app.add_handler(CallbackQueryHandler(category_cb, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(difficulty_cb, pattern=r"^diff\|"))

    app.add_handler(CallbackQueryHandler(answer_cb, pattern=r"^ans\|"))
    app.add_handler(CallbackQueryHandler(hint_cb, pattern=r"^hint$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(next_cb, pattern=r"^next$"))

# ===================== СТАРТ =====================
async def startup(app: Application):
    await init_db()
    log.info("Бот готов.")

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    register_handlers(app)
    app.post_init = startup  # вызов init_db() после старта
    return app

def main():
    app = build_app()
    # polling на Render работает нормально
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
