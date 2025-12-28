import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider, send_long_message, beautify_text
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files
from bot.handlers.common import should_respond, get_user_model_settings, MEDIA_GROUP_CACHE

logger = logging.getLogger(__name__)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка фото з підписом або без"""
    # 1. Перевірка: чи треба відповідати (для груп)
    if not should_respond(update, context):
        return
        
    message = update.message
    caption = message.caption
    media_group_id = message.media_group_id
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Кешування підписів для альбомів (групи фото)
    if media_group_id:
        if caption: MEDIA_GROUP_CACHE[media_group_id] = caption
        elif media_group_id in MEDIA_GROUP_CACHE: caption = MEDIA_GROUP_CACHE[media_group_id]
    
    if caption:
        # Якщо є підпис (або тригер спрацював на підпис), аналізуємо фото
        provider = await get_ai_provider(user_id)
        if not provider:
            return

        # Формуємо розширений промпт, якщо це відповідь на інше повідомлення
        full_prompt = caption
        if message.reply_to_message:
            reply_msg = message.reply_to_message
            quoted_text = reply_msg.text or reply_msg.caption or "[Медіа]"
            full_prompt = f"CONTEXT (User replied to this): {quoted_text}\n\nIMAGE CAPTION/PROMPT: {caption}"

        status_msg = await update.message.reply_text("👀 Дивлюсь...", quote=True)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        
        temp_files = []
        try:
            # Завантажуємо фото (беремо найбільший розмір)
            photo_file = await message.photo[-1].get_file()
            image_path = await download_file(photo_file, f"vis_{message.message_id}")
            temp_files.append(image_path)
            
            # Отримуємо контекст чату
            messages = await context_manager.get_context(user_id, chat_id, limit=5)
            
            # Додаємо налаштування (передаємо поточну модель користувача)
            settings = await get_user_model_settings(user_id)
            
            full_response = ""
            last_update_len = 0
            
            # Викликаємо аналіз
            async for chunk in provider.analyze_image(image_path, full_prompt, messages, settings):
                full_response += chunk
                if len(full_response) - last_update_len > 50:
                    try:
                        await status_msg.edit_text(full_response + " ▌")
                        last_update_len = len(full_response)
                    except: pass
            
            await status_msg.delete()
            await send_long_message(message, full_response, parse_mode=ParseMode.HTML)
            
            # Зберігаємо в історію
            await context_manager.save_message(user_id, chat_id, 'user', f"[Photo Analysis]: {full_prompt}")
            await context_manager.save_message(user_id, chat_id, 'assistant', full_response)

        except Exception as e:
            logger.error(f"Vision Direct Error: {e}")
            await status_msg.edit_text(f"❌ Помилка: {e}")
        finally:
            cleanup_files(temp_files)
    else:
        # Якщо підпису немає, але ми в приваті (або змусили бота відповісти), показуємо меню
        if update.effective_chat.type == 'private':
            keyboard = [
                [InlineKeyboardButton("🖼 Описати", callback_data="photo_desc"), InlineKeyboardButton("📄 Текст (OCR)", callback_data="photo_read")],
                [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
            ]
            await update.message.reply_text("Що зробити з цим зображенням?", reply_markup=InlineKeyboardMarkup(keyboard), quote=True)

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка голосових та відео повідомлень"""
    if not update.message: return
    user = update.effective_user
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    
    # У групах відповідаємо тільки якщо є тригер (або це відео, яке ми фільтруємо окремо)
    if update.message.video and chat_type != 'private':
        if not should_respond(update, context): return

    # Визначаємо тип файлу
    if update.message.voice: file_obj = update.message.voice; is_video = False
    elif update.message.video_note: file_obj = update.message.video_note; is_video = True
    elif update.message.video: file_obj = update.message.video; is_video = True
    else: return

    provider = await get_ai_provider(user.id, for_transcription=True)
    if not provider:
        if chat_type == 'private': await update.message.reply_text("⚠️ Немає ключа API.")
        return

    status_msg = None
    if chat_type == 'private': status_msg = await update.message.reply_text("📥 Завантажую...")
    
    temp_files = []
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        input_path = await download_file(tg_file, file_obj.file_id)
        temp_files.append(input_path)

        if is_video:
            audio_path = await extract_audio(input_path)
            temp_files.append(audio_path)
        else:
            audio_path = input_path

        if status_msg: await status_msg.edit_text("🎙 Розпізнаю...")
        settings = await get_user_model_settings(user.id)
        lang = settings.get('language', 'uk')
        
        # Транскрибація
        transcription = await provider.transcribe(audio_path, language=lang)
        
        # Покращення тексту (Beautify)
        if status_msg: await status_msg.edit_text("✨ Оформлюю текст...")
        clean_text = await beautify_text(user.id, transcription)
        
        if status_msg: await status_msg.delete()

        if clean_text:
            reply_markup = None
            if chat_type == 'private':
                keyboard = [
                    [InlineKeyboardButton("🤖 Відправити боту", callback_data="run_gpt")],
                    [InlineKeyboardButton("📝 Підсумувати", callback_data="summarize"), InlineKeyboardButton("✍️ Переформулювати", callback_data="reword")],
                    [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Зберігаємо транскрипцію
            await context_manager.save_message(user.id, chat_id, 'user', f"[Транскрипція]: {clean_text}")
            await send_long_message(update.message, f"<code>{clean_text}</code>", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            
    except Exception as e:
        logger.error(f"Media error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Помилка: {e}")
    finally:
        cleanup_files(temp_files)