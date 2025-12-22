from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from bot.utils.helpers import get_or_create_user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Якщо це натискання на Inline кнопку "Назад"
    if update.callback_query:
        await update.callback_query.answer()
        # Для inline-режиму ми не можемо надіслати ReplyKeyboard, 
        # тому просто редагуємо текст і лишаємо inline кнопку
        keyboard = [[InlineKeyboardButton("⚙️ Налаштування", callback_data="settings_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(
            f"Вітаю, {user.first_name}! Ви в головному меню.",
            reply_markup=reply_markup
        )
        return

    # Звичайна команда /start
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
    
    # Постійна кнопка під полем вводу
    menu_button = KeyboardButton("⚙️ Налаштування")
    reply_keyboard = ReplyKeyboardMarkup([[menu_button]], resize_keyboard=True)
    
    # Inline кнопка під повідомленням
    inline_keyboard = [[InlineKeyboardButton("⚙️ Відкрити меню", callback_data="settings_menu")]]
    
    await update.message.reply_text(
        text, 
        reply_markup=reply_keyboard, # Основна клавіатура
        parse_mode='HTML'
    )
    
    # Дублюємо inline для зручності
    await update.message.reply_text("Швидкий доступ:", reply_markup=InlineKeyboardMarkup(inline_keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()