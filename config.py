import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Kiev") 

admin_ids_str = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]
except ValueError:
    ADMIN_IDS = []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")
TEMP_DIR = os.path.join(BASE_DIR, "temp")

os.makedirs(TEMP_DIR, exist_ok=True)

BOT_TRIGGERS = ["бот", "bot", "gpt", "асистент"]

AVAILABLE_MODELS = {
    "openai": {
        "common": ["gpt-4o-mini"],
        "advanced": ["gpt-4o", "gpt-4-turbo"]
    },
    "google": [
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-2.0-flash-exp"
    ]
}

TRANSCRIPTION_MODELS = {
    "openai": ["whisper-1"],
    "google": ["gemini-1.5-flash"]
}

COMMON_INSTRUCTION = (
    "ВАЖЛИВО: Твоя мова спілкування задана в системних налаштуваннях. "
    "ФОРМАТУВАННЯ: Використовуй ТІЛЬКИ <b>, <i>, <code>, <pre>, <a>. "
    "СУВОРО ЗАБОРОНЕНО: Markdown (**bold**), <div>, <p>, <br>, <ul>, <li>. "
    "Для списків використовуй символ '•' на початку рядка. Не роби порожніх рядків між пунктами списку."
)

PERSONAS = {
    "assistant": {
        "name": "👔 Асистент",
        "prompt": f"Ти — лаконічний асистент. {COMMON_INSTRUCTION}"
    },
    "friend": {
        "name": "🍺 Друзяка",
        "prompt": f"Ти — друзяка. Спілкуйся на 'ти', жартуй. {COMMON_INSTRUCTION}"
    },
    "psychologist": {
        "name": "🧠 Психолог",
        "prompt": f"Ти — емпатичний психолог. {COMMON_INSTRUCTION}"
    },
    "coder": {
        "name": "👨‍💻 Програміст",
        "prompt": f"Ти — Senior Dev. Код у тегах <pre><code>...</code></pre>. {COMMON_INSTRUCTION}"
    }
}

DEFAULT_SETTINGS = {
    'postprocess': True,
    'summarize': True,
    'rewrite': True,
    'temperature': 0.7,
    'model': 'gpt-4o-mini',
    'transcription_model': 'whisper-1', 
    'language': 'uk',
    'system_prompt': PERSONAS['assistant']['prompt'],
    'allow_search': True,
    
    'summary_prompt': (
        "Ти — аналітик. Перетвори текст на стислий звіт.\n"
        "1. Видали вступ та 'воду'.\n"
        "2. Головну суть виділи <b>жирним</b>.\n"
        "3. Використовуй '•' для списків. Кожен пункт з нового рядка БЕЗ порожніх рядків між ними.\n"
        "4. Формат: Тільки чистий HTML без тегів p/div/br."
    ),
    
    'reword_prompt': (
        "Ти — редактор. Перепиши текст літературною мовою.\n"
        "1. Виправи помилки, прибери слова-паразити.\n"
        "2. Збережи зміст. Не додавай зайвих абзаців та відступів.\n"
        "3. Формат: Тільки чистий HTML."
    ),

    'beautify_prompt': (
        "Ти — коректор. Розстав абзаци у тексті.\n"
        "НЕ змінюй слова. Тільки додай ОДИН перенос рядка там, де змінюється думка.\n"
        "Не роби подвійних відступів. Поверни чистий текст."
    ),

    'transcription_prompt': (
        "Listen to this audio and provide a verbatim transcription. "
        "Output ONLY the text."
    )
}