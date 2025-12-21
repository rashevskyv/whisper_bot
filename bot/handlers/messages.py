import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files
from config import DEFAULT_SETTINGS, BOT_TRIGGERS

logger = logging.getLogger(__name__)

def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_type = update.effective_chat.type
    if chat_type == 'private': return True

    message = update.message
    if not message: return False

    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        return True

    text = (message.text or message.caption or "").lower()
    bot_username = context.bot.username.lower()
    triggers = BOT_TRIGGERS + [f"@{bot_username}"]
    
    if any(trigger in text for trigger in triggers):
        return True

    return False

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    if not text: return

    if should_respond(update, context):
        await context_manager.save_message(user.id, 'user', text)
        await process_gpt_request(update, context, user.id)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка фото: Відправляє меню вибору дій"""
    
    if not should_respond(update, context):
        return

    # Меню залишається, даємо quote=True, щоб точно прив'язатися до фото
    keyboard = [
        [
            InlineKeyboardButton("🖼 Описати", callback_data="photo_desc"),
            InlineKeyboardButton("📄 Текст (OCR)", callback_data="photo_read")
        ],
        [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Що зробити з цим зображенням?",
        reply_markup=reply_markup,
        quote=True
    )

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type
    
    if update.message.video and chat_type != 'private':
        if not should_respond(update, context): return

    if update.message.voice:
        file_obj = update.message.voice
        is_video = False
    elif update.message.video_note:
        file_obj = update.message.video_note
        is_video = True
    elif update.message.video:
        file_obj = update.message.video
        is_video = True
    else:
        return

    provider = await get_ai_provider(user.id)
    if not provider:
        if chat_type == 'private': await update.message.reply_text("⚠️ Немає доступу до AI.")
        return

    status_msg = None
    if chat_type == 'private':
        status_msg = await update.message.reply_text("📥 Завантажую...")
    
    temp_files = []
    
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        input_path = await download_file(tg_file, file_obj.file_id)
        temp_files.append(input_path)

        if is_video:
            if status_msg: await status_msg.edit_text("⚙️ Витягую аудіо...")
            audio_path = await extract_audio(input_path)
            temp_files.append(audio_path)
        else:
            audio_path = input_path

        if status_msg: await status_msg.edit_text("🎙 Розпізнаю...")
        transcription = await provider.transcribe(audio_path)
        
        if status_msg: await status_msg.delete()

        if transcription:
            await context_manager.save_message(user.id, 'user', f"[Транскрипція]: {transcription}")
            
            reply_markup = None
            if chat_type == 'private':
                keyboard = [
                    [
                        InlineKeyboardButton("🤖 Відправити боту", callback_data="run_gpt"),
                        InlineKeyboardButton("📝 Підсумувати", callback_data="summarize")
                    ],
                    [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"<code>{transcription}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )

    except Exception as e:
        logger.error(f"Media error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Помилка: {e}")
    finally:
        cleanup_files(temp_files)

async def process_gpt_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    provider = await get_ai_provider(user_id)
    if not provider: return

    msg_func = update.callback_query.message.reply_text if update.callback_query else update.message.reply_text
    status_msg = await msg_func("⏳")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    messages = await context_manager.get_context(user_id, limit=20)
    await stream_response(provider, messages, status_msg, user_id)

async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_summarize: str):
    user_id = update.effective_user.id
    provider = await get_ai_provider(user_id)
    if not provider: return

    # ОНОВЛЕНО: Відповідаємо новим повідомленням, щоб не затирати кнопки
    status_msg = await update.callback_query.message.reply_text("📝 Аналізую...")
    
    messages = [
        {"role": "system", "content": DEFAULT_SETTINGS['summary_prompt']},
        {"role": "user", "content": text_to_summarize}
    ]
    await stream_response(provider, messages, status_msg, user_id, save_to_history=False)

async def process_photo_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Обробка фото після натискання кнопки"""
    user_id = update.effective_user.id
    provider = await get_ai_provider(user_id)
    if not provider: return

    # Отримуємо повідомлення з фото (на яке відповіло наше меню)
    menu_message = update.callback_query.message
    photo_message = menu_message.reply_to_message
    
    # Якщо це переслане повідомлення, іноді лінк втрачається, 
    # але при явному reply_to_message він має бути
    if not photo_message:
        await menu_message.reply_text("❌ Помилка: не можу знайти оригінальне фото.")
        return

    # Шукаємо фото (або документ, якщо відправили файлом)
    photo_file_id = None
    if photo_message.photo:
        photo_file_id = photo_message.photo[-1].file_id
    elif photo_message.document and photo_message.document.mime_type.startswith('image'):
        photo_file_id = photo_message.document.file_id

    if not photo_file_id:
        await menu_message.reply_text("❌ Фото не знайдено (можливо, це файл без прев'ю).")
        return

    # ВІДПРАВЛЯЄМО НОВЕ ПОВІДОМЛЕННЯ (старе меню не чіпаємо)
    status_msg = await menu_message.reply_text("👀 Дивлюсь...", quote=True)
    
    if mode == "desc":
        prompt = "Опиши детально, що зображено на цьому фото. Якщо є жарт - поясни його."
        action_log = "[Користувач попросив описати фото]"
    elif mode == "read":
        prompt = "Випиши весь текст, який ти бачиш на зображенні. Збережи структуру. Тільки текст."
        action_log = "[Користувач попросив прочитати текст з фото]"
    else:
        return

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
                try:
                    await status_msg.edit_text(full_response + " ▌")
                    last_update_len = len(full_response)
                except Exception:
                    pass

        try:
            await status_msg.edit_text(full_response, parse_mode=ParseMode.HTML)
        except Exception:
            await status_msg.edit_text(full_response)
            
        await context_manager.save_message(user_id, 'user', action_log)
        await context_manager.save_message(user_id, 'assistant', full_response)

    except Exception as e:
        logger.error(f"Vision error: {e}")
        await status_msg.edit_text(f"❌ Помилка: {e}")
    finally:
        cleanup_files(temp_files)

async def stream_response(provider, messages, status_msg, user_id, save_to_history=True):
    full_response = ""
    last_update_len = 0
    try:
        async for chunk in provider.generate_stream(messages, {'model': 'gpt-4o'}):
            full_response += chunk
            if len(full_response) - last_update_len > 50:
                try:
                    await status_msg.edit_text(full_response + " ▌")
                    last_update_len = len(full_response)
                except Exception:
                    pass
        try:
            await status_msg.edit_text(full_response, parse_mode=ParseMode.HTML)
        except Exception:
            await status_msg.edit_text(full_response)
        if save_to_history:
            await context_manager.save_message(user_id, 'assistant', full_response)
    except Exception as e:
        logger.error(f"GPT Error: {e}")
        await status_msg.edit_text(f"❌ {str(e)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # Просто підсвічуємо натискання, але не видаляємо кнопки
    if query.data == "delete_msg":
        await query.message.delete()
        
    elif query.data == "run_gpt":
        await query.answer("Відправляю боту...")
        # Тут кнопки можна прибрати, або залишити - як хочете. 
        # Зараз залишаємо, бо ви просили "buttons should remain". 
        # Але зазвичай для тексту це дивно. Нехай для тексту (run_gpt) видаляються,
        # а для фото (desc/read) залишаються.
        user = update.effective_user
        await query.message.edit_reply_markup(reply_markup=None) 
        await process_gpt_request(update, context, user.id)

    elif query.data == "summarize":
        await query.answer("Роблю вижимку...")
        # Тут не видаляємо кнопки, щоб можна було і боту відправити
        transcription_text = query.message.text
        if transcription_text:
            await summarize_text(update, context, transcription_text)
        else:
            await query.message.reply_text("❌ Помилка читання тексту.")

    elif query.data == "photo_desc":
        await query.answer("Описую...")
        await process_photo_analysis(update, context, "desc")
        
    elif query.data == "photo_read":
        await query.answer("Читаю текст...")
        await process_photo_analysis(update, context, "read")
    
    else:
        await query.answer()