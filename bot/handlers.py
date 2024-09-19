import os
import re
import time
import asyncio
import logging
import traceback
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from .transcription import transcribe_audio, postprocess_text, summarize_text, rewrite_text, analyze_content
from .settings_handler import get_user_settings, settings_menu
from .context_manager import context_manager
from moviepy.editor import VideoFileClip

logger = logging.getLogger(__name__)

# Глобальне визначення temp_dir
temp_dir = os.path.join(os.getcwd(), 'temp')
os.makedirs(temp_dir, exist_ok=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Команда /start отримана")
    try:
        keyboard = [
            [KeyboardButton("Меню")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст. Або почніть повідомлення зі слова "бот", щоб спілкуватися з AI. Для налаштувань натисніть кнопку "Меню".', reply_markup=reply_markup)
        logger.info("Відповідь на команду /start надіслана успішно")
    except Exception as e:
        logger.error(f"Помилка при обробці команди /start: {e}")
        logger.error(traceback.format_exc())

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

async def send_streaming_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text_generator):
    message = await update.message.reply_text("Обробка розпочата...")
    full_text = ""
    last_edit_time = time.time()
    chunk_count = 0
    async for text_chunk in text_generator:
        full_text += text_chunk
        chunk_count += 1
        current_time = time.time()
        if current_time - last_edit_time > 1 or len(full_text) >= 3900:
            try:
                await message.edit_text(full_text[:4096])
                last_edit_time = current_time
                logger.debug(f"Оновлено повідомлення. Кількість чанків: {chunk_count}")
            except Exception as e:
                logger.error(f"Помилка при оновленні повідомлення: {e}")
        await asyncio.sleep(0.01)
    try:
        await message.edit_text(full_text[:4096])
        logger.info(f"Фінальне оновлення повідомлення. Загальна кількість чанків: {chunk_count}")
    except Exception as e:
        logger.error(f"Помилка при фінальному оновленні повідомлення: {e}")
    return full_text

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_path: str = None, source: str = "audio") -> None:
    logger.info(f"Початок обробки {source}")
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    try:
        if not audio_path:
            if source == "audio":
                audio_file = update.message.audio or update.message.voice
            elif source in ["video", "video_note"]:
                audio_file = update.message[source]
            else:
                raise ValueError(f"Невідомий тип джерела аудіо: {source}")
            
            logger.info(f"Отримання файлу для {source}")
            file = await context.bot.get_file(audio_file.file_id)
            audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
            logger.info(f"Завантаження файлу {source} до {audio_path}")
            await file.download_to_drive(audio_path)
            logger.info(f"Файл {source} успішно завантажено")
        
        logger.info(f"Початок транскрибації {source}")
        streaming_message = await update.message.reply_text("Обробка розпочата...")
        transcription = ""
        async for chunk in transcribe_audio(audio_path, user_settings['LANGUAGE']):
            transcription += chunk
            await streaming_message.edit_text(transcription)
        
        await streaming_message.delete()

        if transcription.startswith("Помилка:"):
            logger.error(f"Помилка при транскрибації {source}: {transcription}")
            await update.message.reply_text(transcription)
            return

        logger.info(f"Транскрибація {source} успішна")

        if user_settings['ENABLE_POSTPROCESSING']:
            logger.info(f"Початок постобробки транскрипції {source}")
            transcription = await postprocess_text(transcription)
            logger.info(f"Постобробка транскрипції {source} завершена")

        message_id = f"{source}_{user_id}_{int(time.time())}"
        if 'transcriptions' not in context.bot_data:
            context.bot_data['transcriptions'] = {}
        context.bot_data['transcriptions'][message_id] = transcription
        
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{message_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        logger.info(f"Відправка результату транскрипції {source}")
        await update.message.reply_text(
            f'`{transcription}`',
            reply_to_message_id=update.message.message_id,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        logger.info(f"Результат транскрипції {source} успішно відправлено")
    
    except Exception as e:
        logger.error(f"Помилка при обробці {source}: {str(e)}", exc_info=True)
        await update.message.reply_text(f"Виникла помилка при обробці {source}: {str(e)}. Будь ласка, спробуйте ще раз.")
    finally:
        if audio_path:
            cleanup_temp_files(audio_path)

def extract_audio_from_video(video_path: str, audio_path: str) -> None:
    video = VideoFileClip(video_path)
    video.audio.write_audiofile(audio_path)
    video.close()

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    if not user_settings['ENABLE_VIDEO_PROCESSING']:
        await update.message.reply_text("Обробка відео вимкнена в налаштуваннях.")
        return

    video = update.message.video
    video_path = os.path.join(temp_dir, f'{video.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video.file_id}.ogg')

    try:
        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(video_path)
        logger.info(f"Відео завантажено: {video_path}")
        
        # Витягуємо аудіо з відео
        extract_audio_from_video(video_path, audio_path)
        logger.info(f"Аудіо витягнуто з відео: {audio_path}")
        
        # Обробляємо аудіо
        await handle_audio(update, context, audio_path, source="video")

    except Exception as e:
        logger.error(f"Помилка при обробці відео: {e}", exc_info=True)
        await update.message.reply_text("Виникла помилка при обробці відео. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_path, audio_path)

async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    if not user_settings['ENABLE_VIDEO_NOTE_PROCESSING']:
        await update.message.reply_text("Обробка відеоповідомлень вимкнена в налаштуваннях.")
        return

    video_note = update.message.video_note
    video_note_path = os.path.join(temp_dir, f'{video_note.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video_note.file_id}.ogg')

    try:
        file = await context.bot.get_file(video_note.file_id)
        await file.download_to_drive(video_note_path)
        logger.info(f"Відео-нотатка завантажена: {video_note_path}")
        
        # Витягуємо аудіо з відео-нотатки
        extract_audio_from_video(video_note_path, audio_path)
        logger.info(f"Аудіо витягнуто з відео-нотатки: {audio_path}")
        
        # Обробляємо аудіо
        await handle_audio(update, context, audio_path, source="video_note")

    except Exception as e:
        logger.error(f"Помилка при обробці відеоповідомлення: {e}", exc_info=True)
        await update.message.reply_text("Виникла помилка при обробці відеоповідомлення. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_note_path, audio_path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Функція handle_message викликана")
    try:
        user_id = update.effective_user.id
        user_settings = get_user_settings(context, user_id)
        logger.info(f"Отримано налаштування для користувача {user_id}: {user_settings}")
        
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

        text = message.text or message.caption or ""
        
        if text == "Меню":
            logger.info("Викликано меню налаштувань")
            await settings_menu(update, context)
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
            await handle_audio(update, context)
            return

        # Обробка відео повідомлень
        if message.video:
            logger.info(f"Отримано відео. {user_info}, {chat_info}")
            if user_settings['ENABLE_VIDEO_PROCESSING']:
                await handle_video(update, context)
            else:
                await update.message.reply_text("Обробка відео вимкнена в налаштуваннях.")
            return

        # Обробка відео-нотаток
        if message.video_note:
            logger.info(f"Отримано відеоповідомлення. {user_info}, {chat_info}")
            if user_settings['ENABLE_VIDEO_NOTE_PROCESSING']:
                await handle_video_note(update, context)
            else:
                await update.message.reply_text("Обробка відеоповідомлень вимкнена в налаштуваннях.")
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
                        bot_message = await message.reply_text("Створюю резюме...", parse_mode=ParseMode.MARKDOWN)
                        full_response = ""
                        async for chunk in summarize_text(original_message):
                            full_response += chunk
                            if len(full_response) % 100 == 0:
                                try:
                                    await context.bot.edit_message_text(
                                        chat_id=update.effective_chat.id,
                                        message_id=bot_message.message_id,
                                        text=f'Резюме:\n`\n{full_response}\n`',
                                        parse_mode=ParseMode.MARKDOWN
                                    )
                                except Exception as e:
                                    logger.error(f"Помилка при оновленні повідомлення: {e}")
                        try:
                            await context.bot.edit_message_text(
                                chat_id=update.effective_chat.id,
                                message_id=bot_message.message_id,
                                text=f'Резюме:\n`\n{full_response}\n`',
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception as e:
                            logger.error(f"Помилка при відправці фінального повідомлення: {e}")
                    else:
                        await update.message.reply_text("Функція резюмування вимкнена в налаштуваннях.")
                    return

                elif "перепиши" in text.lower():
                    logger.info("Запит на переписування цитованого повідомлення")
                    if user_settings['ENABLE_REWRITING']:
                        bot_message = await message.reply_text("Переписую текст...", parse_mode=ParseMode.MARKDOWN)
                        full_response = ""
                        async for chunk in rewrite_text(original_message):
                            full_response += chunk
                            if len(full_response) % 100 == 0:
                                try:
                                    await context.bot.edit_message_text(
                                        chat_id=update.effective_chat.id,
                                        message_id=bot_message.message_id,
                                        text=f'Переписаний текст:\n`\n{full_response}\n`',
                                        parse_mode=ParseMode.MARKDOWN
                                    )
                                except Exception as e:
                                    logger.error(f"Помилка при оновленні повідомлення: {e}")
                        try:
                            await context.bot.edit_message_text(
                                chat_id=update.effective_chat.id,
                                message_id=bot_message.message_id,
                                text=f'Переписаний текст:\n`\n{full_response}\n`',
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception as e:
                            logger.error(f"Помилка при відправці фінального повідомлення: {e}")
                    else:
                        await update.message.reply_text("Функція переписування вимкнена в налаштуваннях.")
                    return
                
        # Обробка звичайних повідомлень
        should_process = (
            (chat_type == 'private') or
            (chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"))
        )

        if should_process:
            logger.info(f"Обробка повідомлення: {text[:50] if text else 'Зображення без тексту'}...")
            if chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"):
                text = text[3:].strip()

            image_path = None
            if message.photo:
                logger.debug(f"Виявлено зображення в поточному повідомленні. {user_info}, {chat_info}")
                image_file = await message.photo[-1].get_file()
                image_path = os.path.join(temp_dir, f'{image_file.file_id}.jpg')
                logger.debug(f"Спроба завантаження зображення: {image_path}")
                try:
                    await image_file.download_to_drive(image_path)
                    logger.debug(f"Зображення успішно завантажено: {image_path}")
                except Exception as e:
                    logger.error(f"Помилка при завантаженні зображення: {e}", exc_info=True)
                    await update.message.reply_text("Виникла помилка при завантаженні зображення. Будь ласка, спробуйте ще раз.")
                    return
            
            if text or image_path:
                try:
                    response_generator = analyze_content(text, image_path, context_manager.get_context(user_id) if user_settings['ENABLE_CONTEXT'] else None)
                    await send_streaming_message(update, context, response_generator)
                except Exception as e:
                    logger.error(f"Помилка в analyze_and_respond: {e}", exc_info=True)
                    await update.message.reply_text("Виникла помилка при аналізі вашого запиту. Будь ласка, спробуйте ще раз.")
            else:
                await update.message.reply_text("Будь ласка, надайте текст або зображення для аналізу.")
        
        else:
            logger.debug(f"Повідомлення не є запитом до бота. {user_info}, {chat_info}")

    except Exception as e:
        logger.error(f"Помилка при обробці повідомлення: {e}", exc_info=True)
        await update.message.reply_text("Виникла помилка при обробці вашого запиту. Будь ласка, спробуйте ще раз.")

async def analyze_and_respond(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, image_path: str = None):
    logger.info(f"Функція analyze_and_respond викликана. Текст: {text[:50]}..., Шлях до зображення: {image_path}")
    try:
        user_id = update.effective_user.id
        user_settings = get_user_settings(context, user_id)
        logger.info(f"Отримано налаштування для користувача {user_id}: {user_settings}")
        
        bot_message = await update.effective_message.reply_text("Аналізую запит...", parse_mode=ParseMode.MARKDOWN)
        logger.debug("Відправлено початкове повідомлення про аналіз")
        
        full_response = ""
        
        if user_settings['ENABLE_CONTEXT']:
            conversation_context = context_manager.get_context(user_id)
            logger.debug(f"Отримано контекст розмови для користувача {user_id}: {len(conversation_context)} повідомлень")
        else:
            conversation_context = []
            logger.debug("Контекст розмови вимкнено")
        
        if user_settings['ENABLE_CONTEXT']:
            context_manager.add_message(user_id, 'user', text)
            logger.debug(f"Додано повідомлення користувача до контексту: {text[:50]}...")
        
        logger.info("Початок аналізу контенту")
        async for chunk in analyze_content(text, image_path, conversation_context):
            full_response += chunk
            if len(full_response) % 100 == 0:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=bot_message.message_id,
                        text=full_response[:4096],
                        parse_mode=ParseMode.MARKDOWN
                    )
                    logger.debug(f"Оновлено проміжне повідомлення. Довжина відповіді: {len(full_response)}")
                except Exception as e:
                    logger.error(f"Помилка при оновленні повідомлення: {e}")
        
        logger.info("Аналіз контенту завершено")
        
        if full_response:
            if full_response != "Аналізую запит...":
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=bot_message.message_id,
                        text=full_response[:4096],
                        parse_mode=ParseMode.MARKDOWN
                    )
                    logger.info("Відповідь успішно надіслана")
                except Exception as e:
                    if "Message is not modified" not in str(e):
                        logger.error(f"Помилка при відправці фінального повідомлення: {e}")
                        await update.effective_message.reply_text("Виникла помилка при відправці відповіді. Будь ласка, спробуйте ще раз.")
            else:
                logger.info("Відповідь ідентична початковому повідомленню. Пропускаємо оновлення.")
        else:
            await update.effective_message.reply_text("На жаль, не вдалося отримати відповідь. Будь ласка, спробуйте ще раз.")
            logger.warning("Не вдалося отримати відповідь від аналізу контенту")

        if user_settings['ENABLE_CONTEXT']:
            context_manager.add_message(user_id, 'assistant', full_response)
            logger.debug("Додано відповідь асистента до контексту")

    except Exception as e:
        logger.error(f"Помилка при аналізі та відповіді: {e}", exc_info=True)
        await update.effective_message.reply_text("Виникла помилка при обробці вашого запиту. Будь ласка, спробуйте ще раз.")
    finally:
        if image_path:
            cleanup_temp_files(image_path)
            logger.debug(f"Очищено тимчасовий файл зображення: {image_path}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("send_to_bot:"):
        message_id = data.split(":")[1]
        if 'transcriptions' in context.bot_data and message_id in context.bot_data['transcriptions']:
            full_text = context.bot_data['transcriptions'][message_id]
            await analyze_and_respond(update, context, full_text, None)
        else:
            await query.edit_message_text("На жаль, текст розшифровки більше недоступний.")

    # Видаляємо кнопку після її використання
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.error(f"Не вдалося видалити кнопку: {e}")
                        
__all__ = ['start', 'handle_message', 'settings_menu', 'button_handler', 'handle_audio', 'handle_video', 'handle_video_note']
