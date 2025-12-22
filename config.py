import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Додано, якщо використовуєте Gemini

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

PERSONAS = {
    "assistant": {
        "name": "👔 Асистент",
        "prompt": "Ти — асистент. Відповідай лаконічно. Використовуй HTML (<b>, <i>, <code>, <a href='...'>)."
    },
    "friend": {
        "name": "🍺 Друзяка",
        "prompt": "Ти — друзяка. Спілкуйся на 'ти', використовуй сленг. Формат: HTML."
    },
    "psychologist": {
        "name": "🧠 Психолог",
        "prompt": "Ти — емпатичний психолог. Слухай, підтримуй, задавай питання. Формат: HTML."
    },
    "coder": {
        "name": "👨‍💻 Програміст",
        "prompt": "Ти — Senior Dev. Пиши чистий код. Використовуй <pre><code class='language-python'>...</code></pre>."
    }
}

DEFAULT_SETTINGS = {
    'postprocess': True,
    'summarize': True,
    'rewrite': True,
    'temperature': 0.7,
    'model': 'gpt-4o-mini',
    'language': 'uk', # ОНОВЛЕНО: Мова за замовчуванням
    'system_prompt': PERSONAS['assistant']['prompt'],
    'allow_search': True,
    
    'summary_prompt': (
        "Ти — аналітик. Перетвори сирий текст на структурований звіт.\n"
        "Використовуй списки (•) та <b>жирний шрифт</b> для головного."
    )
}