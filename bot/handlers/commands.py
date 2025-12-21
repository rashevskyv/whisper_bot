from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.utils.helpers import get_or_create_user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Реєстрація в БД
    db_user = await get_or_create_user(user)
    
    text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        f"Я — мульти-модельний AI бот.\n"
        f"Зараз я працюю на базі <b>GPT-4o</b>.\n\n"
        f"Що я вмію:\n"
        f"🔹 Транскрибувати голосові та відео\n"
        f"🔹 Аналізувати зображення\n"
        f"🔹 Пам'ятати контекст розмови\n\n"
        f"Налаштування та ключі доступні в меню."
    )
    
    keyboard = [
        [InlineKeyboardButton("⚙️ Налаштування", callback_data="settings_menu")],
        [InlineKeyboardButton("🔑 Мої ключі", callback_data="keys_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')

# Заглушка для callback кнопок (реалізуємо пізніше)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Це меню поки в розробці :)")