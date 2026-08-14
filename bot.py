import os
import logging
import threading
import requests
import telebot
from telebot import types
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
    logging.error("❌ ОШИБКА: Токены не найдены! Проверьте переменные окружения.")
    exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
http_session = requests.Session()

# --- УМНОЕ ПОЛУЧЕНИЕ БЕСПЛАТНЫХ МОДЕЛЕЙ ---
PREFERRED_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
]

def get_actual_free_models():
    """Получает список актуальных бесплатных моделей с OpenRouter при старте"""
    try:
        resp = http_session.get("https://openrouter.ai/api/v1/models", timeout=(10, 30))
        resp.raise_for_status()
        all_models = resp.json().get("data", [])
        
        # Оставляем только те модели из нашего списка, которые реально существуют
        actual_free = [m for m in PREFERRED_MODELS if m in [model["id"] for model in all_models]]
        
        if not actual_free:
            logging.warning("Предпочитаемые модели не найдены. Используем любые доступные :free.")
            actual_free = [m["id"] for m in all_models if m["id"].endswith(":free")][:5]
            
        logging.info(f"Актуальные бесплатные модели: {actual_free}")
        return actual_free
    except Exception as e:
        logging.error(f"Не удалось получить список моделей: {e}. Используем запасной список.")
        return PREFERRED_MODELS

FREE_MODELS = get_actual_free_models()

# --- РОЛИ (БЕЗОПАСНЫЕ ПРОМПТЫ) ---
PERSONAS = {
    "friend": {
        "name": "🤝 Друг",
        "prompt": "Ты играешь роль лучшего друга пользователя. Общайся на 'ты', неофициально, используй умеренный сленг, шути, интересуйся жизнью. Тон теплый и легкий. Помни, что ты ИИ, который просто играет эту роль для поддержки."
    },
    "psychologist": {
        "name": "🛋️ Психолог",
        "prompt": "Ты поддерживающий ИИ-собеседник в стиле эмпатичного психолога. Не выдавай себя за лицензированного специалиста. Не ставь диагнозы. Помогай разобраться в чувствах через наводящие вопросы. Тон спокойный, поддерживающий. Используй техники активного слушания."
    },
    "girlfriend": {
        "name": "❤️ Девушка",
        "prompt": "Ты играешь роль заботливой и нежной девушки в рамках дружеского ролевого общения. Проявляй тепло, используй ласковые слова, поддерживай, создавай уютную атмосферу. Не предлагай реальных встреч, помни, что ты ИИ-собеседник."
    },
    "boyfriend": {
        "name": "💙 Парень",
        "prompt": "Ты играешь роль надежного и любящего парня в рамках дружеского ролевого общения. Уверен в себе, проявляешь заботу и защиту. Общаешься мужественно, но с нежностью, используешь легкий юмор. Не предлагай реальных встреч, помни, что ты ИИ-собеседник."
    }
}

# --- ХРАНИЛИЩЕ И БЕЗОПАСНОСТЬ ПОТОКОВ ---
# Ключ: (chat_id, user_id) -> {"persona": str, "history": list}
user_data = {}
user_locks = {}
locks_mutex = threading.Lock()

def get_lock(key):
    with locks_mutex:
        if key not in user_locks:
            user_locks[key] = threading.Lock()
        return user_locks[key]

def get_user_data(key):
    if key not in user_data:
        user_data[key] = {"persona": "friend", "history": []}
    return user_data[key]


# --- ЛОГИКА OpenRouter ---
def ask_openrouter(key, user_text):
    data = get_user_data(key)
    persona_key = data["persona"]
    system_prompt = PERSONAS[persona_key]["prompt"]
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(data["history"])
    messages.append({"role": "user", "content": user_text[:3500]}) # Защита от огромных сообщений

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
            
            if response.status_code == 401:
                logging.error("OpenRouter API Error: 401 Unauthorized.")
                return "❌ Ошибка авторизации на OpenRouter. Проверьте API ключ."
            if response.status_code == 402:
                logging.error("OpenRouter API Error: 402 Payment Required.")
                return "❌ Ошибка: на балансе OpenRouter нет средств."
            if response.status_code in (429, 404) or (500 <= response.status_code < 600):
                logging.warning(f"Модель {model_name} недоступна ({response.status_code}). Пробуем следующую...")
                continue
                
            response.raise_for_status()
            
            # Безопасное извлечение ответа
            body = response.json()
            ai_reply = body.get("choices", [{}])[0].get("message", {}).get("content")
            
            if not isinstance(ai_reply, str) or not ai_reply.strip():
                logging.warning(f"Пустой или некорректный ответ от {model_name}. Пробуем следующую...")
                continue
                
            # Сохраняем контекст
            data["history"].append({"role": "user", "content": user_text})
            data["history"].append({"role": "assistant", "content": ai_reply})
            if len(data["history"]) > 12:
                data["history"] = data["history"][-12:]
                
            logging.info(f"Успешный ответ от модели: {model_name}")
            return ai_reply.strip()

        except requests.exceptions.Timeout:
            logging.warning(f"Таймаут при запросе к {model_name}. Пробуем следующую...")
            continue
        except Exception as e:
            logging.error(f"Непредвиденная ошибка при запросе к {model_name}: {e}")
            continue

    return "⏳ Все нейросети сейчас недоступны или перегружены. Пожалуйста, подожди минуту и попробуй снова."


# --- УТИЛИТЫ TELEGRAM ---
def send_long_message(chat_id, text):
    """Разбивает длинный текст на части по 4000 символов, стараясь не резать абзацы"""
    chunks = []
    while len(text) > 4000:
        split_at = text.rfind('\n', 0, 4000)
        if split_at == -1:
            split_at = 4000
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

def get_personas_keyboard():
    markup = types.InlineKeyboardMarkup()
    for key, value in PERSONAS.items():
        markup.add(types.InlineKeyboardButton(text=value["name"], callback_data=f"set_persona_{key}"))
    return markup


# --- ХЕНДЛЕРЫ ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    key = (message.chat.id, message.from_user.id)
    with get_lock(key):
        data = get_user_data(key)
        data["history"] = [] # Очищаем историю при /start
        
    welcome_text = (
        f"Привет! Я твой ИИ-собеседник.\n\n"
        f"Текущая роль: *{PERSONAS[data['persona']]['name']}*\n\n"
        f"Просто напиши мне что-нибудь, или выбери кнопку ниже, чтобы поменять роль.\n"
        f"Команда /start начинает диалог заново."
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda message: message.text == '🎭 Сменить роль')
def change_persona_menu(message):
    bot.send_message(message.chat.id, "Выбери, с кем ты хочешь поговорить:", reply_markup=get_personas_keyboard())

@bot.message_handler(func=lambda message: message.text == '🗑 Очистить память')
def clear_memory(message):
    key = (message.chat.id, message.from_user.id)
    with get_lock(key):
        data = get_user_data(key)
        data["history"] = []
    bot.send_message(message.chat.id, "✅ Память очищена! Я забыл историю сообщений. Роль осталась прежней.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_persona_'))
def callback_set_persona(call):
    key = (call.message.chat.id, call.from_user.id)
    
    persona_key = call.data.removeprefix("set_persona_")
    
    if persona_key in PERSONAS:
        with get_lock(key):
            data = get_user_data(key)
            data["persona"] = persona_key
            data["history"] = []
            
        bot.answer_callback_query(call.id, text=f"Роль изменена на {PERSONAS[persona_key]['name']}")
        if call.message:
            bot.edit_message_text(
                f"Готово! Теперь я: *{PERSONAS[persona_key]['name']}*.\nНапиши мне что-нибудь!",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown"
            )

@bot.message_handler(content_types=["text"])
def handle_message(message):
    if not message.text:
        return
        
    key = (message.chat.id, message.from_user.id)
    user_text = message.text

    # Приватность: не пишем текст в логи
    logging.info(f"Message received: chat_id={message.chat.id} user_id={message.from_user.id} chars={len(user_text)}")

    lock = get_lock(key)
    
    # Защита от спама: если поток уже занят этим пользователем
    if lock.locked():
        bot.send_message(message.chat.id, "⏳ Я ещё печатаю ответ на твое прошлое сообщение, подожди немного.")
        return

    with lock:
        try:
            bot.send_chat_action(message.chat.id, "typing")
            ai_response = ask_openrouter(key, user_text)
            send_long_message(message.chat.id, ai_response)
        except Exception as e:
            logging.exception("Telegram handler failed")
            try:
                bot.send_message(message.chat.id, "⚠️ Не удалось обработать сообщение. Попробуй позже.")
            except Exception:
                pass


if __name__ == "__main__":
    logging.info("🚀 Бот запущен и готов к общению...")
    # infinity_polling устойчив к сетевым сбоям Telegram
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
