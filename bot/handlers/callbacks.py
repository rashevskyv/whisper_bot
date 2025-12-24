import logging
from telegram import Update
from telegram.ext import ContextTypes
from bot.utils.context import context_manager
from bot.handlers.ai import process_gpt_request, summarize_text, reword_text, process_photo_analysis

logger = logging.getLogger(__name__)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    
    if query.data == "delete_msg":
        await query.message.delete()
        
    elif query.data == "close_menu":
        try:
            await query.message.delete()
        except:
            pass
            
    elif query.data == "run_gpt":
        await query.answer("Відправляю боту...")
        await query.message.edit_reply_markup(None)
        
        transcription_text = await context_manager.get_last_transcription(user.id)
        if not transcription_text:
            transcription_text = query.message.text
            
        await process_gpt_request(update, context, user.id, manual_text=None)
        
    elif query.data == "summarize":
        await query.answer("Роблю вижимку...")
        transcription_text = await context_manager.get_last_transcription(user.id)
        if not transcription_text: transcription_text = query.message.text
            
        if transcription_text:
            await summarize_text(update, context, transcription_text)
        else:
            await query.message.reply_text("❌ Помилка: не знайдено текст.")
            
    elif query.data == "reword":
        await query.answer("Переписую...")
        transcription_text = await context_manager.get_last_transcription(user.id)
        if not transcription_text: transcription_text = query.message.text
            
        if transcription_text:
            await reword_text(update, context, transcription_text)
        else:
            await query.message.reply_text("❌ Помилка: не знайдено текст.")
            
    elif query.data == "photo_desc":
        await query.answer("Описую...")
        await process_photo_analysis(update, context, "desc")
    elif query.data == "photo_read":
        await query.answer("Читаю...")
        await process_photo_analysis(update, context, "read")
    else:
        await query.answer()