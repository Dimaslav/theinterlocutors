import os
import logging
import threading
import sqlite3
import requests
import telebot
from telebot import types
from telebot.handler_backends import State, StatesGroup
from dotenv import load_dotenv

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# --- ЗАГРУЗКА КЛЮЧЕЙ ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    logging.error("❌ ОШИБКА: Токены не найдены!")
    exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
http_session = requests.Session()

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
DB_LOCK = threading.Lock()
def init_db():
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db', check_same_thread=False)
        cursor = conn.cursor()
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                current_persona TEXT DEFAULT 'friend'
            )
        ''')
        # Таблица истории сообщений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица кастомных персонажей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS custom_personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                prompt TEXT
            )
        ''')
        conn.commit()
        conn.close()

init_db()

# --- СОСТОЯНИЯ ДЛЯ СОЗДАНИЯ ПЕРСОНАЖА ---
class CreatePersonaStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_prompt = State()

# --- НАСТРОЙКИ МОДЕЛЕЙ ---
PREFERRED_MODELS = [
    "google/gemma-2-9b-it:free",
    "qwen/qwen-2.5-7b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free"
]

def get_actual_free_models():
    try:
        resp = http_session.get("https://openrouter.ai/api/v1/models", timeout=(10, 30))
        resp.raise_for_status()
        all_models = resp.json().get("data", [])
        actual_free = [m for m in PREFERRED_MODELS if m in [model["id"] for model in all_models]]
        if not actual_free:
            actual_free = [m["id"] for m in all_models if m["id"].endswith(":free")][:5]
        logging.info(f"Актуальные бесплатные модели: {actual_free}")
        return actual_free
    except Exception as e:
        logging.error(f"Не удалось получить список моделей: {e}.")
        return PREFERRED_MODELS

FREE_MODELS = get_actual_free_models()

# --- СТАНДАРТНЫЕ РОЛИ (УЛУЧШЕННЫЕ ПРОМПТЫ) ---
DEFAULT_PERSONAS = {
    "friend": {
        "name": "🤝 Друг",
        "prompt": "Ты — близкий друг пользователя. Твой тон: легкий, поддерживающий, иногда с доброй иронией. Ты не просто отвечаешь на вопросы, а инициируешь темы, интересуешься жизнью (учеба, работа, хобби). Общайся на 'ты'. Помни, что ты ИИ, но веди себя максимально естественно, как живой человек в мессенджере."
    },
    "psychologist": {
        "name": "🛋️ Психолог",
        "prompt": "Ты — эмпатичный ИИ-психолог. Твоя задача — выслушать, поддержать и помочь разобраться в мыслях. Строгие правила: не ставь диагнозы, не назначай лекарства, не выдавай себя за лицензированного врача. Задавай открытые вопросы, используй рефрейминг и валидацию эмоций. Тон: спокойный, безопасный, безоценочный."
    },
    "girlfriend": {
        "name": "❤️ Девушка",
        "prompt": "Ты играешь роль заботливой, любящей девушки в формате текстового ролевого общения. Тон: нежный, теплый, с легким флиртом. Интересуйся днем партнера, поддерживай в трудные моменты, создавай иллюзию близости через текст. Строгие правила: ты ИИ, не предлагай реальных встреч, не обсуждай откровенный сексуальный контент."
    },
    "boyfriend": {
        "name": "💙 Парень",
        "prompt": "Ты играешь роль надежного, любящего парня. Тон: уверенный, заботливый, мужественный, но нежный с пользователем. Поддерживай, шути, проявляй инициативу в разговоре. Строгие правила: ты ИИ, не предлагай реальных встреч, избегай токсичности, обеспечивай эмоциональную поддержку."
    },
    "create": {
        "name": "🛠 Создать своего...",
        "prompt": "" # Заглушка
    }
}

# --- ФУНКЦИИ РАБОТЫ С БД ---
def get_or_create_user(user_id: int, username: str) -> str:
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        cursor.execute("SELECT current_persona FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        if not result:
            cursor.execute("INSERT INTO users (user_id, username, current_persona) VALUES (?, ?, 'friend')", (user_id, username))
            conn.commit()
            return "friend"
        conn.close()
        return result[0]

def set_user_persona(user_id: int, persona_key: str):
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET current_persona = ? WHERE user_id = ?", (persona_key, user_id))
        conn.commit()
        conn.close()

def get_user_persona(user_id: int, persona_key: str):
    # Если стандартная
    if persona_key in DEFAULT_PERSONAS:
        return DEFAULT_PERSONAS[persona_key]
    # Если кастомная (id формата custom_123)
    if persona_key.startswith("custom_"):
        custom_id = int(persona_key.replace("custom_", ""))
        with DB_LOCK:
            conn = sqlite3.connect('bot_database.db')
            cursor = conn.cursor()
            cursor.execute("SELECT name, prompt FROM custom_personas WHERE id = ? AND user_id = ?", (custom_id, user_id))
            result = cursor.fetchone()
            conn.close()
            if result:
                return {"name": f "👤 {result[0]}", "prompt": result[1]}
    return DEFAULT_PERSONAS["friend"] # Фолбэк

def get_user_custom_personas(user_id: int):
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM custom_personas WHERE user_id = ?", (user_id,))
        result = cursor.fetchall()
        conn.close()
        return result

def save_message(user_id: int, chat_id: int, role: str, content: str):
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (user_id, chat_id, role, content) VALUES (?, ?, ?, ?)",
                       (user_id, chat_id, role, content[:3500]))
        conn.commit()
        conn.close()

def get_user_history(user_id: int, chat_id: int, limit: int = 12):
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        # Берем последние N сообщений и разворачиваем их в хронологическом порядке
        cursor.execute("""
            SELECT role, content FROM messages 
            WHERE user_id = ? AND chat_id = ? 
            ORDER BY id DESC LIMIT ?
        """, (user_id, chat_id, limit))
        result = cursor.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(result)]

def clear_user_history(user_id: int, chat_id: int):
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        conn.commit()
        conn.close()

# --- ЛОГИКА OpenRouter ---
def ask_openrouter(user_id: int, chat_id: int, user_text: str, persona_key: str):
    persona = get_user_persona(user_id, persona_key)
    system_prompt = persona["prompt"]
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(get_user_history(user_id, chat_id))
    messages.append({"role": "user", "content": user_text[:3500]})

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me",
        "X-Title": "Telegram Persona Bot", 
    }
    
    payload = {
        "messages": messages,
        "temperature": 0.8,
    }

    for model_name in FREE_MODELS:
        payload["model"] = model_name
        try:
            response = http_session.post(url, headers=headers, json=payload, timeout=(10, 60))
            if response.status_code in (401, 402):
                return "❌ Ошибка авторизации или баланса OpenRouter."
            if response.status_code in (429, 404) or (500 <= response.status_code < 600):
                continue
                
            response.raise_for_status()
            body = response.json()
            ai_reply = body.get("choices", [{}])[0].get("message", {}).get("content")
            
            if not isinstance(ai_reply, str) or not ai_reply.strip():
                continue
                
            # Сохраняем в БД
            save_message(user_id, chat_id, "user", user_text)
            save_message(user_id, chat_id, "assistant", ai_reply)
                
            logging.info(f"Успешный ответ от модели: {model_name}")
            return ai_reply.strip()

        except Exception:
            continue

    return "⏳ Все нейросети сейчас недоступны. Попробуй позже."

# --- УТИЛИТЫ TELEGRAM ---
def send_long_message(chat_id, text):
    chunks = []
    while len(text) > 4000:
        split_at = text.rfind('\n', 0, 4000)
        if split_at == -1: split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    for chunk in chunks:
        bot.send_message(chat_id, chunk)

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton('🎭 Сменить роль'),
        types.KeyboardButton('🗑 Очистить память')
    )
    return markup

def get_personas_keyboard(user_id: int):
    markup = types.InlineKeyboardMarkup()
    # Стандартные
    for key, value in DEFAULT_PERSONAS.items():
        markup.add(types.InlineKeyboardButton(text=value["name"], callback_data=f"set_persona_{key}"))
    
    # Кастомные пользователя
    custom_personas = get_user_custom_personas(user_id)
    for c_id, c_name in custom_personas:
        markup.add(types.InlineKeyboardButton(text=f"👤 {c_name}", callback_data=f"set_persona_custom_{c_id}"))
        
    return markup

# --- ХЕНДЛЕРЫ ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username
    persona_key = get_or_create_user(user_id, username)
    persona = get_user_persona(user_id, persona_key)
    
    welcome_text = (
        f"Привет, {message.from_user.first_name}! Я твой ИИ-собеседник.\n\n"
        f"Текущая роль: *{persona['name']}*\n\n"
        f"Я запоминаю нашу переписку в базу данных. Чтобы я забыл всё, что мы обсуждали, нажми «Очистить память».\n"
        f"Также ты можешь *создать своего уникального персонажа* в меню «Сменить роль»."
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda message: message.text == '🎭 Сменить роль')
def change_persona_menu(message):
    bot.send_message(
        message.chat.id, 
        "Выбери, с кем ты хочешь поговорить, или создай своего:", 
        reply_markup=get_personas_keyboard(message.from_user.id)
    )

@bot.message_handler(func=lambda message: message.text == '🗑 Очистить память')
def clear_memory(message):
    clear_user_history(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, "✅ Память очищена! Я забыл историю наших сообщений в этом чате.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_persona_'))
def callback_set_persona(call):
    user_id = call.from_user.id
    persona_key = call.data.removeprefix("set_persona_")
    
    # Обработка создания
    if persona_key == "create":
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        msg = bot.send_message(call.message.chat.id, "Введи имя для нового персонажа (например: 'Мудрый дед' или 'Детектив'):")
        bot.register_next_step_handler(msg, process_persona_name)
        bot.answer_callback_query(call.id)
        return

    # Проверка существования роли
    is_valid = persona_key in DEFAULT_PERSONAS or persona_key.startswith("custom_")
    
    if is_valid:
        set_user_persona(user_id, persona_key)
        persona = get_user_persona(user_id, persona_key)
        bot.answer_callback_query(call.id, text=f"Роль изменена на {persona['name']}")
        if call.message:
            bot.edit_message_text(
                f"Готово! Теперь я: *{persona['name']}*.\nНапиши мне что-нибудь!",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown"
            )

# --- ЛОГИКА СОЗДАНИЯ ПЕРСОНАЖА ---
def process_persona_name(message):
    name = message.text
    if len(name) > 50:
        bot.send_message(message.chat.id, "Слишком длинное имя. Попробуй еще раз.")
        return
    
    msg = bot.send_message(
        message.chat.id, 
        f"Отлично, имя: *{name}*.\nТеперь опиши его характер и манеру общения.\n"
        f"Например: 'Ты ворчливый, но добрый кот, который любит есть рыбу и давать саркастичные советы.'"
    )
    bot.register_next_step_handler(msg, process_persona_prompt, name)

def process_persona_prompt(message, name):
    prompt = message.text
    if len(prompt) > 1000:
        bot.send_message(message.chat.id, "Слишком длинное описание. Ограничься 1000 символов.")
        return

    user_id = message.from_user.id
    # Сохраняем в БД
    with DB_LOCK:
        conn = sqlite3.connect('bot_database.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO custom_personas (user_id, name, prompt) VALUES (?, ?, ?)", (user_id, name, prompt))
        custom_id = cursor.lastrowid
        conn.commit()
        conn.close()

    # Автоматически переключаем на новую роль
    set_user_persona(user_id, f"custom_{custom_id}")
    
    bot.send_message(
        message.chat.id, 
        f"✅ Персонаж *{name}* создан и выбран для общения!\nНапиши мне что-нибудь.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(content_types=["text"])
def handle_message(message):
    if not message.text:
        return
        
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_text = message.text

    logging.info(f"Message received: chat_id={chat_id} user_id={user_id} chars={len(user_text)}")

    try:
        bot.send_chat_action(chat_id, "typing")
        persona_key = get_or_create_user(user_id, message.from_user.username)
        ai_response = ask_openrouter(user_id, chat_id, user_text, persona_key)
        send_long_message(chat_id, ai_response)
    except Exception as e:
        logging.exception("Telegram handler failed")
        try:
            bot.send_message(chat_id, "⚠️ Не удалось обработать сообщение. Попробуй позже.")
        except Exception:
            pass

if __name__ == "__main__":
    logging.info("🚀 Бот запущен. База данных инициализирована.")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
