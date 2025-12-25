from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from bot.utils.helpers import get_or_create_user
from bot.handlers.settings import get_main_menu_keyboard

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Якщо це callback (кнопка "Назад")
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"Вітаю, {user.first_name}! Ви в головному меню.",
            reply_markup=get_main_menu_keyboard()
        )
        return

    await get_or_create_user(user)
    
    text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        f"Я — мульти-модельний AI бот (GPT-4o + Gemini).\n"
        f"Я вмію бачити, чути, шукати в інтернеті та аналізувати.\n\n"
        f"<b>Як користуватися:</b>\n"
        f"• Просто пиши текст\n"
        f"• Надсилай фото, голосові, відео\n"
        f"• Пиши 'меню' для налаштувань"
    )
    
    menu_button = KeyboardButton("⚙️ Налаштування")
    # is_persistent=True змушує кнопку залишатися видимою на Desktop
    reply_keyboard = ReplyKeyboardMarkup(
        [[menu_button]], 
        resize_keyboard=True, 
        is_persistent=True 
    )
    
    await update.message.reply_text(
        text, 
        reply_markup=reply_keyboard, 
        parse_mode='HTML'
    )
    
    await update.message.reply_text("Швидкий доступ:", reply_markup=get_main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()