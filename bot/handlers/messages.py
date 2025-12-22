import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.helpers import get_ai_provider
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files
from bot.handlers.settings import settings_menu
from config import DEFAULT_SETTINGS, BOT_TRIGGERS, ADMIN_IDS

logger = logging.getLogger(__name__)

def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_type = update.effective_chat.type
    if chat_type == 'private': return True
    message = update.message
    if not message: return False
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id: return True
    text = (message.text or message.caption or "").lower()
    bot_username = context.bot.username.lower()
    triggers = BOT_TRIGGERS + [f"@{bot_username}"]
    if any(trigger in text for trigger in triggers): return True
    return False

async def get_user_model_settings(user_id: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        settings = user.settings if (user and user.settings) else DEFAULT_SETTINGS.copy()
        is_admin = user_id in ADMIN_IDS
        result = await session.execute(
            select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == 'openai', APIKey.is_active == True)
        )
        has_own_key = result.scalar_one_or_none() is not None
        settings['allow_search'] = is_admin or has_own_key
        if 'language' not in settings: settings['language'] = DEFAULT_SETTINGS['language']
        return settings

async def update_user_language(user_id: int, lang_code: str):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            settings = dict(user.settings)
            settings['language'] = lang_code
            user.settings = settings
            await session.commit()
    logger.info(f"User {user_id} language updated to {lang_code}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user = update.effective_user
    text = update.message.text.lower()
    
    # Тригер на слово "налаштування"
    keywords = ["налаштування", "меню", "настройки", "settings", "menu", "⚙️ налаштування"]
    if any(k in text for k in keywords):
        # Робимо таку ж структуру кнопок, як у settings.py
        keyboard = [
            [InlineKeyboardButton("🧠 AI (Модель/Персона)", callback_data="ai_menu")],
            [InlineKeyboardButton("🌐 Мова / Language", callback_data="lang_menu")],
            [InlineKeyboardButton("🔑 Мої ключі API", callback_data="keys_menu")],
            [InlineKeyboardButton("🧹 Очистити пам'ять", callback_data="reset_context")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
        ]
        await update.message.reply_text("⚙️ <b>Головні налаштування:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    if should_respond(update, context):
        await context_manager.save_message(user.id, 'user', update.message.text) # Зберігаємо оригінальний текст
        await process_gpt_request(update, context, user.id)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not should_respond(update, context): return
    keyboard = [
        [InlineKeyboardButton("🖼 Описати", callback_data="photo_desc"), InlineKeyboardButton("📄 Текст (OCR)", callback_data="photo_read")],
        [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
    ]
    await update.message.reply_text("Що зробити з цим зображенням?", reply_markup=InlineKeyboardMarkup(keyboard), quote=True)

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.effective_user
    chat_type = update.effective_chat.type
    
    if update.message.video and chat_type != 'private':
        if not should_respond(update, context): return

    if update.message.voice: file_obj = update.message.voice; is_video = False
    elif update.message.video_note: file_obj = update.message.video_note; is_video = True
    elif update.message.video: file_obj = update.message.video; is_video = True
    else: return

    provider = await get_ai_provider(user.id, force_whisper=True)
    if not provider:
        if chat_type == 'private': await update.message.reply_text("⚠️ Немає доступу до AI.")
        return

    status_msg = None
    if chat_type == 'private': status_msg = await update.message.reply_text("📥 Завантажую...")
    temp_files = []
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        input_path = await download_file(tg_file, file_obj.file_id)
        temp_files.append(input_path)
        audio_path = await extract_audio(input_path) if is_video else input_path
        if is_video: temp_files.append(audio_path)

        if status_msg: await status_msg.edit_text("🎙 Розпізнаю...")
        settings = await get_user_model_settings(user.id)
        lang = settings.get('language', 'uk')
        transcription = await provider.transcribe(audio_path, language=lang)
        if status_msg: await status_msg.delete()

        if transcription:
            await context_manager.save_message(user.id, 'user', f"[Транскрипція]: {transcription}")
            reply_markup = None
            if chat_type == 'private':
                keyboard = [[InlineKeyboardButton("🤖 Відправити боту", callback_data="run_gpt"), InlineKeyboardButton("📝 Підсумувати", callback_data="summarize")], [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"<code>{transcription}</code>", parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Media error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Помилка: {e}")
    finally: cleanup_files(temp_files)

async def process_gpt_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, manual_text: str = None):
    provider = await get_ai_provider(user_id)
    if not provider: return
    msg_func = update.callback_query.message.reply_text if update.callback_query else update.message.reply_text
    status_msg = await msg_func("⏳")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    settings = await get_user_model_settings(user_id)
    messages = await context_manager.get_context(user_id, limit=20)
    if manual_text: messages.append({"role": "user", "content": manual_text})
    await stream_response(provider, messages, status_msg, user_id, settings)

async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_summarize: str):
    user_id = update.effective_user.id
    provider = await get_ai_provider(user_id)
    if not provider: return
    status_msg = await update.callback_query.message.reply_text("📝 Аналізую...")
    messages = [{"role": "system", "content": DEFAULT_SETTINGS['summary_prompt']}, {"role": "user", "content": text_to_summarize}]
    settings = await get_user_model_settings(user_id)
    settings['allow_search'] = False 
    await stream_response(provider, messages, status_msg, user_id, settings, save_to_history=False)

async def process_photo_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    user_id = update.effective_user.id
    provider = await get_ai_provider(user_id)
    if not provider: return
    menu_message = update.callback_query.message
    photo_message = menu_message.reply_to_message
    if not photo_message: await menu_message.reply_text("❌ Помилка фото."); return
    photo_file_id = None
    if photo_message.photo: photo_file_id = photo_message.photo[-1].file_id
    elif photo_message.document: photo_file_id = photo_message.document.file_id
    if not photo_file_id: await menu_message.reply_text("❌ Фото не знайдено."); return
    status_msg = await menu_message.reply_text("👀 Дивлюсь...", quote=True)
    if mode == "desc": prompt = "Опиши детально."; action = "[Опис фото]"
    elif mode == "read": prompt = "Випиши текст."; action = "[OCR]"
    else: return
    temp_files = []
    try:
        tg_file = await context.bot.get_file(photo_file_id)
        image_path = await download_file(tg_file, f"photo_{photo_message.message_id}")
        temp_files.append(image_path)
        messages = await context_manager.get_context(user_id, limit=5)
        full_response = ""
        last_update_len = 0
        async for chunk in provider.analyze_image(image_path, prompt, messages):
            full_response += chunk
            if len(full_response) - last_update_len > 50:
                try: await status_msg.edit_text(full_response + " ▌"); last_update_len = len(full_response)
                except: pass
        try: await status_msg.edit_text(full_response, parse_mode=ParseMode.HTML)
        except: await status_msg.edit_text(full_response)
        await context_manager.save_message(user_id, 'user', action)
        await context_manager.save_message(user_id, 'assistant', full_response)
    except Exception as e:
        logger.error(f"Vision error: {e}")
        await status_msg.edit_text(f"❌ Помилка: {e}")
    finally: cleanup_files(temp_files)

async def stream_response(provider, messages, status_msg, user_id, settings, save_to_history=True):
    full_response = ""
    last_update_len = 0
    try:
        async for chunk in provider.generate_stream(messages, settings):
            if "__SET_LANGUAGE:" in chunk:
                import re
                match = re.search(r"__SET_LANGUAGE:(\w+)__", chunk)
                if match:
                    new_lang = match.group(1)
                    await update_user_language(user_id, new_lang)
                    chunk = chunk.replace(match.group(0), "")
            full_response += chunk
            if len(full_response) - last_update_len > 50:
                try: await status_msg.edit_text(full_response + " ▌"); last_update_len = len(full_response)
                except: pass
        try: await status_msg.edit_text(full_response, parse_mode=ParseMode.HTML)
        except: await status_msg.edit_text(full_response)
        if save_to_history: await context_manager.save_message(user_id, 'assistant', full_response)
    except Exception as e:
        logger.error(f"AI Error: {e}")
        await status_msg.edit_text(f"❌ {str(e)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if query.data == "delete_msg": await query.message.delete()
    elif query.data == "run_gpt":
        await query.answer("Відправляю боту...")
        await query.message.edit_reply_markup(reply_markup=None) 
        transcription_text = query.message.text
        await process_gpt_request(update, context, user.id, manual_text=transcription_text)
    elif query.data == "summarize":
        await query.answer("Роблю вижимку...")
        if query.message.text: await summarize_text(update, context, query.message.text)
        else: await query.message.reply_text("❌ Помилка.")
    elif query.data == "photo_desc": await query.answer("Описую..."); await process_photo_analysis(update, context, "desc")
    elif query.data == "photo_read": await query.answer("Читаю..."); await process_photo_analysis(update, context, "read")
    else: await query.answer()