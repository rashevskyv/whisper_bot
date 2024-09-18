import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, MessageHandler, Filters
from .transcription import transcribe_audio, postprocess_text, summarize_text, rewrite_text, query_gpt4_stream, query_claude_stream
from .settings_handler import settings_menu, toggle_postprocessing, toggle_summarization, toggle_rewriting, change_language, toggle_video_processing, toggle_video_note_processing, LANGUAGE, USE_GPT4, toggle_ai
import time
from moviepy.editor import VideoFileClip

# Налаштування логування
logger = logging.getLogger(__name__)

def start(update: Update, context: CallbackContext) -> None:
    logger.info("Команда /start отримана")
    keyboard = [
        [KeyboardButton("Меню налаштувань")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст. Або почніть повідомлення зі слова "бот", щоб спілкуватися з AI.', reply_markup=reply_markup)

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

def handle_audio(update: Update, context: CallbackContext, audio_path: str = None) -> None:
    from .settings_handler import ENABLE_POSTPROCESSING
    logger.info("Отримано аудіофайл від користувача")
    
    if not audio_path:
        audio_file = update.message.audio or update.message.voice
        file = context.bot.getFile(audio_file.file_id)
        temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
        file.download(audio_path)
        logger.info(f"Файл завантажено для обробки: {audio_path}")

    transcription = transcribe_audio(audio_path, LANGUAGE)
    
    if ENABLE_POSTPROCESSING:
        postprocessed_text = postprocess_text(transcription)
        output_text = f'`\n{postprocessed_text}\n`'
    else:
        output_text = f'Розшифровка аудіо:\n`\n{transcription}\n`'

    update.message.reply_text(output_text, reply_to_message_id=update.message.message_id, parse_mode='Markdown')

    if "зроби резюме" in transcription.lower():
        summary = summarize_text(transcription)
        update.message.reply_text(f'Резюме:\n`\n{summary}\n`', reply_to_message_id=update.message.message_id, parse_mode='Markdown')

    delete_old_files(audio_path, temp_dir)

def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path)

def handle_video(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_VIDEO_PROCESSING
    if not ENABLE_VIDEO_PROCESSING:
        logger.info("Обробка відео вимкнена")
        return

    logger.info("Отримано відеофайл від користувача")
    video_file = update.message.video
    file = context.bot.getFile(video_file.file_id)
    temp_dir = os.path.join(os.getcwd(), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    video_path = os.path.join(temp_dir, f'{video_file.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video_file.file_id}.ogg')
    file.download(video_path)

    extract_audio_from_video(video_path, audio_path)
    handle_audio(update, context, audio_path)

    delete_old_files(video_path, temp_dir)
    delete_old_files(audio_path, temp_dir)

def handle_video_note(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_VIDEO_NOTE_PROCESSING
    if not ENABLE_VIDEO_NOTE_PROCESSING:
        logger.info("Обробка відеоповідомлень вимкнена")
        return

    logger.info("Отримано відеоповідомлення від користувача")
    video_note = update.message.video_note
    file = context.bot.getFile(video_note.file_id)
    temp_dir = os.path.join(os.getcwd(), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    video_note_path = os.path.join(temp_dir, f'{video_note.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video_note.file_id}.ogg')
    file.download(video_note_path)

    extract_audio_from_video(video_note_path, audio_path)
    handle_audio(update, context, audio_path)

    delete_old_files(video_note_path, temp_dir)
    delete_old_files(audio_path, temp_dir)

def handle_text(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_REWRITING, ENABLE_SUMMARIZATION, ENABLE_POSTPROCESSING, USE_GPT4
    message = update.message.text

    if message.lower().startswith("бот"):
        query = message[3:].strip()  # Видаляємо "бот" та пробіли з початку запиту
        if USE_GPT4:
            stream = query_gpt4_stream(query)
        else:
            stream = query_claude_stream(query)
        
        # Відправляємо початкове повідомлення
        bot_message = update.message.reply_text("Генерую відповідь...", parse_mode='Markdown')
        full_response = ""
        
        # Оновлюємо повідомлення по мірі отримання нових частин відповіді
        for chunk in stream:
            full_response += chunk
            if len(full_response) % 100 == 0:  # Оновлюємо кожні 100 символів
                try:
                    context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=bot_message.message_id,
                        text=full_response,
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.error(f"Помилка при оновленні повідомлення: {e}")
        
        # Відправляємо фінальне повідомлення
        try:
            context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=bot_message.message_id,
                text=full_response,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Помилка при відправці фінального повідомлення: {e}")
    
    elif update.message.reply_to_message:
        original_message = update.message.reply_to_message.text

        if ENABLE_REWRITING and "перепиши" in message.lower():
            rewrite = rewrite_text(original_message)
            update.message.reply_text(f'Переписаний текст:\n`\n{rewrite}\n`', parse_mode='Markdown', reply_to_message_id=update.message.message_id)
        elif ENABLE_SUMMARIZATION and "резюме" in message.lower():
            summary = summarize_text(original_message)
            update.message.reply_text(f'Резюме:\n`\n{summary}\n`', parse_mode='Markdown', reply_to_message_id=update.message.message_id)
        elif ENABLE_POSTPROCESSING and "постобробка" in message.lower():
            postprocess = postprocess_text(original_message)
            update.message.reply_text(f'Оброблений текст:\n`\n{postprocess}\n`', parse_mode='Markdown', reply_to_message_id=update.message.message_id)
    else:
        update.message.reply_text("Для запиту до AI, почніть повідомлення зі слова 'бот'. Для інших функцій, надішліть це повідомлення у відповідь на те, яке потрібно обробити.")