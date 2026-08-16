from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from bot.utils.helpers import get_or_create_user
from bot.handlers.settings import get_main_menu_keyboard
from bot.utils.scheduler import scheduler_service

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    # Якщо це callback (кнопка "Назад" в меню)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"Вітаю, {user.first_name}! Ви в головному меню.",
            reply_markup=get_main_menu_keyboard()
        )
        return

    # Реєструємо користувача або групу в БД
    await get_or_create_user(chat)

    text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        f"Я — мульти-модельний AI бот (GPT-4o + Gemini).\n"
    )

    # ЛОГІКА КНОПОК
    if chat.type == 'private':
        # В особистих - показуємо кнопки
        has_reminders = await scheduler_service.get_reminders_count(chat.id) > 0
        buttons_row = [KeyboardButton("⚙️ Налаштування")]
        if has_reminders:
            buttons_row.insert(0, KeyboardButton("⏰ Нагадування"))

        reply_markup = ReplyKeyboardMarkup(
            [buttons_row],
            resize_keyboard=True,
            is_persistent=True
        )

        await update.message.reply_text(
            text + "Я вмію бачити, чути, шукати в інтернеті та аналізувати.",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        await update.message.reply_text("Швидкий доступ:", reply_markup=get_main_menu_keyboard())

    else:
        # В ГРУПАХ - ПРИБИРАЄМО КНОПКИ
        text += (
            f"<b>Команди для адміністраторів:</b>\n"
            f"• <code>налаштування</code> або <code>меню</code> — конфігурація бота.\n\n"
            f"Я відповідаю на реплаї або згадки."
        )

        # ReplyKeyboardRemove прибере клавіатуру у користувача, що викликав команду
        await update.message.reply_text(
            text,
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )