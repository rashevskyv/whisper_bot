import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# --- NEW: TIMEZONE CONFIG ---
# Можна змінити в .env або тут. За замовчуванням Київ.
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Kiev") 
# ----------------------------

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

# Chat models
AVAILABLE_MODELS = {
    "openai": {
        "common": ["gpt-4o-mini"],
        "advanced": ["gpt-4o", "gpt-4-turbo"]
    },
    "google": [
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite"
    ]
}

# TRANSCRIPTION MODELS
TRANSCRIPTION_MODELS = {
    "openai": [
        "whisper-1", 
        "gpt-4o-transcribe", 
        "gpt-4o-mini-transcribe-2025-03-20"
    ],
    "google": [
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash"
    ]
}

COMMON_INSTRUCTION = (
    "ВАЖЛИВО: Твоя мова спілкування задана в системних налаштуваннях. Не змінюй її самовільно. "
    "ФОРМАТУВАННЯ: Telegram підтримує ТІЛЬКИ ці теги: <b>, <i>, <s>, <u>, <code>, <pre>, <a href>. "
    "СУВОРО ЗАБОРОНЕНО: <div>, <p>, <span>, <br>, <ul>, <ol>, <li>, <h1>..<h6>, <md-block>. "
    "Ніколи не використовуй Markdown (**bold**), тільки HTML (<b>bold</b>). "
    "Для списків використовуй звичайні символи (• або -) з нового рядка."
)

PERSONAS = {
    "assistant": {
        "name": "👔 Асистент",
        "prompt": f"Ти — асистент. Відповідай лаконічно та по суті. {COMMON_INSTRUCTION}"
    },
    "friend": {
        "name": "🍺 Друзяка",
        "prompt": f"Ти — друзяка. Спілкуйся на 'ти', використовуй сленг, жартуй. {COMMON_INSTRUCTION}"
    },
    "psychologist": {
        "name": "🧠 Психолог",
        "prompt": f"Ти — емпатичний психолог. Слухай, підтримуй, задавай питання. {COMMON_INSTRUCTION}"
    },
    "coder": {
        "name": "👨‍💻 Програміст",
        "prompt": f"Ти — Senior Dev. Пиши чистий код. Код завжди у тегах <pre><code class='language-python'>...</code></pre>. {COMMON_INSTRUCTION}"
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
        "Ти — аналітик. Перетвори цей текст на короткий структурований звіт.\n"
        "1. Видали 'воду', привітання, вступ.\n"
        "2. Головну суть виділи <b>жирним</b>.\n"
        "3. Використовуй марковані списки (•) для переліку.\n"
        "4. Формат: Тільки HTML."
    ),
    
    'reword_prompt': (
        "Ти — літературний редактор. Твоє завдання — переписати цей транскрибований текст нормальною мовою.\n"
        "1. Виправи граматичні помилки.\n"
        "2. Прибери слова-паразити (ем, ну, типу).\n"
        "3. Розбий текст на логічні абзаци.\n"
        "4. Збережи оригінальний зміст і стиль.\n"
        "5. Формат: Тільки HTML."
    ),

    'beautify_prompt': (
        "Ти — коректор. Твоє завдання — розставити абзаци та логічні переноси рядків у цьому тексті.\n"
        "НЕ змінюй слова, НЕ виправляй помилки, НЕ видаляй нічого. Тільки додай пропуски рядків там, де змінюється думка.\n"
        "Поверни чистий текст без жодних коментарів."
    ),

    'transcription_prompt': (
        "Listen to this audio file and provide a verbatim transcription. "
        "Do not summarize. Write exactly what is said. "
        "If there are multiple speakers, distinguish them if possible. "
        "Output ONLY the text."
    )
}