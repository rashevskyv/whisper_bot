import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, MessageHandler, Filters
from .transcription import transcribe_audio, postprocess_text, summarize_text, rewrite_text
from .settings_handler import settings_menu, toggle_postprocessing, toggle_summarization, toggle_rewriting, change_language, LANGUAGE
import time

# Налаштування логування
logger = logging.getLogger(__name__)

ENABLE_POSTPROCESSING = False
LANGUAGE = 'uk'

def start(update: Update, context: CallbackContext) -> None:
    logger.info("Команда /start отримана")
    keyboard = [
        [KeyboardButton("Меню налаштувань")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст.', reply_markup=reply_markup)

def delete_old_files(current_file_path, directory, max_age_minutes=10):
    current_time = time.time()
    max_age_seconds = max_age_minutes * 60

    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if os.path.isfile(file_path):
            file_age = current_time - os.path.getmtime(file_path)
            if file_age > max_age_seconds:
                os.remove(file_path)
                logger.info(f"Видалено старий файл: {file_path}")
    os.remove(current_file_path)

def handle_audio(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_POSTPROCESSING
    logger.info("Отримано аудіофайл від користувача")
    audio_file = update.message.audio or update.message.voice
    file = context.bot.getFile(audio_file.file_id)
    temp_dir = os.path.join(os.getcwd(), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    file_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
    file.download(file_path)
    logger.info(f"Файл завантажено для обробки: {file_path}")

    transcription = transcribe_audio(file_path, LANGUAGE)
    
    if ENABLE_POSTPROCESSING:
        output_text = f'Розшифровка аудіо (постобробка):\n```\n{postprocess_text(transcription)}\n```'
    else:
        output_text = f'Розшифровка аудіо:\n```\n{transcription}\n```'
    
    update.message.reply_text(output_text, parse_mode='Markdown')

    if "зроби резюме" in transcription.lower():
        summary = summarize_text(transcription)
        update.message.reply_text(f'Резюме:\n```\n{summary}\n```', parse_mode='Markdown')

    delete_old_files(file_path, temp_dir)

def handle_text(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_REWRITING, ENABLE_SUMMARIZATION
    message = update.message.text
    logger.info(f"Отримано текстове повідомлення: {message}")
    
    if ENABLE_REWRITING and "перепиши" in message.lower():
        rewrite = rewrite_text(message)
        update.message.reply_text(f'Переписаний текст:\n```\n{rewrite}\n```', parse_mode='Markdown')
    elif ENABLE_SUMMARIZATION and "зроби резюме" in message.lower():
        summary = summarize_text(message)
        update.message.reply_text(f'Резюме:\n```\n{summary}\n```', parse_mode='Markdown')
    elif update.message.reply_to_message:
        original_message = update.message.reply_to_message.text
        response = process_command(original_message, message)
        if response:
            logger.info("Переписування текстового повідомлення")
            update.message.reply_text(response, reply_to_message_id=update.message.message_id, parse_mode='Markdown')

def process_command(transcription: str, message: str) -> str:
    from .settings_handler import ENABLE_SUMMARIZATION, ENABLE_REWRITING, ENABLE_POSTPROCESSING

    if ENABLE_SUMMARIZATION and ("бот зроби резюме" in message or "бот резюме" in message):
        return f'Резюме:\n```\n{summarize_text(transcription)}\n```'
    elif ENABLE_REWRITING and "бот перепиши" in message:
        return f'Переписаний текст:\n```\n{rewrite_text(transcription)}\n```'
    elif ENABLE_POSTPROCESSING and "бот постобробка" in message:
        return f'Постоброблений текст:\n```\n{postprocess_text(transcription)}\n```'
    return ''
