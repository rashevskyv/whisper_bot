import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext

# Налаштування логування
logger = logging.getLogger(__name__)

ENABLE_POSTPROCESSING = False
ENABLE_SUMMARIZATION = True
ENABLE_REWRITING = True
LANGUAGE = 'uk'

def settings_menu(update: Update, context: CallbackContext) -> None:
    global ENABLE_POSTPROCESSING, ENABLE_SUMMARIZATION, ENABLE_REWRITING, LANGUAGE
    logger.info(f"Відображення меню налаштувань. ENABLE_POSTPROCESSING: {ENABLE_POSTPROCESSING}")
    keyboard = [
        [InlineKeyboardButton(
            "Постобробка: вимкнути" if ENABLE_POSTPROCESSING else "Постобробка: увімкнути",
            callback_data='toggle_postprocessing')],
        [InlineKeyboardButton(
            "Резюмування: вимкнути" if ENABLE_SUMMARIZATION else "Резюмування: увімкнути",
            callback_data='toggle_summarization')],
        [InlineKeyboardButton(
            "Переписування: вимкнути" if ENABLE_REWRITING else "Переписування: увімкнути",
            callback_data='toggle_rewriting')],
        [InlineKeyboardButton(
            "Мова: Українська" if LANGUAGE == 'uk' else "Мова: Англійська" if LANGUAGE == 'en' else "Мова: Російська",
            callback_data='change_language')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        update.callback_query.edit_message_text(text="Налаштування:", reply_markup=reply_markup)
    else:
        update.message.reply_text("Налаштування:", reply_markup=reply_markup)

def toggle_postprocessing(update: Update, context: CallbackContext) -> None:
    global ENABLE_POSTPROCESSING
    ENABLE_POSTPROCESSING = not ENABLE_POSTPROCESSING
    logger.info(f"Постобробка {'увімкнена' if ENABLE_POSTPROCESSING else 'вимкнена'}")
    logger.info(f"Новий стан ENABLE_POSTPROCESSING: {ENABLE_POSTPROCESSING}")
    settings_menu(update, context)

def toggle_summarization(update: Update, context: CallbackContext) -> None:
    global ENABLE_SUMMARIZATION
    ENABLE_SUMMARIZATION = not ENABLE_SUMMARIZATION
    logger.info(f"Резюмування {'увімкнене' if ENABLE_SUMMARIZATION else 'вимкнене'}")
    settings_menu(update, context)

def toggle_rewriting(update: Update, context: CallbackContext) -> None:
    global ENABLE_REWRITING
    ENABLE_REWRITING = not ENABLE_REWRITING
    logger.info(f"Переписування {'увімкнене' if ENABLE_REWRITING else 'вимкнене'}")
    settings_menu(update, context)

def change_language(update: Update, context: CallbackContext) -> None:
    global LANGUAGE
    if LANGUAGE == 'uk':
        LANGUAGE = 'en'
    elif LANGUAGE == 'en':
        LANGUAGE = 'ru'
    else:
        LANGUAGE = 'uk'
    logger.info(f"Мова змінена на {LANGUAGE}")
    settings_menu(update, context)
