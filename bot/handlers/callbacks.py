import logging
import zoneinfo
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from bot.utils.context import context_manager
from bot.handlers.ai import process_gpt_request, summarize_text, reword_text, process_photo_analysis
from bot.utils.scheduler import scheduler_service
from bot.handlers.common import get_user_model_settings
from config import BOT_TIMEZONE

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
            
    elif query.data.startswith("del_rem_"):
        # Видалення нагадування
        rem_id = int(query.data.split("_")[2])
        await scheduler_service.delete_reminder_by_id(rem_id)
        await query.answer("Нагадування видалено!")
        
        # Оновлюємо список
        active_rems = await scheduler_service.get_active_reminders(update.effective_chat.id)
        
        # Оновлення нижньої клавіатури (приховати кнопку якщо 0)
        buttons_row = [KeyboardButton("⚙️ Налаштування")]
        if len(active_rems) > 0:
            buttons_row.insert(0, KeyboardButton("⏰ Нагадування"))
        
        # У Telegram не можна просто оновити ReplyKeyboard з CallbackQuery без надсилання повідомлення.
        # Тому ми оновлюємо лише саме повідомлення зі списком (Inline).
        
        if not active_rems:
            await query.message.edit_text("📭 Список нагадувань порожній.")
        else:
            # Перебудовуємо меню з урахуванням таймзони користувача
            settings = await get_user_model_settings(user.id)
            user_tz_str = settings.get('timezone', BOT_TIMEZONE)
            
            try:
                local_tz = zoneinfo.ZoneInfo(user_tz_str)
            except:
                local_tz = zoneinfo.ZoneInfo("UTC")

            msg = f"<b>📅 Активні нагадування ({user_tz_str}):</b>\n\n"
            keyboard = []
            for rem in active_rems:
                # Таймзона DB -> Local
                trigger_time = rem.trigger_time
                if trigger_time.tzinfo is None:
                    trigger_time = trigger_time.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
                
                local_time = trigger_time.astimezone(local_tz).strftime("%d.%m %H:%M")
                short_text = (rem.text[:30] + '..') if len(rem.text) > 30 else rem.text
                
                msg += f"🕒 <b>{local_time}</b>: {rem.text}\n"
                keyboard.append([InlineKeyboardButton(f"❌ {local_time} | {short_text}", callback_data=f"del_rem_{rem.id}")])
            
            keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
            await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

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