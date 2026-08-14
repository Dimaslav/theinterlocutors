import os
import time
import requests
import telebot
from telebot import types
from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Список бесплатных моделей. Если первая перегружена (429), бот автоматически попробует следующую.
FREE_MODELS = [
    "google/gemma-4-31b-it:free",
    "google/gemma-2-9b-it:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "mistralai/mistral-7b-instruct:free"
]

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Словарь с ролями и их системными промптами
PERSONAS = {
    "friend": {
        "name": "🤝 Друг",
        "prompt": "Ты — лучший друг пользователя. Ты общаешься на равных, неофициально, используешь сленг (но в меру), поддерживаешь шутки, интересуешься жизнью. Твой тон теплый, дружеский и легкий."
    },
    "psychologist": {
        "name": "🛋️ Психолог",
        "prompt": "Ты — эмпатичный и внимательный психолог. Ты не даешь прямых указаний, а помогаешь пользователю разобраться в своих чувствах через наводящие вопросы. Твой тон спокойный, поддерживающий и безопасный. Ты используешь техники активного слушания."
    },
    "girlfriend": {
        "name": "❤️ Девушка",
        "prompt": "Ты — заботливая и нежная девушка пользователя. Ты проявляешь любовь, используешь ласковые слова, интересуешься днем пользователя, поддерживаешь его и создаешь атмосферу уюта и романтики в общении."
    },
    "boyfriend": {
        "name": "💙 Парень",
        "prompt": "Ты — надежный и любящий парень пользователя. Ты уверен в себе, проявляешь заботу, защищаешь и поддерживаешь. Общаешься мужественно, но с нежностью к пользователю, используешь легкий юмор."
    }
}

# Хранилище данных пользователей в оперативной памяти
user_data = {}

def get_user_data(user_id):
    """Инициализация данных пользователя при первом запуске"""
    if user_id not in user_data:
        user_data[user_id] = {
            "persona": "friend", # Роль по умолчанию
            "history": []
        }
    return user_data[user_id]

def ask_openrouter(user_id, user_text):
    """Функция запроса к нейросети с автопереключением моделей при перегрузке"""
    data = get_user_data(user_id)
    persona_key = data["persona"]
    system_prompt = PERSONAS[persona_key]["prompt"]
    
    # Формируем историю сообщений
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

    # Перебираем модели, если какая-то выдает ошибку перегрузки (429)
    for model_name in FREE_MODELS:
        payload["model"] = model_name
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            
            # Если сервер перегружен (429), пробуем следующую модель
            if response.status_code == 429:
                continue
                
            response.raise_for_status()
            ai_reply = response.json()['choices'][0]['message']['content']
            
            # Сохраняем контекст (ограничиваем 10 последними сообщениями)
            data["history"].append({"role": "user", "content": user_text})
            data["history"].append({"role": "assistant", "content": ai_reply})
            
            if len(data["history"]) > 10:
                data["history"] = data["history"][-10:]
                
            return ai_reply

        except requests.exceptions.HTTPError as e:
            # Если другая ошибка (например 404 - модель не найдена), тоже идем к следующей
            continue
        except Exception as e:
            return f"⚠️ Произошла системная ошибка: {str(e)}"

    # Если все бесплатные модели вернули 429
    return "⏳ Все бесплатные нейросети сейчас сильно перегружены. Пожалуйста, подожди минуту и попробуй написать снова."


# --- Клавиатуры ---

def get_main_keyboard():
    """Главная клавиатура под чатом"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_personas = types.KeyboardButton('🎭 Сменить роль')
    btn_clear = types.KeyboardButton('🗑 Очистить память')
    markup.row(btn_personas, btn_clear)
    return markup

def get_personas_keyboard():
    """Инлайн клавиатура для выбора роли"""
    markup = types.InlineKeyboardMarkup()
    for key, value in PERSONAS.items():
        markup.add(types.InlineKeyboardButton(text=value["name"], callback_data=f"set_persona_{key}"))
    return markup


# --- Хендлеры (обработчики сообщений) ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    data = get_user_data(message.from_user.id)
    welcome_text = f"Привет! Я твой ИИ-собеседник.\n\nТекущая роль: *{PERSONAS[data['persona']]['name']}*\n\nПросто напиши мне что-нибудь, или выбери кнопку ниже, чтобы поменять роль."
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
        data["history"] = [] # При смене роли очищаем историю
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
    
    ai_response = ask_openrouter(user_id, user_text)
    bot.send_message(message.chat.id, ai_response)


if __name__ == "__main__":
    print("Бот запущен и готов к общению...")
    bot.polling(none_stop=True)