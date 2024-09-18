import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext

# Налаштування логування
logger = logging.getLogger(__name__)

ENABLE_POSTPROCESSING = False
ENABLE_SUMMARIZATION = True
ENABLE_REWRITING = True
ENABLE_VIDEO_PROCESSING = False
ENABLE_VIDEO_NOTE_PROCESSING = True
LANGUAGE = 'uk'
USE_GPT4 = True  # Нова змінна для вибору між GPT-4 і Claude

def settings_menu(update: Update, context: CallbackContext) -> None:
    global ENABLE_POSTPROCESSING, ENABLE_SUMMARIZATION, ENABLE_REWRITING, ENABLE_VIDEO_PROCESSING, ENABLE_VIDEO_NOTE_PROCESSING, LANGUAGE, USE_GPT4
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
            "Обробка відео: вимкнути" if ENABLE_VIDEO_PROCESSING else "Обробка відео: увімкнути",
            callback_data='toggle_video_processing')],
        [InlineKeyboardButton(
            "Обробка відеоповідомлень: вимкнути" if ENABLE_VIDEO_NOTE_PROCESSING else "Обробка відеоповідомлень: увімкнути",
            callback_data='toggle_video_note_processing')],
        [InlineKeyboardButton(
            "Мова: Українська" if LANGUAGE == 'uk' else "Мова: Англійська" if LANGUAGE == 'en' else "Мова: Російська",
            callback_data='change_language')],
        [InlineKeyboardButton(
            "AI: GPT-4" if USE_GPT4 else "AI: Claude",
            callback_data='toggle_ai')]
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

def toggle_video_processing(update: Update, context: CallbackContext) -> None:
    global ENABLE_VIDEO_PROCESSING
    ENABLE_VIDEO_PROCESSING = not ENABLE_VIDEO_PROCESSING
    logger.info(f"Обробка відео {'увімкнена' if ENABLE_VIDEO_PROCESSING else 'вимкнена'}")
    settings_menu(update, context)

def toggle_video_note_processing(update: Update, context: CallbackContext) -> None:
    global ENABLE_VIDEO_NOTE_PROCESSING
    ENABLE_VIDEO_NOTE_PROCESSING = not ENABLE_VIDEO_NOTE_PROCESSING
    logger.info(f"Обробка відеоповідомлень {'увімкнена' if ENABLE_VIDEO_NOTE_PROCESSING else 'вимкнена'}")
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

def toggle_ai(update: Update, context: CallbackContext) -> None:
    global USE_GPT4
    USE_GPT4 = not USE_GPT4
    logger.info(f"AI змінено на {'GPT-4' if USE_GPT4 else 'Claude'}")
    settings_menu(update, context)