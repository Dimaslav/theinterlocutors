import os
import logging
import requests
import telebot
from telebot import types
from dotenv import load_dotenv

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
# Это поможет видеть ошибки в консоли хостинга
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# --- ЗАГРУЗКА КЛЮЧЕЙ ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    logging.error("❌ ОШИБКА: Токены не найдены! Проверьте переменные окружения (.env или настройки хостинга).")
    exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# --- НАСТРОЙКИ НЕЙРОСЕТЕЙ ---
# Список бесплатных моделей. Бот перебирает их сверху вниз, если предыдущая занята (429).
FREE_MODELS = [
    "google/gemma-2-9b-it:free",
    "qwen/qwen-2.5-7b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "mistralai/mistral-7b-instruct:free"
]

# --- РОЛИ (ПЕРСОНЫ) ---
PERSONAS = {
    "friend": {
        "name": "🤝 Друг",
        "prompt": "Ты — лучший друг пользователя. Общайся на 'ты', неофициально, используй умеренный сленг, шути, интересуйся жизнью. Тон теплый и легкий."
    },
    "psychologist": {
        "name": "🛋️ Психолог",
        "prompt": "Ты — эмпатичный психолог. Не даешь прямых советов, а помогаешь разобраться в чувствах через наводящие вопросы. Тон спокойный, поддерживающий. Используешь активное слушание."
    },
    "girlfriend": {
        "name": "❤️ Девушка",
        "prompt": "Ты — заботливая и нежная девушка. Проявляй любовь, используй ласковые слова, поддерживай, создавай романтичную и уютную атмосферу."
    },
    "boyfriend": {
        "name": "💙 Парень",
        "prompt": "Ты — надежный и любящий парень. Уверен в себе, проявляешь заботу и защиту. Общаешься мужественно, но с нежностью, используешь легкий юмор."
    }
}

# Хранилище диалогов в оперативной памяти
user_data = {}

def get_user_data(user_id):
    if user_id not in user_data:
        user_data[user_id] = {"persona": "friend", "history": []}
    return user_data[user_id]


def ask_openrouter(user_id, user_text):
    """Запрос к OpenRouter с умной обработкой ошибок и автопереключением моделей"""
    data = get_user_data(user_id)
    persona_key = data["persona"]
    system_prompt = PERSONAS[persona_key]["prompt"]
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(data["history"])
    messages.append({"role": "user", "content": user_text})

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
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            # 401 - Неверный API ключ (нет смысла пробовать другие модели)
            if response.status_code == 401:
                logging.error("OpenRouter API Error: 401 Unauthorized. Неверный API ключ.")
                return "❌ Ошибка авторизации на OpenRouter. Проверьте ваш API ключ."
                
            # 402 - Закончились кредиты (хоть модели и free, бывает при блокировке)
            if response.status_code == 402:
                logging.error("OpenRouter API Error: 402 Payment Required.")
                return "❌ Ошибка: на балансе OpenRouter нет средств."
                
            # 429 - Перегрузка (пробуем следующую модель)
            if response.status_code == 429:
                logging.warning(f"Модель {model_name} перегружена (429). Пробуем следующую...")
                continue
                
            # 404 - Модель не найдена (пробуем следующую)
            if response.status_code == 404:
                logging.warning(f"Модель {model_name} не найдена (404). Пробуем следующую...")
                continue
                
            # Проверка на другие HTTP ошибки
            response.raise_for_status()
            
            ai_reply = response.json()['choices'][0]['message']['content']
            
            # Сохраняем контекст (оставляем последние 12 сообщений = 6 реплик)
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
            logging.error(f"Непредвиденная ошибка: {e}")
            return "⚠️ Произошла внутренняя ошибка. Попробуйте позже."

    return "⏳ Все бесплатные нейросети сейчас перегружены. Пожалуйста, подожди минуту и попробуй написать снова."


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


# --- ОБРАБОТЧИКИ TELEGRAM ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    data = get_user_data(message.from_user.id)
    welcome_text = (
        f"Привет! Я твой ИИ-собеседник.\n\n"
        f"Текущая роль: *{PERSONAS[data['persona']]['name']}*\n\n"
        f"Просто напиши мне что-нибудь, или выбери кнопку ниже, чтобы поменять роль."
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda message: message.text == '🎭 Сменить роль')
def change_persona_menu(message):
    bot.send_message(message.chat.id, "Выбери, с кем ты хочешь поговорить:", reply_markup=get_personas_keyboard())

@bot.message_handler(func=lambda message: message.text == '🗑 Очистить память')
def clear_memory(message):
    data = get_user_data(message.from_user.id)
    data["history"] = []
    bot.send_message(message.chat.id, "✅ Память очищена! Я забыл всё, что было до этого. Начинаем с чистого листа.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_persona_'))
def callback_set_persona(call):
    user_id = call.from_user.id
    data = get_user_data(user_id)
    
    persona_key = call.data.replace('set_persona_', '')
    
    if persona_key in PERSONAS:
        data["persona"] = persona_key
        data["history"] = [] # Очищаем историю при смене роли
        
        bot.answer_callback_query(call.id, text=f"Роль изменена на {PERSONAS[persona_key]['name']}")
        bot.edit_message_text(
            f"Готово! Теперь я: *{PERSONAS[persona_key]['name']}*.\nНапиши мне что-нибудь!",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    user_text = message.text
    
    bot.send_chat_action(message.chat.id, 'typing')
    
    logging.info(f"Пользователь {user_id} написал: {user_text[:50]}...")
    
    ai_response = ask_openrouter(user_id, user_text)
    bot.send_message(message.chat.id, ai_response)


if __name__ == "__main__":
    logging.info("🚀 Бот запущен и готов к общению...")
    # none_stop=True означает, что бот не перестанет работать при получении ошибки от Telegram
    bot.infinity_polling(none_stop=True)
