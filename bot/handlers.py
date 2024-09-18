import os
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, MessageHandler, Filters
from .transcription import transcribe_audio, postprocess_text, summarize_text, rewrite_text, query_gpt4_stream, query_claude_stream, analyze_image, analyze_content
from .settings_handler import settings_menu, toggle_postprocessing, toggle_summarization, toggle_rewriting, change_language, toggle_video_processing, toggle_video_note_processing, LANGUAGE, USE_GPT4, toggle_ai
from moviepy.editor import VideoFileClip

# Налаштування логування
logger = logging.getLogger(__name__)

# Глобальне визначення temp_dir
temp_dir = os.path.join(os.getcwd(), 'temp')
os.makedirs(temp_dir, exist_ok=True)

def cleanup_temp_files(*file_paths):
    """
    Видаляє тимчасові файли з кількома спробами.
    
    :param file_paths: Шляхи до файлів, які потрібно видалити
    """
    for file_path in file_paths:
        for attempt in range(3):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Успішно видалено файл: {file_path}")
                break
            except Exception as e:
                logger.warning(f"Спроба {attempt + 1} видалити файл {file_path} не вдалася: {e}")
                time.sleep(1)  # Чекаємо секунду перед повторною спробою
        else:
            logger.error(f"Не вдалося видалити файл {file_path} після кількох спроб")

def start(update: Update, context: CallbackContext) -> None:
    logger.info("Команда /start отримана")
    keyboard = [
        [KeyboardButton("Меню налаштувань")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст. Або почніть повідомлення зі слова "бот", щоб спілкуватися з AI.', reply_markup=reply_markup)

def handle_audio(update: Update, context: CallbackContext, audio_path: str = None) -> None:
    from .settings_handler import ENABLE_POSTPROCESSING
    logger.info("Отримано аудіофайл від користувача")
    
    if not audio_path:
        audio_file = update.message.audio or update.message.voice
        file = context.bot.getFile(audio_file.file_id)
        audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
        file.download(audio_path)
        logger.info(f"Файл завантажено для обробки: {audio_path}")

    try:
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
    finally:
        cleanup_temp_files(audio_path)

def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path)
    video.close()

def handle_video(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_VIDEO_PROCESSING
    if not ENABLE_VIDEO_PROCESSING:
        logger.info("Обробка відео вимкнена")
        return

    logger.info("Отримано відеофайл від користувача")
    video_file = update.message.video
    file = context.bot.getFile(video_file.file_id)
    video_path = os.path.join(temp_dir, f'{video_file.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video_file.file_id}.ogg')
    
    try:
        file.download(video_path)
        extract_audio_from_video(video_path, audio_path)
        handle_audio(update, context, audio_path)
    finally:
        cleanup_temp_files(video_path, audio_path)

def handle_video_note(update: Update, context: CallbackContext) -> None:
    from .settings_handler import ENABLE_VIDEO_NOTE_PROCESSING
    if not ENABLE_VIDEO_NOTE_PROCESSING:
        logger.info("Обробка відеоповідомлень вимкнена")
        return

    logger.info("Отримано відеоповідомлення від користувача")
    video_note = update.message.video_note
    file = context.bot.getFile(video_note.file_id)
    
    video_note_path = os.path.join(temp_dir, f'{video_note.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video_note.file_id}.ogg')

    try:
        # Спроба завантажити файл з кількома повторами
        for attempt in range(3):
            try:
                file.download(video_note_path)
                break
            except Exception as e:
                logger.warning(f"Спроба {attempt + 1} завантажити файл не вдалася: {e}")
                time.sleep(1)  # Чекаємо секунду перед повторною спробою
        else:
            raise Exception("Не вдалося завантажити файл після кількох спроб")

        # Витягуємо аудіо з відео
        extract_audio_from_video(video_note_path, audio_path)

        # Обробляємо аудіо
        handle_audio(update, context, audio_path)

    except Exception as e:
        logger.error(f"Помилка при обробці відеоповідомлення: {e}")
        update.message.reply_text("Виникла помилка при обробці відеоповідомлення. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_note_path, audio_path)

def handle_message(update: Update, context: CallbackContext) -> None:
    message = update.message or update.edited_message
    
    if not message:
        logger.debug("Отримано порожнє повідомлення")
        return

    chat_type = message.chat.type
    logger.debug(f"Отримано повідомлення типу: {chat_type}")

    # Отримуємо текст повідомлення
    text = message.text or message.caption
    logger.debug(f"Текст повідомлення: {text}")

    image_path = None

    # Перевіряємо, чи є зображення в цитованому повідомленні
    if message.reply_to_message and message.reply_to_message.photo:
        logger.debug("Виявлено зображення в цитованому повідомленні")
        image_file = message.reply_to_message.photo[-1].get_file()
        image_path = os.path.join(temp_dir, f'quoted_{image_file.file_id}.jpg')
        image_file.download(image_path)
        logger.debug(f"Цитоване зображення завантажено: {image_path}")

    # Визначаємо, чи потрібно обробляти повідомлення
    should_process = (
        (chat_type == 'private') or
        (chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"))
    )

    if should_process:
        # Видаляємо префікс "бот" для групових чатів
        if chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"):
            text = text[3:].strip()

        # Якщо зображення не було в цитованому повідомленні, перевіряємо поточне повідомлення
        if not image_path and message.photo:
            logger.debug("Виявлено зображення в поточному повідомленні")
            image_file = message.photo[-1].get_file()
            image_path = os.path.join(temp_dir, f'{image_file.file_id}.jpg')
            image_file.download(image_path)
            logger.debug(f"Зображення завантажено: {image_path}")
        
        # Відправляємо початкове повідомлення
        bot_message = message.reply_text("Аналізую запит...", parse_mode='Markdown')
        
        try:
            # Отримуємо аналіз контенту
            full_response = ""
            for chunk in analyze_content(text, image_path):
                full_response += chunk
                if len(full_response) % 100 == 0:  # Оновлюємо кожні 100 символів
                    try:
                        context.bot.edit_message_text(
                            chat_id=update.effective_chat.id,
                            message_id=bot_message.message_id,
                            text=full_response[:4096],  # Обмежуємо довжину повідомлення
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Помилка при оновленні повідомлення: {e}")
            
            # Відправляємо фінальне повідомлення
            try:
                context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=bot_message.message_id,
                    text=full_response[:4096],  # Обмежуємо довжину повідомлення
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Помилка при відправці фінального повідомлення: {e}")
        finally:
            # Видаляємо тимчасовий файл зображення, якщо він був створений
            if image_path:
                cleanup_temp_files(image_path)
    
    elif message.video_note:
        logger.info("Отримано відеоповідомлення")
        handle_video_note(update, context)
    
    elif message.voice or message.audio:
        logger.info("Отримано аудіоповідомлення")
        handle_audio(update, context)
    
    elif message.video:
        logger.info("Отримано відео")
        handle_video(update, context)
    
    elif update.message.reply_to_message and text:
        from .settings_handler import ENABLE_REWRITING, ENABLE_SUMMARIZATION, ENABLE_POSTPROCESSING
        original_message = update.message.reply_to_message.text

        if ENABLE_REWRITING and "перепиши" in text.lower():
            rewrite = rewrite_text(original_message)
            update.message.reply_text(f'Переписаний текст:\n`\n{rewrite}\n`', parse_mode='Markdown', reply_to_message_id=update.message.message_id)
        elif ENABLE_SUMMARIZATION and "резюме" in text.lower():
            summary = summarize_text(original_message)
            update.message.reply_text(f'Резюме:\n`\n{summary}\n`', parse_mode='Markdown', reply_to_message_id=update.message.message_id)
        elif ENABLE_POSTPROCESSING and "постобробка" in text.lower():
            postprocess = postprocess_text(original_message)
            update.message.reply_text(f'Оброблений текст:\n`\n{postprocess}\n`', parse_mode='Markdown', reply_to_message_id=update.message.message_id)
    
    else:
        logger.debug("Повідомлення не є запитом до бота")