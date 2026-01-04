import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider, send_long_message, beautify_text
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files
from bot.handlers.common import should_respond, get_user_model_settings, MEDIA_GROUP_CACHE

logger = logging.getLogger(__name__)

def get_log_user(user, chat_id):
    return f"[User: {user.id} ({user.first_name}) | Chat: {chat_id}]"

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not should_respond(update, context): 
        return
        
    message = update.message
    caption = message.caption
    media_group_id = message.media_group_id
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    user_log = get_log_user(update.effective_user, chat_id)
    
    if media_group_id:
        if caption: MEDIA_GROUP_CACHE[media_group_id] = caption
        elif media_group_id in MEDIA_GROUP_CACHE: caption = MEDIA_GROUP_CACHE[media_group_id]
    
    if caption:
        logger.info(f"📸 {user_log} Photo with caption: '{caption}'")
        provider = await get_ai_provider(user_id)
        if not provider: return

        full_prompt = caption
        if message.reply_to_message:
            reply_msg = message.reply_to_message
            full_prompt = f"CONTEXT (User replied to): {reply_msg.text or reply_msg.caption or '[Media]'}\n\nPROMPT: {caption}"

        status_msg = await update.message.reply_text("👀 Дивлюсь...", quote=True)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        
        temp_files = []
        try:
            photo_file = await message.photo[-1].get_file()
            image_path = await download_file(photo_file, f"vis_{message.message_id}")
            temp_files.append(image_path)
            
            messages = await context_manager.get_context(user_id, chat_id, limit=5)
            settings = await get_user_model_settings(user_id)
            
            full_response = ""
            last_len = 0
            
            logger.info(f"   -> Sending to Vision AI...")
            async for chunk in provider.analyze_image(image_path, full_prompt, messages, settings):
                full_response += chunk
                if len(full_response) - last_len > 50:
                    try: await status_msg.edit_text(full_response + " ▌"); last_len = len(full_response)
                    except: pass
            
            await status_msg.delete()
            await send_long_message(message, full_response, parse_mode=ParseMode.HTML)
            await context_manager.save_message(user_id, chat_id, 'user', f"[Photo]: {full_prompt}")
            await context_manager.save_message(user_id, chat_id, 'assistant', full_response)
            logger.info(f"✅ {user_log} Vision response sent.")

        except Exception as e:
            logger.error(f"❌ {user_log} Vision error: {e}")
            await status_msg.edit_text(f"❌ {e}")
        finally: cleanup_files(temp_files)
    else:
        logger.info(f"📸 {user_log} Photo without caption.")
        if update.effective_chat.type == 'private':
            kb = [[InlineKeyboardButton("🖼 Описати", callback_data="photo_desc"), InlineKeyboardButton("📄 Текст (OCR)", callback_data="photo_read")], [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]]
            await update.message.reply_text("Дії із зображенням:", reply_markup=InlineKeyboardMarkup(kb), quote=True)

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_log = get_log_user(user, chat_id)
    
    if update.message.video and update.effective_chat.type != 'private':
        if not should_respond(update, context): return

    media_type = "Voice"
    if update.message.voice: file_obj = update.message.voice; is_video = False
    elif update.message.video_note: file_obj = update.message.video_note; is_video = True; media_type = "Video Note"
    elif update.message.video: file_obj = update.message.video; is_video = True; media_type = "Video File"
    else: return

    logger.info(f"🎙 {user_log} Received {media_type}. Processing...")

    provider = await get_ai_provider(user.id, for_transcription=True)
    if not provider:
        if update.effective_chat.type == 'private': await update.message.reply_text("⚠️ Немає ключа API.")
        return

    status = await update.message.reply_text("📥 Завантажую...", reply_to_message_id=update.message.message_id)
    temp_files = []
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        input_path = await download_file(tg_file, file_obj.file_id)
        temp_files.append(input_path)
        audio_path = await extract_audio(input_path) if is_video else input_path
        if is_video: temp_files.append(audio_path)

        if status: await status.edit_text("🎙 Розпізнаю...")
        settings = await get_user_model_settings(user.id)
        
        logger.info(f"   -> Sending to Whisper...")
        text = await provider.transcribe(audio_path, language=settings.get('language', 'uk'))
        logger.info(f"   -> Raw Transcribed Length: {len(text)}")
        
        if status: await status.edit_text("✨ Оформлюю...")
        clean_text = await beautify_text(user.id, text)
        
        if status: await status.delete()

        if clean_text:
            kb = None
            if update.effective_chat.type == 'private':
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤖 Відправити боту", callback_data="run_gpt")],
                    [InlineKeyboardButton("📝 Підсумувати", callback_data="summarize"), InlineKeyboardButton("✍️ Переформулювати", callback_data="reword")],
                    [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
                ])
            
            await context_manager.save_message(user.id, chat_id, 'transcription', clean_text)
            
            await send_long_message(
                update.message, 
                f"<code>{clean_text}</code>", 
                reply_markup=kb, 
                parse_mode=ParseMode.HTML,
                reply_to_msg_id=update.message.message_id
            )
            logger.info(f"✅ {user_log} Transcription sent.")
            
    except Exception as e:
        logger.error(f"❌ {user_log} Media error: {e}")
        if status: await status.edit_text(f"❌ {e}")
    finally: cleanup_files(temp_files)