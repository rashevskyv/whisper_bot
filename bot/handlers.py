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

temp_dir = os.path.join(os.getcwd(), 'temp')
os.makedirs(temp_dir, exist_ok=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("Меню")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text('Вітаю! Надішліть мені аудіофайл, і я розшифрую його в текст. Або почніть повідомлення зі слова "бот", щоб спілкуватися з AI. Для налаштувань натисніть кнопку "Меню".', reply_markup=reply_markup)

def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def cleanup_temp_files(*file_paths):
    for file_path in file_paths:
        for attempt in range(3):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                break
            except Exception as e:
                time.sleep(1)

async def send_streaming_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text_generator):
    message = await update.message.reply_text("Обробка розпочата...")
    full_text = ""
    last_edit_time = time.time()
    async for text_chunk in text_generator:
        full_text += text_chunk
        current_time = time.time()
        if current_time - last_edit_time > 1 or len(full_text) >= 3900:
            try:
                await message.edit_text(full_text[:4096])
                last_edit_time = current_time
            except Exception as e:
                pass
        await asyncio.sleep(0.01)
    try:
        await message.edit_text(full_text[:4096])
    except Exception as e:
        pass

    user = update.effective_user
    chat = update.effective_chat
    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    logger.info(f"Sent response. {user_info}, {chat_info}: {full_text}")

    return full_text

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_path: str = None, source: str = "audio") -> None:
    user = update.effective_user
    chat = update.effective_chat
    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    logger.info(f"Received {source}. {user_info}, {chat_info}")

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
            
            file = await context.bot.get_file(audio_file.file_id)
            audio_path = os.path.join(temp_dir, f'{audio_file.file_id}.ogg')
            await file.download_to_drive(audio_path)
        
        streaming_message = await update.message.reply_text("Обробка розпочата...")
        transcription = ""
        async for chunk in transcribe_audio(audio_path, user_settings['LANGUAGE']):
            transcription += chunk
            await streaming_message.edit_text(transcription)
        
        await streaming_message.delete()

        if transcription.startswith("Помилка:"):
            await update.message.reply_text(transcription)
            return

        if user_settings['ENABLE_POSTPROCESSING']:
            transcription = await postprocess_text(transcription)

        message_id = f"{source}_{user_id}_{int(time.time())}"
        if 'transcriptions' not in context.bot_data:
            context.bot_data['transcriptions'] = {}
        context.bot_data['transcriptions'][message_id] = transcription
        
        keyboard = [[InlineKeyboardButton("Відправити боту", callback_data=f"send_to_bot:{message_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f'`{transcription}`',
            reply_to_message_id=update.message.message_id,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        logger.info(f"Sent transcription. {user_info}, {chat_info}: {transcription}")
    
    except Exception as e:
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
    chat_type = update.effective_chat.type
    
    if chat_type in ['group', 'supergroup']:
        if not (update.message.reply_to_message and 
                update.message.reply_to_message.video and 
                update.message.text and 
                update.message.text.lower().startswith("бот")):
            return
        video = update.message.reply_to_message.video
    else:
        if not user_settings['ENABLE_VIDEO_PROCESSING']:
            await update.message.reply_text("Обробка відео вимкнена в налаштуваннях.")
            return
        video = update.message.video

    video_path = os.path.join(temp_dir, f'{video.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{video.file_id}.ogg')

    try:
        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(video_path)
        
        extract_audio_from_video(video_path, audio_path)
        
        await handle_audio(update, context, audio_path, source="video")

    except Exception as e:
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
        
        extract_audio_from_video(video_note_path, audio_path)
        
        await handle_audio(update, context, audio_path, source="video_note")

    except Exception as e:
        await update.message.reply_text("Виникла помилка при обробці відеоповідомлення. Будь ласка, спробуйте ще раз.")

    finally:
        cleanup_temp_files(video_note_path, audio_path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.message or update.edited_message
    
    if not message:
        return

    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"

    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    chat_type = message.chat.type

    text = message.text or message.caption or ""
    
    if text == "Меню":
        await settings_menu(update, context)
        return

    if message.voice or message.audio:
        await handle_audio(update, context)
        return

    if message.video:
        if user_settings['ENABLE_VIDEO_PROCESSING']:
            await handle_video(update, context)
        else:
            await update.message.reply_text("Обробка відео вимкнена в налаштуваннях.")
        return

    if message.video_note:
        if user_settings['ENABLE_VIDEO_NOTE_PROCESSING']:
            await handle_video_note(update, context)
        else:
            await update.message.reply_text("Обробка відеоповідомлень вимкнена в налаштуваннях.")
        return

    if message.reply_to_message:
        quoted_message = message.reply_to_message
        
        if quoted_message.text:
            content_type = "text"
            content = quoted_message.text
        elif quoted_message.voice or quoted_message.audio:
            content_type = "audio"
            content = quoted_message.voice or quoted_message.audio
        elif quoted_message.video:
            content_type = "video"
            content = quoted_message.video
        elif quoted_message.photo:
            content_type = "photo"
            content = quoted_message.photo[-1]
        else:
            content_type = "unknown"
            content = None
        
        if text.lower().startswith("бот"):
            user_query = text[3:].strip()
            
            try:
                if content_type == "text":
                    await process_text_content(update, context, content, user_query)
                elif content_type == "audio":
                    await process_audio_content(update, context, content, user_query)
                elif content_type == "video":
                    await process_video_content(update, context, content, user_query)
                elif content_type == "photo":
                    await process_photo_content(update, context, content, user_query)
                else:
                    await update.message.reply_text("Вибачте, я не можу обробити цей тип контенту.")
            except Exception as e:
                await update.message.reply_text("Виникла помилка при обробці цитованого контенту. Будь ласка, спробуйте ще раз.")
        else:
            return
    
    else:
        should_process = (
            (chat_type == 'private') or
            (chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"))
        )

        if should_process:
            if chat_type in ['group', 'supergroup'] and text and text.lower().startswith("бот"):
                text = text[3:].strip()

            image_path = None
            if message.photo:
                image_file = await message.photo[-1].get_file()
                image_path = os.path.join(temp_dir, f'{image_file.file_id}.jpg')
                try:
                    await image_file.download_to_drive(image_path)
                except Exception as e:
                    await update.message.reply_text("Виникла помилка при завантаженні зображення. Будь ласка, спробуйте ще раз.")
                    return
            
            if text or image_path:
                try:
                    response_generator = analyze_content(text, image_path, context_manager.get_context(user_id) if user_settings['ENABLE_CONTEXT'] else None)
                    await send_streaming_message(update, context, response_generator)
                except Exception as e:
                    await update.message.reply_text("Виникла помилка при аналізі вашого запиту. Будь ласка, спробуйте ще раз.")
            else:
                await update.message.reply_text("Будь ласка, надайте текст або зображення для аналізу.")
        
async def process_text_content(update, context, content, user_query):
    full_text = f"{content}\n\nЗапит користувача: {user_query}"
    response_generator = analyze_content(full_text, None, context_manager.get_context(update.effective_user.id) if get_user_settings(context, update.effective_user.id)['ENABLE_CONTEXT'] else None)
    await send_streaming_message(update, context, response_generator)

async def process_audio_content(update, context, content, user_query):
    audio_path = os.path.join(temp_dir, f'{content.file_id}.ogg')
    try:
        file = await content.get_file()
        await file.download_to_drive(audio_path)
        transcription = await transcribe_audio(audio_path, get_user_settings(context, update.effective_user.id)['LANGUAGE'])
        full_text = f"Транскрипція аудіо: {transcription}\n\nЗапит користувача: {user_query}"
        response_generator = analyze_content(full_text, None, context_manager.get_context(update.effective_user.id) if get_user_settings(context, update.effective_user.id)['ENABLE_CONTEXT'] else None)
        await send_streaming_message(update, context, response_generator)
    finally:
        cleanup_temp_files(audio_path)

async def process_video_content(update, context, content, user_query):
    video_path = os.path.join(temp_dir, f'{content.file_id}.mp4')
    audio_path = os.path.join(temp_dir, f'{content.file_id}.ogg')
    try:
        file = await content.get_file()
        await file.download_to_drive(video_path)
        extract_audio_from_video(video_path, audio_path)
        transcription = await transcribe_audio(audio_path, get_user_settings(context, update.effective_user.id)['LANGUAGE'])
        full_text = f"Транскрипція відео: {transcription}\n\nЗапит користувача: {user_query}"
        response_generator = analyze_content(full_text, None, context_manager.get_context(update.effective_user.id) if get_user_settings(context, update.effective_user.id)['ENABLE_CONTEXT'] else None)
        await send_streaming_message(update, context, response_generator)
    finally:
        cleanup_temp_files(video_path, audio_path)

async def process_photo_content(update, context, content, user_query):
    photo_path = os.path.join(temp_dir, f'{content.file_id}.jpg')
    try:
        file = await content.get_file()
        await file.download_to_drive(photo_path)
        full_text = f"Запит користувача: {user_query}"
        response_generator = analyze_content(full_text, photo_path, context_manager.get_context(update.effective_user.id) if get_user_settings(context, update.effective_user.id)['ENABLE_CONTEXT'] else None)
        await send_streaming_message(update, context, response_generator)
    finally:
        cleanup_temp_files(photo_path)

async def analyze_and_respond(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, image_path: str = None):
    user = update.effective_user
    chat = update.effective_chat
    user_info = f"User: {user.first_name} {user.last_name or ''} (@{user.username or 'No username'}, ID: {user.id})"
    chat_info = f"Chat: {chat.title or 'Private'} (@{chat.username or 'No username'}, ID: {chat.id})"
    
    user_id = update.effective_user.id
    user_settings = get_user_settings(context, user_id)
    
    bot_message = await update.effective_message.reply_text("Аналізую запит...", parse_mode=ParseMode.MARKDOWN)
    
    full_response = ""
    
    conversation_context = context_manager.get_context(user_id) if user_settings['ENABLE_CONTEXT'] else None
    
    if user_settings['ENABLE_CONTEXT']:
        context_manager.add_message(user_id, 'user', text)
    
    try:
        async for chunk in analyze_content(text, image_path, conversation_context):
            full_response += chunk
            if len(full_response) % 100 == 0:
                try:
                    await bot_message.edit_text(full_response[:4096], parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    logger.error(f"Помилка при оновленні повідомлення: {str(e)}")
    except Exception as e:
        logger.error(f"Помилка при аналізі контенту: {str(e)}")
        await update.effective_message.reply_text("Виникла помилка при аналізі вашого запиту. Будь ласка, спробуйте ще раз.")
        return

    if full_response:
        try:
            await bot_message.edit_text(full_response[:4096], parse_mode=ParseMode.MARKDOWN)
            logger.info(f"Sent response. {user_info}, {chat_info}: {full_response}")
        except Exception as e:
            logger.error(f"Помилка при відправці фінального повідомлення: {str(e)}")
            await update.effective_message.reply_text("Виникла помилка при відправці відповіді. Будь ласка, спробуйте ще раз.")
    else:
        await update.effective_message.reply_text("На жаль, не вдалося отримати відповідь. Будь ласка, спробуйте ще раз.")

    if user_settings['ENABLE_CONTEXT']:
        context_manager.add_message(user_id, 'assistant', full_response)

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

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        pass
                        
__all__ = ['start', 'handle_message', 'settings_menu', 'button_handler', 'handle_audio', 'handle_video', 'handle_video_note']