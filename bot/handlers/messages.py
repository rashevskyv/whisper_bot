import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка звичайного тексту"""
    user = update.effective_user
    text = update.message.text
    
    if not text:
        return

    await context_manager.save_message(user.id, 'user', text)
    await process_gpt_request(update, context, user.id)

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка медіа"""
    user = update.effective_user
    
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
        await update.message.reply_text("⚠️ Немає доступу до AI.")
        return

    status_msg = await update.message.reply_text("📥 Завантажую...")
    temp_files = []
    
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        input_path = await download_file(tg_file, file_obj.file_id)
        temp_files.append(input_path)

        if is_video:
            await status_msg.edit_text("⚙️ Витягую аудіо...")
            audio_path = await extract_audio(input_path)
            temp_files.append(audio_path)
        else:
            audio_path = input_path

        await status_msg.edit_text("🎙 Розпізнаю...")
        transcription = await provider.transcribe(audio_path)
        await status_msg.delete()

        if transcription:
            # Зберігаємо оригінал транскрипції в історію
            await context_manager.save_message(user.id, 'user', f"[Транскрипція]: {transcription}")
            
            # ОНОВЛЕНО: Додано кнопку "Підсумувати"
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
        else:
            await update.message.reply_text("❓ Пусто.")

    except Exception as e:
        logger.error(f"Media error: {e}")
        await status_msg.edit_text(f"❌ Помилка: {e}")
    finally:
        cleanup_files(temp_files)

async def process_gpt_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Стандартний запит з контекстом діалогу"""
    provider = await get_ai_provider(user_id)
    if not provider: return

    msg_func = update.callback_query.message.reply_text if update.callback_query else update.message.reply_text
    status_msg = await msg_func("⏳")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    messages = await context_manager.get_context(user_id, limit=20)
    await stream_response(provider, messages, status_msg, user_id)

async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_summarize: str):
    """Спеціальний запит для сумаризації (без контексту діалогу)"""
    user_id = update.effective_user.id
    provider = await get_ai_provider(user_id)
    if not provider: return

    status_msg = await update.callback_query.message.reply_text("📝 Аналізую...")
    
    # Формуємо ізольований контекст тільки для цієї задачі
    messages = [
        {"role": "system", "content": DEFAULT_SETTINGS['summary_prompt']},
        {"role": "user", "content": text_to_summarize}
    ]
    
    await stream_response(provider, messages, status_msg, user_id, save_to_history=False)

async def stream_response(provider, messages, status_msg, user_id, save_to_history=True):
    """Загальна функція стрімінгу відповіді"""
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
            await status_msg.edit_text(full_response) # Fallback без HTML

        if save_to_history:
            await context_manager.save_message(user_id, 'assistant', full_response)

    except Exception as e:
        logger.error(f"GPT Error: {e}")
        await status_msg.edit_text(f"❌ {str(e)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    
    if query.data == "delete_msg":
        await query.message.delete()
        
    elif query.data == "run_gpt":
        await query.answer("Відправляю боту...")
        # Прибираємо кнопки
        await query.message.edit_reply_markup(reply_markup=None)
        await process_gpt_request(update, context, user.id)

    elif query.data == "summarize":
        await query.answer("Роблю вижимку...")
        # Отримуємо текст транскрипції з повідомлення
        transcription_text = query.message.text
        if transcription_text:
            await summarize_text(update, context, transcription_text)
        else:
            await query.message.reply_text("❌ Не вдалося прочитати текст повідомлення.")
    
    else:
        await query.answer()