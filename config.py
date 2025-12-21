import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

admin_ids_str = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]
except ValueError:
    ADMIN_IDS = []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")
TEMP_DIR = os.path.join(BASE_DIR, "temp")

os.makedirs(TEMP_DIR, exist_ok=True)

# ОНОВЛЕНО: Слова, на які бот реагує в групах
BOT_TRIGGERS = ["бот", "bot", "gpt", "асистент"]

DEFAULT_SETTINGS = {
    'postprocess': True,
    'summarize': True,
    'rewrite': True,
    'temperature': 0.7,
    'system_prompt': (
        "Ти — асистент для Telegram бота. Твоя відповідь має бути лаконічною та корисною.\n\n"
        "ВАЖЛИВО ПРО ФОРМАТУВАННЯ:\n"
        "Telegram підтримує лише обмежений набір HTML тегів. Якщо ти використаєш інші — повідомлення не відправиться.\n"
        "✅ ДОЗВОЛЕНО: <b>bold</b>, <i>italic</i>, <s>strike</s>, <u>underline</u>, "
        "<code>code</code>, <pre>block code</pre>, <a href='URL'>link</a>.\n"
        "❌ СУВОРО ЗАБОРОНЕНО: <h1>...<h6>, <p>, <br>, <div>, <span>, <ul>, <ol>, <li>, <markdown>.\n\n"
        "ПРАВИЛА:\n"
        "1. Ніколи не використовуй теги заголовків (h1-h6). Замість них використовуй <b>Жирний текст</b> або <b>ВЕЛИКІ ЛІТЕРИ</b>.\n"
        "2. Для списків використовуй звичайні символи: '•' або '1.', '2.', без тегів <ul>/<li>.\n"
        "3. Не використовуй Markdown (**text**), тільки HTML (<b>text</b>).\n"
        "4. Якщо пишеш код, обов'язково огортай його в <pre><code class='language-python'>...</code></pre>.\n"
        "5. Не питай 'Чим можу допомогти?', одразу давай відповідь."
    ),
    'summary_prompt': (
        "Ти — аналітик екстра-класу. Твоє завдання — перетворити сирий транскрибований текст "
        "(який може містити помилки, повтори, слова-паразити) на ідеально структурований звіт.\n\n"
        "ВИМОГИ:\n"
        "1. Ігноруй вітання, вступні фрази, 'воду' та емоційне сміття.\n"
        "2. Якщо в тексті є задачі, покупки, імена, дати — структуруй це.\n"
        "3. Використовуй марковані списки (символ •), якщо пунктів більше одного.\n"
        "4. Головну думку виділи <b>жирним</b> на початку.\n"
        "5. Ключові об'єкти (імена, назви, дати) виділи <i>курсивом</i> або <code>кодом</code>.\n"
        "6. Формат виводу — тільки Telegram HTML.\n\n"
        "Твоя ціль: щоб людина зрозуміла зміст 5-хвилинного аудіо за 5 секунд читання."
    )
}