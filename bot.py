import json
import random
import sqlite3
import asyncio
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# ================== НАСТРОЙКИ ==================
TOKEN = "8212049255:AAHoqDBpbx_O8cnwzQKZHfYxVJ2TblmT2xw"
DB_PATH = "quiz.db"
QUESTIONS_FILE = "questions.json"

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
DIFFICULTY_LABELS = {"easy": "Лёгкий 🙂", "medium": "Средний 😐", "hard": "Сложный 😎"}

# ================== ЗАГРУЗКА ВОПРОСОВ ==================
with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)["questions"]

# ================== БАЗА ДАННЫХ ==================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        score INTEGER DEFAULT 0,
        combo INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT score, combo FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO users (user_id, score, combo) VALUES (?, 0, 0)", (user_id,))
        conn.commit()
        row = (0, 0)
    conn.close()
    return {"score": row[0], "combo": row[1]}

def update_user(user_id, score=None, combo=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if score is not None:
        c.execute("UPDATE users SET score=? WHERE user_id=?", (score, user_id))
    if combo is not None:
        c.execute("UPDATE users SET combo=? WHERE user_id=?", (combo, user_id))
    conn.commit()
    conn.close()

# ================== ХЭЛПЕРЫ ==================
def main_menu(user_id=None):
    score_text = ""
    if user_id:
        user = get_user(user_id)
        score_text = f"\nТвой прогресс: {user['score']} очков"

    keyboard = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        for name, cat in CATEGORIES[i:i+2]:
            row.append(InlineKeyboardButton(name, callback_data=f"cat|{cat}"))
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard), score_text

def difficulty_menu(category):
    keyboard = [
        [InlineKeyboardButton(DIFFICULTY_LABELS["easy"], callback_data=f"diff|{category}|easy")],
        [InlineKeyboardButton(DIFFICULTY_LABELS["medium"], callback_data=f"diff|{category}|medium")],
        [InlineKeyboardButton(DIFFICULTY_LABELS["hard"], callback_data=f"diff|{category}|hard")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def question_keyboard(options, with_hint=True):
    keyboard = [[InlineKeyboardButton(opt, callback_data=f"ans|{opt}")] for opt in options]
    row = []
    if with_hint:
        row.append(InlineKeyboardButton("💡 Подсказка (-10)", callback_data="hint"))
    row.append(InlineKeyboardButton("⬅️ Меню", callback_data="menu"))
    keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)

# ================== ХЕНДЛЕРЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard, score_text = main_menu(update.message.from_user.id)
    await update.message.reply_text(
        "Привет! 👋 Добро пожаловать в Викторину!\nВыбирай категорию:" + score_text,
        reply_markup=keyboard
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "timer_task" in context.user_data:
        context.user_data["timer_task"].cancel()
    query = update.callback_query
    await query.answer()
    keyboard, score_text = main_menu(query.from_user.id)
    await query.edit_message_text("Главное меню. Выбирай категорию:" + score_text, reply_markup=keyboard)

async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, category = query.data.split("|")
    await query.answer()
    await query.edit_message_text(f"Ты выбрал категорию: {category}\nВыбери сложность:", 
                                  reply_markup=difficulty_menu(category))

async def difficulty_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, category, difficulty = query.data.split("|")
    await query.answer()

    user_data = context.user_data
    user_data["category"] = category
    user_data["difficulty"] = difficulty
    user_data.setdefault("asked", set())

    await ask_question(update, context)

async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    category = user_data["category"]
    difficulty = user_data["difficulty"]

    available = [q for q in QUESTIONS if q["category"].lower() == category
                 and q["difficulty"] == difficulty
                 and q["question"] not in user_data["asked"]]

    if not available:
        keyboard, score_text = main_menu(update.callback_query.from_user.id)
        await update.callback_query.edit_message_text(
            "Вопросы в этой категории закончились 😢" + score_text,
            reply_markup=keyboard
        )
        return

    question = random.choice(available)
    user_data["current_question"] = question
    user_data["asked"].add(question["question"])

    options = question["options"].copy()
    random.shuffle(options)
    user_data["options"] = options

    if "timer_task" in user_data:
        user_data["timer_task"].cancel()
    user_data["timer_task"] = asyncio.create_task(timer(update, context, 30))

    user = get_user(update.callback_query.from_user.id)
    await update.callback_query.edit_message_text(
        f"❓ {question['question']}\n⏳ Осталось: 30 секунд\nТекущий счёт: {user['score']}",
        reply_markup=question_keyboard(options)
    )

async def timer(update: Update, context: ContextTypes.DEFAULT_TYPE, seconds: int):
    query = update.callback_query
    for i in range(seconds, 0, -1):
        try:
            await asyncio.sleep(1)
            user = get_user(query.from_user.id)
            await query.edit_message_text(
                f"❓ {context.user_data['current_question']['question']}\n⏳ Осталось: {i-1} секунд\nТекущий счёт: {user['score']}",
                reply_markup=question_keyboard(context.user_data["options"])
            )
        except:
            return
    keyboard, score_text = main_menu(query.from_user.id)
    await query.edit_message_text("⏰ Время вышло! Ответ не засчитан." + score_text, reply_markup=keyboard)

async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, ans = query.data.split("|")
    await query.answer()

    user_id = query.from_user.id
    question = context.user_data["current_question"]
    correct = question["answer"]

    context.user_data["timer_task"].cancel()

    user = get_user(user_id)
    score, combo = user["score"], user["combo"]

    if ans == correct:
        points = DIFFICULTY_POINTS[question["difficulty"]]
        combo += 1
        bonus_text = ""
        if combo >= 3:
            points += 5
            bonus_text = " (Комбо! +5)"
            combo = 0
        score += points
        update_user(user_id, score=score, combo=combo)

        await query.edit_message_text(f"✅ Правильно! +{points} очков{bonus_text}\nТвой счёт: {score}")
        await asyncio.sleep(1)
        await ask_question(update, context)
    else:
        combo = 0
        update_user(user_id, score=score, combo=combo)
        keyboard, score_text = main_menu(user_id)
        await query.edit_message_text(f"❌ Неверно! Правильный ответ: {correct}\nТвой счёт: {score}" + score_text,
                                      reply_markup=keyboard)

async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user = get_user(user_id)
    if user["score"] < 10:
        keyboard, score_text = main_menu(user_id)
        await query.edit_message_text("Недостаточно очков для подсказки!" + score_text, reply_markup=keyboard)
        return

    update_user(user_id, score=user["score"] - 10)

    question = context.user_data["current_question"]
    options = context.user_data["options"].copy()
    if question["answer"] in options:
        options.remove(question["answer"])
    remove = random.sample(options, min(2, len(options)))
    new_opts = [question["answer"]] + [o for o in options if o not in remove]
    random.shuffle(new_opts)
    context.user_data["options"] = new_opts

    user = get_user(user_id)
    await query.edit_message_text(
        f"❓ {question['question']}\n💡 Подсказка! Осталось {len(new_opts)} варианта\nТекущий счёт: {user['score']}",
        reply_markup=question_keyboard(new_opts, with_hint=False)
    )

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.message.from_user.id)
    await update.message.reply_text(f"Твой счёт: {user['score']}")

# ================== ЗАПУСК ==================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CallbackQueryHandler(category_chosen, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(difficulty_chosen, pattern=r"^diff\|"))
    app.add_handler(CallbackQueryHandler(answer, pattern=r"^ans\|"))
    app.add_handler(CallbackQueryHandler(hint, pattern=r"^hint$"))
    app.add_handler(CallbackQueryHandler(menu, pattern=r"^menu$"))

    app.run_polling()

if __name__ == "__main__":
    main()
