import os
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
import os
import logging
from .transcription import transcribe_audio, postprocess_text
from .settings_handler import get_user_settings
from .context_manager import context_manager
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
        [KeyboardButton("Меню")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст. Або почніть повідомлення зі слова "бот", щоб спілкуватися з AI. Для налаштувань натисніть кнопку "Меню".', reply_markup=reply_markup)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def handle_audio(update: Update, context: CallbackContext, audio_path: str = None) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    user = update.effective_user
    chat = update.effective_chat

    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    
    logger.info(f"Отримано аудіофайл від користувача. {user_info}, {chat_info}")
    
    if not audio_path:
        audio_file = update.message.audio or update.message.voice
        file = context.bot.getFile(audio_file.file_id)
        audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
        file.download(audio_path)
        logger.info(f"Файл завантажено для обробки: {audio_path}")

    try:
        transcription = transcribe_audio(audio_path, user_settings['LANGUAGE'])
        
        if user_settings['ENABLE_POSTPROCESSING']:
            postprocessed_text = postprocess_text(transcription)
            output_text = f'Розшифровка аудіо (після обробки):\n`\n{postprocessed_text}\n`'
        else:
            output_text = f'Розшифровка аудіо:\n`\n{transcription}\n`'

        # Обмежуємо розмір transcription для callback_data
        max_callback_data_length = 64  # Telegram обмежує callback_data до 64 байтів
        truncated_transcription = transcription[:max_callback_data_length]
        
        # Створюємо кнопку для відправки розшифрованого тексту боту
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{truncated_transcription}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Відправляємо повідомлення з обмеженим текстом, якщо він занадто довгий
        max_message_length = 4096
        if len(output_text) > max_message_length:
            chunks = [output_text[i:i+max_message_length] for i in range(0, len(output_text), max_message_length)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    update.message.reply_text(chunk, reply_to_message_id=update.message.message_id, 
                                              parse_mode='Markdown', reply_markup=reply_markup)
                else:
                    update.message.reply_text(chunk, parse_mode='Markdown')
        else:
            update.message.reply_text(output_text, reply_to_message_id=update.message.message_id, 
                                      parse_mode='Markdown', reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Помилка при обробці аудіо: {str(e)}")
        update.message.reply_text("Виникла помилка при обробці аудіо. Будь ласка, спробуйте ще раз.")
    finally:
        cleanup_temp_files(audio_path)

def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path)
    video.close()

def handle_video(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    user = update.effective_user
    chat = update.effective_chat

    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    
    logger.info(f"Отримано відео від користувача. {user_info}, {chat_info}")
    
    video = update.message.video
    file = context.bot.get_file(video.file_id)
    video_path = os.path.join(temp_dir, f'{video.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video.file_id}.ogg')

    try:
        file.download(video_path)
        logger.info(f"Відео завантажено: {video_path}")

        # Витягуємо аудіо з відео
        extract_audio_from_video(video_path, audio_path)
        logger.info(f"Аудіо витягнуто з відео: {audio_path}")

        # Обробляємо аудіо
        transcription = transcribe_audio(audio_path, user_settings['LANGUAGE'])
        
        if user_settings['ENABLE_POSTPROCESSING']:
            postprocessed_text = postprocess_text(transcription)
            output_text = f'Розшифровка відео (після обробки):\n{postprocessed_text}'
        else:
            output_text = f'Розшифровка відео:\n{transcription}'

        # Обмежуємо розмір тексту для callback_data
        callback_text = transcription[:20]  # Беремо перші 20 символів
        
        # Створюємо кнопку для відправки розшифрованого тексту боту
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{callback_text}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Відправляємо повідомлення без reply_markup, якщо виникає помилка з кнопкою
        try:
            update.message.reply_text(output_text[:4096], reply_to_message_id=update.message.message_id, 
                                      reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            logger.warning(f"Не вдалося створити кнопку: {e}")
            update.message.reply_text(output_text[:4096], reply_to_message_id=update.message.message_id)

    except Exception as e:
        logger.error(f"Помилка при обробці відео: {e}")
        update.message.reply_text("Виникла помилка при обробці відео. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_path, audio_path)
        
def handle_video_note(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    user = update.effective_user
    chat = update.effective_chat

    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    
    if not user_settings['ENABLE_VIDEO_NOTE_PROCESSING']:
        logger.info(f"Обробка відеоповідомлень вимкнена. {user_info}, {chat_info}")
        return

    logger.info(f"Отримано відеоповідомлення. {user_info}, {chat_info}")
    video_note = update.message.video_note
    file = context.bot.get_file(video_note.file_id)
    
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
        transcription = transcribe_audio(audio_path, user_settings['LANGUAGE'])
        
        if user_settings['ENABLE_POSTPROCESSING']:
            postprocessed_text = postprocess_text(transcription)
            output_text = f'Розшифровка відеоповідомлення (після обробки):\n{postprocessed_text}'
        else:
            output_text = f'Розшифровка відеоповідомлення:\n{transcription}'

        # Обмежуємо розмір тексту для callback_data
        callback_text = transcription[:20]  # Беремо перші 20 символів
        
        # Створюємо кнопку для відправки розшифрованого тексту боту
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{callback_text}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Відправляємо повідомлення без reply_markup, якщо виникає помилка з кнопкою
        try:
            update.message.reply_text(output_text[:4096], reply_to_message_id=update.message.message_id, 
                                      reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            logger.warning(f"Не вдалося створити кнопку: {e}")
            update.message.reply_text(output_text[:4096], reply_to_message_id=update.message.message_id)

    except Exception as e:
        logger.error(f"Помилка при обробці відеоповідомлення: {e}")
        update.message.reply_text("Виникла помилка при обробці відеоповідомлення. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_note_path, audio_path)

def cleanup_temp_files(*file_paths):
    for file_path in file_paths:
        for attempt in range(3):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Успішно видалено файл: {file_path}")
                break
            except Exception as e:
                logger.warning(f"Спроба {attempt + 1} видалити файл {file_path} не вдалася: {e}")
                time.sleep(1)
        else:
            logger.error(f"Не вдалося видалити файл {file_path} після кількох спроб")

import re
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from .transcription import transcribe_audio, postprocess_text, summarize_text, rewrite_text, analyze_content
from .settings_handler import get_user_settings, settings_menu
from .context_manager import context_manager

logger = logging.getLogger(__name__)

# Глобальне визначення temp_dir
temp_dir = os.path.join(os.getcwd(), 'temp')
os.makedirs(temp_dir, exist_ok=True)

def handle_message(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    message = update.message or update.edited_message
    
    if not message:
        logger.debug("Отримано порожнє повідомлення")
        return

    chat_type = message.chat.type
    user = update.effective_user
    chat = update.effective_chat

    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    
    logger.debug(f"Отримано повідомлення. {user_info}, {chat_info}, Тип чату: {chat_type}")

    text = message.text or message.caption
    
    if text:
        logger.debug(f"Текст повідомлення: {text}")

    if text == "Меню":
        settings_menu(update, context)
        return

    # Функція для перевірки наявності ключових слів резюмування
    def is_summarize_command(text, chat_type):
        summarize_keywords = r'\b(резюме|підсумуй|резюмуй|підсумок)\w*'
        if chat_type == 'private':
            return re.search(summarize_keywords, text.lower())
        else:
            return re.search(r'\bбот\W+.*' + summarize_keywords, text.lower())

    # Обробка аудіо повідомлень
    if message.voice or message.audio:
        logger.info(f"Отримано аудіоповідомлення. {user_info}, {chat_info}")
        handle_audio(update, context)
        return

    # Обробка відео повідомлень
    if message.video:
        logger.info(f"Отримано відео. {user_info}, {chat_info}")
        if user_settings['ENABLE_VIDEO_PROCESSING']:
            handle_video(update, context)
        else:
            update.message.reply_text("Обробка відео вимкнена в налаштуваннях.")
        return

    # Обробка відео-нотаток
    if message.video_note:
        logger.info(f"Отримано відеоповідомлення. {user_info}, {chat_info}")
        if user_settings['ENABLE_VIDEO_NOTE_PROCESSING']:
            handle_video_note(update, context)
        else:
            update.message.reply_text("Обробка відеоповідомлень вимкнена в налаштуваннях.")
        return

    # Обробка цитованих повідомлень
    if message.reply_to_message:
        logger.debug("Виявлено цитоване повідомлення")
        original_message = message.reply_to_message.text or message.reply_to_message.caption
        if original_message:
            logger.debug(f"Текст цитованого повідомлення: {original_message}")
            
            if is_summarize_command(text, chat_type):
                logger.info("Запит на резюмування цитованого повідомлення")
                if user_settings['ENABLE_SUMMARIZATION']:
                    bot_message = message.reply_text("Створюю резюме...", parse_mode='Markdown')
                    full_response = ""
                    for chunk in summarize_text(original_message):
                        full_response += chunk
                        if len(full_response) % 100 == 0:
                            try:
                                context.bot.edit_message_text(
                                    chat_id=update.effective_chat.id,
                                    message_id=bot_message.message_id,
                                    text=f'Резюме:\n`\n{full_response}\n`',
                                    parse_mode='Markdown'
                                )
                            except Exception as e:
                                logger.error(f"Помилка при оновленні повідомлення: {e}")
                    try:
                        context.bot.edit_message_text(
                            chat_id=update.effective_chat.id,
                            message_id=bot_message.message_id,
                            text=f'Резюме:\n`\n{full_response}\n`',
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Помилка при відправці фінального повідомлення: {e}")
                else:
                    update.message.reply_text("Функція резюмування вимкнена в налаштуваннях.")
                return
            
            elif "перепиши" in text.lower():
                logger.info("Запит на переписування цитованого повідомлення")
                if user_settings['ENABLE_REWRITING']:
                    bot_message = message.reply_text("Переписую текст...", parse_mode='Markdown')
                    full_response = ""
                    for chunk in rewrite_text(original_message):
                        full_response += chunk
                        if len(full_response) % 100 == 0:
                            try:
                                context.bot.edit_message_text(
                                    chat_id=update.effective_chat.id,
                                    message_id=bot_message.message_id,
                                    text=f'Переписаний текст:\n`\n{full_response}\n`',
                                    parse_mode='Markdown'
                                )
                            except Exception as e:
                                logger.error(f"Помилка при оновленні повідомлення: {e}")
                    try:
                        context.bot.edit_message_text(
                            chat_id=update.effective_chat.id,
                            message_id=bot_message.message_id,
                            text=f'Переписаний текст:\n`\n{full_response}\n`',
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Помилка при відправці фінального повідомлення: {e}")
                else:
                    update.message.reply_text("Функція переписування вимкнена в налаштуваннях.")
                return

    # Обробка звичайних повідомлень
    should_process = (
        (chat_type == 'private') or
        (chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"))
    )

    if should_process:
        if chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"):
            text = text[3:].strip()

        image_path = None
        if message.photo:
            logger.debug(f"Виявлено зображення в поточному повідомленні. {user_info}, {chat_info}")
            image_file = message.photo[-1].get_file()
            image_path = os.path.join(temp_dir, f'{image_file.file_id}.jpg')
            image_file.download(image_path)
            logger.debug(f"Зображення завантажено: {image_path}")
        
        if not text and not image_path:
            update.message.reply_text("Будь ласка, надайте текст або зображення для аналізу.")
            return

        analyze_and_respond(update, context, text, image_path)
    
    else:
        logger.debug(f"Повідомлення не є запитом до бота. {user_info}, {chat_info}")

def cleanup_temp_files(*file_paths):
    for file_path in file_paths:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Успішно видалено файл: {file_path}")
        except Exception as e:
            logger.error(f"Не вдалося видалити файл {file_path}: {e}")

def handle_audio(update: Update, context: CallbackContext, audio_path: str = None) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    user = update.effective_user
    chat = update.effective_chat

    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    
    logger.info(f"Отримано аудіофайл від користувача. {user_info}, {chat_info}")
    
    if not audio_path:
        audio_file = update.message.audio or update.message.voice
        file = context.bot.getFile(audio_file.file_id)
        audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
        file.download(audio_path)
        logger.info(f"Файл завантажено для обробки: {audio_path}")

    try:
        transcription = transcribe_audio(audio_path, user_settings['LANGUAGE'])
        
        if user_settings['ENABLE_POSTPROCESSING']:
            postprocessed_text = postprocess_text(transcription)
            output_text = f'Розшифровка аудіо (після обробки):\n{postprocessed_text}'
        else:
            output_text = f'Розшифровка аудіо:\n{transcription}'

        # Обмежуємо розмір тексту для callback_data
        callback_text = transcription[:20]  # Беремо перші 20 символів
        
        # Створюємо кнопку для відправки розшифрованого тексту боту
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{callback_text}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Відправляємо повідомлення без reply_markup, якщо виникає помилка з кнопкою
        try:
            update.message.reply_text(output_text[:4096], reply_to_message_id=update.message.message_id, 
                                      reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            logger.warning(f"Не вдалося створити кнопку: {e}")
            update.message.reply_text(output_text[:4096], reply_to_message_id=update.message.message_id)

    except Exception as e:
        logger.error(f"Помилка при обробці аудіо: {e}")
        update.message.reply_text("Виникла помилка при обробці аудіо. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(audio_path)

def analyze_and_respond(update: Update, context: CallbackContext, text: str, image_path: str = None):
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    bot_message = update.effective_message.reply_text("Аналізую запит...", parse_mode='Markdown')
    
    try:
        full_response = ""
        
        if user_settings['ENABLE_CONTEXT']:
            conversation_context = context_manager.get_context(user_id)
        else:
            conversation_context = []
        
        if user_settings['ENABLE_CONTEXT']:
            context_manager.add_message(user_id, 'user', text)
        
        for chunk in analyze_content(text, image_path, conversation_context):
            full_response += chunk
        
        if full_response:
            if full_response != "Аналізую запит...":
                try:
                    context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=bot_message.message_id,
                        text=full_response[:4096],
                        parse_mode='Markdown'
                    )
                except telegram.error.BadRequest as e:
                    if "Message is not modified" in str(e):
                        logger.info("Повідомлення не було змінено, оскільки воно ідентичне.")
                    else:
                        logger.error(f"Помилка при відправці фінального повідомлення: {e}")
                        update.effective_message.reply_text("Виникла помилка при відправці відповіді. Будь ласка, спробуйте ще раз.")
            else:
                logger.info("Відповідь ідентична початковому повідомленню. Пропускаємо оновлення.")
        else:
            update.effective_message.reply_text("На жаль, не вдалося отримати відповідь. Будь ласка, спробуйте ще раз.")

        if user_settings['ENABLE_CONTEXT']:
            context_manager.add_message(user_id, 'assistant', full_response)
    except Exception as e:
        logger.error(f"Помилка при обробці повідомлення: {e}")
        update.effective_message.reply_text("Виникла помилка при обробці вашого запиту. Будь ласка, спробуйте ще раз.")

def button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    data = query.data
    if data.startswith("send_to_bot:"):
        full_text = query.message.text
        if full_text:
            if full_text.startswith("Розшифровка"):
                full_text = full_text.split("\n", 1)[-1].strip()
            full_text = full_text.strip('`').strip()
        
        analyze_and_respond(update, context, full_text, None)  # Додано None для image_path

    # Видаляємо кнопку після її використання
    try:
        context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=None
        )
    except Exception as e:
        logger.error(f"Не вдалося видалити кнопку: {e}")

__all__ = ['start', 'handle_message', 'settings_menu', 'button_handler', 'handle_audio', 'handle_video', 'handle_video_note']
