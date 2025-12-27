import logging
import re
import os
import zoneinfo
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from bot.database.session import AsyncSessionLocal
from bot.database.models import DownloadQueue
from bot.utils.context import context_manager
from bot.utils.downloader import download_media_direct
from bot.handlers.settings import get_main_menu_keyboard
from bot.handlers.common import should_respond, get_user_model_settings
from bot.handlers.ai import process_gpt_request
from bot.utils.scheduler import scheduler_service
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

USERBOT_REGEX = re.compile(r'(https?://(www\.)?(instagram\.com|tiktok\.com|pin\.it|pinterest\.com)/[^\s]+)')
DIRECT_REGEX = re.compile(r'(https?://(www\.)?(youtube\.com|youtu\.be|twitter\.com|x\.com)/[^\s]+)')

async def handle_internal_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes media received from Userbot"""
    message = update.message
    caption = message.caption or ""
    if not caption.startswith("task_id:"): return

    try:
        task_id = int(caption.split(":")[1])
        async with AsyncSessionLocal() as session:
            task = await session.get(DownloadQueue, task_id)
            if task:
                await context.bot.copy_message(
                    chat_id=task.user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                    caption="",
                    reply_to_message_id=task.message_id
                )
        await message.delete()
    except Exception as e: logger.error(f"Task forward error: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user = update.effective_user
    text = update.message.text
    chat_type = update.effective_chat.type
    is_private = chat_type == 'private'
    
    # 1. Reminders
    if text == "⏰ Нагадування":
        reminders = await scheduler_service.get_active_reminders(update.effective_chat.id)
        if not reminders:
            await update.message.reply_text("📭 Немає активних нагадувань.")
            return

        settings = await get_user_model_settings(user.id)
        user_tz_str = settings.get('timezone', BOT_TIMEZONE)
        try: local_tz = zoneinfo.ZoneInfo(user_tz_str)
        except: local_tz = zoneinfo.ZoneInfo("UTC")

        days = {"Monday":"Пн","Tuesday":"Вт","Wednesday":"Ср","Thursday":"Чт","Friday":"Пт","Saturday":"Сб","Sunday":"Нд"}

        msg = f"<b>📅 Активні нагадування ({user_tz_str}):</b>\n\n"
        keyboard = []
        for rem in reminders:
            t_utc = rem.trigger_time.replace(tzinfo=timezone.utc) if rem.trigger_time.tzinfo is None else rem.trigger_time
            l_dt = t_utc.astimezone(local_tz)
            d_name = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
            l_time = l_dt.strftime(f"{d_name}, %d.%m %H:%M")
            msg += f"🕒 <b>{l_time}</b>: {rem.text}\n"
            keyboard.append([InlineKeyboardButton(f"❌ {l_time} | {rem.text[:20]}", callback_data=f"del_rem_{rem.id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 2. Settings
    if text.lower().strip() in ["налаштування", "меню", "⚙️ налаштування"]:
        has_rem = await scheduler_service.get_reminders_count(update.effective_chat.id) > 0
        btn = [KeyboardButton("⚙️ Налаштування")]
        if has_rem: btn.insert(0, KeyboardButton("⏰ Нагадування"))
        await update.message.reply_text("⚙️ <b>Меню:</b>", reply_markup=ReplyKeyboardMarkup([btn], resize_keyboard=True))
        await update.message.reply_text("Оберіть пункт:", reply_markup=get_main_menu_keyboard())
        return

    # 3. Direct Download
    dm = DIRECT_REGEX.search(text)
    if dm:
        url = dm.group(0)
        s = await update.message.reply_text("⏳ Завантажую...") if is_private else None
        try:
            m = await download_media_direct(url)
            if m and os.path.exists(m['path']):
                if s: await s.edit_text("📤 Відправляю...")
                if m['type'] == 'video': await update.message.reply_video(video=open(m['path'], 'rb'))
                else: await update.message.reply_document(document=open(m['path'], 'rb'))
                if s: await s.delete()
                os.remove(m['path'])
        except: pass
        return

    # 4. Userbot
    um = USERBOT_REGEX.search(text)
    if um:
        async with AsyncSessionLocal() as session:
            session.add(DownloadQueue(user_id=update.effective_chat.id, message_id=update.message.message_id, link=um.group(0)))
            await session.commit()
        if is_private: await update.message.reply_text("🔗 Передав юзерботу...")
        return

    # 5. GPT
    if should_respond(update, context):
        p = text
        if update.message.reply_to_message:
            r = update.message.reply_to_message
            p = f"--- QUOTE ---\n{r.text or r.caption}\n--- END ---\n\nUSER: {text}"
        await context_manager.save_message(user.id, 'user', p)
        await process_gpt_request(update, context, user.id)