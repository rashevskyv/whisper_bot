import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider, send_long_message, beautify_text
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files
from bot.handlers.common import should_respond, get_user_model_settings, MEDIA_GROUP_CACHE
from bot.handlers.ai import process_gpt_request

logger = logging.getLogger(__name__)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not should_respond(update, context):
        return
        
    message = update.message
    caption = message.caption
    media_group_id = message.media_group_id
    
    if media_group_id:
        if caption:
            MEDIA_GROUP_CACHE[media_group_id] = caption
        elif media_group_id in MEDIA_GROUP_CACHE:
            caption = MEDIA_GROUP_CACHE[media_group_id]
    
    if caption:
        provider = await get_ai_provider(update.effective_user.id)
        if not provider:
            await update.message.reply_text("⚠️ Немає доступу до AI.")
            return

        status_msg = await update.message.reply_text("👀 Дивлюсь...", reply_to_message_id=message.message_id)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        
        temp_files = []
        try:
            photo_file = await message.photo[-1].get_file()
            image_path = await download_file(photo_file, f"vision_prompt_{message.message_id}")
            temp_files.append(image_path)
            
            messages = await context_manager.get_context(update.effective_user.id, limit=5)
            full_response = ""
            last_update_len = 0
            
            async for chunk in provider.analyze_image(image_path, caption, messages):
                full_response += chunk
                if len(full_response) - last_update_len > 50:
                    try:
                        await status_msg.edit_text(full_response + " ▌")
                        last_update_len = len(full_response)
                    except: pass
            
            await status_msg.delete()
            await send_long_message(message, full_response, parse_mode=ParseMode.HTML)
            
            await context_manager.save_message(update.effective_user.id, 'user', f"[Photo with Caption]: {caption}")
            await context_manager.save_message(update.effective_user.id, 'assistant', full_response)

        except Exception as e:
            logger.error(f"Vision Direct Error: {e}")
            await status_msg.edit_text(f"❌ Помилка: {e}")
        finally:
            cleanup_files(temp_files)
    else:
        keyboard = [
            [InlineKeyboardButton("🖼 Описати", callback_data="photo_desc"), InlineKeyboardButton("📄 Текст (OCR)", callback_data="photo_read")],
            [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
        ]
        await update.message.reply_text("Що зробити з цим зображенням?", reply_markup=InlineKeyboardMarkup(keyboard), quote=True)

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.effective_user; chat_type = update.effective_chat.type
    if update.message.video and chat_type != 'private':
        if not should_respond(update, context): return
    if update.message.voice: file_obj = update.message.voice; is_video = False
    elif update.message.video_note: file_obj = update.message.video_note; is_video = True
    elif update.message.video: file_obj = update.message.video; is_video = True
    else: return

    provider = await get_ai_provider(user.id, force_whisper=True)
    if not provider:
        if chat_type == 'private': await update.message.reply_text("⚠️ Немає ключа.")
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
        settings = await get_user_model_settings(user.id); lang = settings.get('language', 'uk')
        
        transcription = await provider.transcribe(audio_path, language=lang)
        
        if status_msg: await status_msg.edit_text("✨ Форматую текст...")
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
            
            await context_manager.save_message(user.id, 'user', f"[Транскрипція]: {clean_text}")
            await send_long_message(update.message, f"<code>{clean_text}</code>", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            
    except Exception as e:
        logger.error(f"Media error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Помилка: {e}")
    finally:
        cleanup_files(temp_files)