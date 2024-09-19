import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from .context_manager import context_manager

logger = logging.getLogger(__name__)

# Оновлені значення за замовчуванням
DEFAULT_SETTINGS = {
    'ENABLE_POSTPROCESSING': False,
    'ENABLE_SUMMARIZATION': True,
    'ENABLE_REWRITING': True,
    'ENABLE_VIDEO_PROCESSING': False,
    'ENABLE_VIDEO_NOTE_PROCESSING': True,
    'LANGUAGE': 'uk',
    'USE_GPT4': True,
    'CONTEXT_DURATION': 1,  # в годинах
    'ENABLE_CONTEXT': True
}

def get_user_settings(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if 'user_settings' not in context.bot_data:
        context.bot_data['user_settings'] = {}
    if user_id not in context.bot_data['user_settings']:
        context.bot_data['user_settings'][user_id] = DEFAULT_SETTINGS.copy()
    return context.bot_data['user_settings'][user_id]

def update_user_setting(context: ContextTypes.DEFAULT_TYPE, user_id: int, setting: str, value):
    user_settings = get_user_settings(context, user_id)
    user_settings[setting] = value

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Обробка тексту", callback_data='text_processing')],
        [InlineKeyboardButton("Обробка медіа", callback_data='media_processing')],
        [InlineKeyboardButton("Налаштування AI", callback_data='ai_settings')],
        [InlineKeyboardButton("Налаштування контексту", callback_data='context_settings')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text="Оберіть категорію налаштувань:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Оберіть категорію налаштувань:", reply_markup=reply_markup)

async def text_processing_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"Постобробка {'✅' if user_settings['ENABLE_POSTPROCESSING'] else '❌'}",
            callback_data='toggle_postprocessing')],
        [InlineKeyboardButton(
            f"Резюмування {'✅' if user_settings['ENABLE_SUMMARIZATION'] else '❌'}",
            callback_data='toggle_summarization')],
        [InlineKeyboardButton(
            f"Переписування {'✅' if user_settings['ENABLE_REWRITING'] else '❌'}",
            callback_data='toggle_rewriting')],
        [InlineKeyboardButton("Назад", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text="Налаштування обробки тексту:", reply_markup=reply_markup)

async def media_processing_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"Обробка відео {'✅' if user_settings['ENABLE_VIDEO_PROCESSING'] else '❌'}",
            callback_data='toggle_video_processing')],
        [InlineKeyboardButton(
            f"Обробка відеоповідомлень {'✅' if user_settings['ENABLE_VIDEO_NOTE_PROCESSING'] else '❌'}",
            callback_data='toggle_video_note_processing')],
        [InlineKeyboardButton("Назад", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text="Налаштування обробки медіа:", reply_markup=reply_markup)

async def ai_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"Мова: {'🇺🇦' if user_settings['LANGUAGE'] == 'uk' else '🇬🇧' if user_settings['LANGUAGE'] == 'en' else '🇷🇺'}",
            callback_data='change_language')],
        [InlineKeyboardButton(
            f"AI: {'GPT-4' if user_settings['USE_GPT4'] else 'Claude'}",
            callback_data='toggle_ai')],
        [InlineKeyboardButton("Назад", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text="Налаштування AI:", reply_markup=reply_markup)

async def context_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"Контекст {'✅' if user_settings['ENABLE_CONTEXT'] else '❌'}",
            callback_data='toggle_context')],
        [InlineKeyboardButton(
            f"Тривалість контексту: {user_settings['CONTEXT_DURATION']} год",
            callback_data='change_context_duration')],
        [InlineKeyboardButton("Скинути контекст", callback_data='reset_context')],
        [InlineKeyboardButton("Назад", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text="Налаштування контексту:", reply_markup=reply_markup)

async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, setting: str) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    user_settings[setting] = not user_settings[setting]
    logger.info(f"{setting} {'увімкнено' if user_settings[setting] else 'вимкнено'} для користувача {user_id}")
    await update.callback_query.answer(f"{setting} {'увімкнено' if user_settings[setting] else 'вимкнено'}")

async def toggle_postprocessing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'ENABLE_POSTPROCESSING')
    await text_processing_menu(update, context)

async def toggle_summarization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'ENABLE_SUMMARIZATION')
    await text_processing_menu(update, context)

async def toggle_rewriting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'ENABLE_REWRITING')
    await text_processing_menu(update, context)

async def toggle_video_processing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'ENABLE_VIDEO_PROCESSING')
    await media_processing_menu(update, context)

async def toggle_video_note_processing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'ENABLE_VIDEO_NOTE_PROCESSING')
    await media_processing_menu(update, context)

async def change_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    if user_settings['LANGUAGE'] == 'uk':
        user_settings['LANGUAGE'] = 'en'
        language_name = 'Англійська'
    elif user_settings['LANGUAGE'] == 'en':
        user_settings['LANGUAGE'] = 'ru'
        language_name = 'Російська'
    else:
        user_settings['LANGUAGE'] = 'uk'
        language_name = 'Українська'
    logger.info(f"Мова змінена на {user_settings['LANGUAGE']} для користувача {user_id}")
    await update.callback_query.answer(f"Мова змінена на {language_name}")
    await ai_settings_menu(update, context)

async def toggle_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'USE_GPT4')
    await ai_settings_menu(update, context)

async def toggle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle_setting(update, context, 'ENABLE_CONTEXT')
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    if not user_settings['ENABLE_CONTEXT']:
        context_manager.clear_context(user_id)
    await context_settings_menu(update, context)

async def change_context_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    durations = [1, 3, 6, 12, 24, 48, 72]
    current_index = durations.index(user_settings['CONTEXT_DURATION'])
    new_duration = durations[(current_index + 1) % len(durations)]
    user_settings['CONTEXT_DURATION'] = new_duration
    context_manager.set_context_duration(user_id, new_duration * 3600)  # конвертуємо години в секунди
    logger.info(f"Тривалість контексту змінена на {new_duration} годин для користувача {user_id}")
    await update.callback_query.answer(f"Тривалість контексту змінена на {new_duration} годин")
    await context_settings_menu(update, context)

async def reset_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context_manager.clear_context(user_id)
    await update.callback_query.answer("Контекст скинуто")
    await context_settings_menu(update, context)

# Експортуємо всі необхідні функції та змінні
__all__ = ['get_user_settings', 'update_user_setting', 'settings_menu', 'toggle_postprocessing', 
           'toggle_summarization', 'toggle_rewriting', 'toggle_video_processing', 
           'toggle_video_note_processing', 'change_language', 'toggle_ai', 'DEFAULT_SETTINGS',
           'toggle_context', 'change_context_duration', 'text_processing_menu', 'media_processing_menu',
           'ai_settings_menu', 'context_settings_menu', 'reset_context']