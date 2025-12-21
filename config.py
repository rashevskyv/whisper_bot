import os
from dotenv import load_dotenv

# Завантажуємо змінні з .env файлу
load_dotenv()

# Основні налаштування
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

DEFAULT_SETTINGS = {
    'postprocess': True,
    'summarize': True,
    'rewrite': True,
    'temperature': 0.7,
    
    # Промпт для основного спілкування
    'system_prompt': (
        "Ти — асистент для Telegram бота. Твоя відповідь має бути лаконічною та корисною.\n"
        "ВАЖЛИВО: Використовуй тільки HTML теги (<b>, <i>, <code>, <pre>, <a>). "
        "Ніколи не використовуй Markdown (**text**) та заголовки <h>."
    ),

    # ОНОВЛЕНО: Спеціальний промпт для кнопки "Підсумувати"
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