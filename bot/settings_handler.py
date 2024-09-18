import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext

logger = logging.getLogger(__name__)

# Значення за замовчуванням
DEFAULT_SETTINGS = {
    'ENABLE_POSTPROCESSING': False,
    'ENABLE_SUMMARIZATION': True,
    'ENABLE_REWRITING': True,
    'ENABLE_VIDEO_PROCESSING': False,
    'ENABLE_VIDEO_NOTE_PROCESSING': True,
    'LANGUAGE': 'uk',
    'USE_GPT4': True
}

def get_user_settings(context: CallbackContext, user_id: int):
    if 'user_settings' not in context.bot_data:
        context.bot_data['user_settings'] = {}
    if user_id not in context.bot_data['user_settings']:
        context.bot_data['user_settings'][user_id] = DEFAULT_SETTINGS.copy()
    return context.bot_data['user_settings'][user_id]

def update_user_setting(context: CallbackContext, user_id: int, setting: str, value):
    user_settings = get_user_settings(context, user_id)
    user_settings[setting] = value

def settings_menu(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"Постобробка: {'вимкнути' if user_settings['ENABLE_POSTPROCESSING'] else 'увімкнути'}",
            callback_data='toggle_postprocessing')],
        [InlineKeyboardButton(
            f"Резюмування: {'вимкнути' if user_settings['ENABLE_SUMMARIZATION'] else 'увімкнути'}",
            callback_data='toggle_summarization')],
        [InlineKeyboardButton(
            f"Переписування: {'вимкнути' if user_settings['ENABLE_REWRITING'] else 'увімкнути'}",
            callback_data='toggle_rewriting')],
        [InlineKeyboardButton(
            f"Обробка відео: {'вимкнути' if user_settings['ENABLE_VIDEO_PROCESSING'] else 'увімкнути'}",
            callback_data='toggle_video_processing')],
        [InlineKeyboardButton(
            f"Обробка відеоповідомлень: {'вимкнути' if user_settings['ENABLE_VIDEO_NOTE_PROCESSING'] else 'увімкнути'}",
            callback_data='toggle_video_note_processing')],
        [InlineKeyboardButton(
            f"Мова: {'Українська' if user_settings['LANGUAGE'] == 'uk' else 'Англійська' if user_settings['LANGUAGE'] == 'en' else 'Російська'}",
            callback_data='change_language')],
        [InlineKeyboardButton(
            f"AI: {'GPT-4' if user_settings['USE_GPT4'] else 'Claude'}",
            callback_data='toggle_ai')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        update.callback_query.edit_message_text(text="Налаштування:", reply_markup=reply_markup)
    else:
        update.message.reply_text("Налаштування:", reply_markup=reply_markup)

def toggle_setting(update: Update, context: CallbackContext, setting: str) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    user_settings[setting] = not user_settings[setting]
    logger.info(f"{setting} {'увімкнено' if user_settings[setting] else 'вимкнено'} для користувача {user_id}")
    settings_menu(update, context)

def toggle_postprocessing(update: Update, context: CallbackContext) -> None:
    toggle_setting(update, context, 'ENABLE_POSTPROCESSING')

def toggle_summarization(update: Update, context: CallbackContext) -> None:
    toggle_setting(update, context, 'ENABLE_SUMMARIZATION')

def toggle_rewriting(update: Update, context: CallbackContext) -> None:
    toggle_setting(update, context, 'ENABLE_REWRITING')

def toggle_video_processing(update: Update, context: CallbackContext) -> None:
    toggle_setting(update, context, 'ENABLE_VIDEO_PROCESSING')

def toggle_video_note_processing(update: Update, context: CallbackContext) -> None:
    toggle_setting(update, context, 'ENABLE_VIDEO_NOTE_PROCESSING')

def change_language(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    if user_settings['LANGUAGE'] == 'uk':
        user_settings['LANGUAGE'] = 'en'
    elif user_settings['LANGUAGE'] == 'en':
        user_settings['LANGUAGE'] = 'ru'
    else:
        user_settings['LANGUAGE'] = 'uk'
    logger.info(f"Мова змінена на {user_settings['LANGUAGE']} для користувача {user_id}")
    settings_menu(update, context)

def toggle_ai(update: Update, context: CallbackContext) -> None:
    toggle_setting(update, context, 'USE_GPT4')

# Експортуємо всі необхідні функції та змінні
__all__ = ['get_user_settings', 'update_user_setting', 'settings_menu', 'toggle_postprocessing', 
           'toggle_summarization', 'toggle_rewriting', 'toggle_video_processing', 
           'toggle_video_note_processing', 'change_language', 'toggle_ai', 'DEFAULT_SETTINGS']