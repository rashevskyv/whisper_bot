import os
import re
import time
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from .transcription import transcribe_audio, postprocess_text, summarize_text, rewrite_text, analyze_content
from .settings_handler import get_user_settings, settings_menu
from .context_manager import context_manager
from telegram.parsemode  import ParseMode
from moviepy.editor import VideoFileClip

logger = logging.getLogger(__name__)

# Глобальне визначення temp_dir
temp_dir = os.path.join(os.getcwd(), 'temp')
os.makedirs(temp_dir, exist_ok=True)

def start(update: Update, context: CallbackContext) -> None:
    logger.info("Команда /start отримана")
    keyboard = [
        [KeyboardButton("Меню")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст. Або почніть повідомлення зі слова "бот", щоб спілкуватися з AI. Для налаштувань натисніть кнопку "Меню".', reply_markup=reply_markup)

def escape_markdown(text):
    """Функція для екранування спеціальних символів у Markdown V2"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

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

async def send_streaming_message(update: Update, context: CallbackContext, text_generator):
    message = await update.message.reply_text("Обробка розпочата...")
    full_text = ""
    last_edit_time = 0
    for text_chunk in text_generator:
        full_text += text_chunk
        current_time = time.time()
        if current_time - last_edit_time > 0.5:  # Оновлюємо кожні 0.5 секунд
            try:
                await message.edit_text(full_text)
                last_edit_time = current_time
            except Exception as e:
                logger.error(f"Помилка при оновленні повідомлення: {e}")
        await asyncio.sleep(0.01)  # Невелика затримка для запобігання блокування
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Помилка при видаленні повідомлення: {e}")
    return full_text

async def handle_audio(update: Update, context: CallbackContext, audio_path: str = None, source: str = "audio") -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    if not audio_path:
        if source == "audio":
            audio_file = update.message.audio or update.message.voice
        elif source in ["video", "video_note"]:
            audio_file = update.message[source]
        else:
            raise ValueError(f"Невідомий тип джерела аудіо: {source}")
        
        file = await context.bot.getFile(audio_file.file_id)
        audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
        await file.download(audio_path)
    
    try:
        transcription_generator = transcribe_audio(audio_path, user_settings['LANGUAGE'])
        transcription = await send_streaming_message(update, context, transcription_generator)
        
        if transcription.startswith("Помилка:"):
            await update.message.reply_text(transcription)
            return

        if user_settings['ENABLE_POSTPROCESSING']:
            transcription = postprocess_text(transcription)

        message_id = f"{source}_{user_id}_{int(time.time())}"
        if 'transcriptions' not in context.bot_data:
            context.bot_data['transcriptions'] = {}
        context.bot_data['transcriptions'][message_id] = transcription
        
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{message_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f'`{transcription}`',
            reply_to_message_id=update.message.message_id,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    except Exception as e:
        logger.error(f"Помилка при обробці {source}: {str(e)}", exc_info=True)
        await update.message.reply_text(f"Виникла помилка при обробці {source}. Будь ласка, спробуйте ще раз.")
    finally:
        cleanup_temp_files(audio_path)

def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path)
    video.close()

def handle_video(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    if not user_settings['ENABLE_VIDEO_PROCESSING']:
        update.message.reply_text("Обробка відео вимкнена в налаштуваннях.")
        return

    video = update.message.video
    video_path = os.path.join(temp_dir, f'{video.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video.file_id}.ogg')

    try:
        file = context.bot.get_file(video.file_id)
        file.download(video_path)
        logger.info(f"Відео завантажено: {video_path}")
        
        # Витягуємо аудіо з відео
        extract_audio_from_video(video_path, audio_path)
        logger.info(f"Аудіо витягнуто з відео: {audio_path}")
        
        # Обробляємо аудіо
        handle_audio(update, context, audio_path, source="video")

    except Exception as e:
        logger.error(f"Помилка при обробці відео: {e}", exc_info=True)
        update.message.reply_text("Виникла помилка при обробці відео. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_path, audio_path)

def handle_video_note(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    if not user_settings['ENABLE_VIDEO_NOTE_PROCESSING']:
        update.message.reply_text("Обробка відеоповідомлень вимкнена в налаштуваннях.")
        return

    video_note = update.message.video_note
    video_note_path = os.path.join(temp_dir, f'{video_note.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video_note.file_id}.ogg')

    try:
        file = context.bot.get_file(video_note.file_id)
        file.download(video_note_path)
        logger.info(f"Відео-нотатка завантажена: {video_note_path}")
        
        # Витягуємо аудіо з відео-нотатки
        extract_audio_from_video(video_note_path, audio_path)
        logger.info(f"Аудіо витягнуто з відео-нотатки: {audio_path}")
        
        # Обробляємо аудіо
        handle_audio(update, context, audio_path, source="video_note")

    except Exception as e:
        logger.error(f"Помилка при обробці відеоповідомлення: {e}", exc_info=True)
        update.message.reply_text("Виникла помилка при обробці відеоповідомлення. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_note_path, audio_path)
                
def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    try:
        video = VideoFileClip(video_path)
        video.audio.write_audiofile(audio_path)
        video.close()
    except Exception as e:
        logger.error(f"Помилка при витяганні аудіо з відео: {e}")
        raise

def cleanup_temp_files(*file_paths):
    for file_path in file_paths:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Успішно видалено файл: {file_path}")
        except Exception as e:
            logger.error(f"Не вдалося видалити файл {file_path}: {e}")

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
        
        if text or image_path:
            analyze_and_respond(update, context, text, image_path)
        else:
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

def escape_markdown(text):
    """Функція для екранування спеціальних символів у Markdown V2"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
                        
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
        message_id = data.split(":")[1]
        if 'transcriptions' in context.bot_data and message_id in context.bot_data['transcriptions']:
            full_text = context.bot_data['transcriptions'][message_id]
            analyze_and_respond(update, context, full_text, None)
        else:
            query.edit_message_text("На жаль, текст розшифровки більше недоступний.")

    # Видаляємо кнопку після її використання
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.error(f"Не вдалося видалити кнопку: {e}")
        
__all__ = ['start', 'handle_message', 'settings_menu', 'button_handler', 'handle_audio', 'handle_video', 'handle_video_note']
