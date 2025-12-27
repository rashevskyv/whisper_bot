import logging
import re
import os
import zoneinfo
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
    """Обробляє файли, надіслані Userbot-ом"""
    message = update.message
    caption = message.caption or ""
    
    if not caption.startswith("task_id:"):
        return

    try:
        task_id = int(caption.split(":")[1])
        async with AsyncSessionLocal() as session:
            task = await session.get(DownloadQueue, task_id)
            if task:
                try:
                    await context.bot.copy_message(
                        chat_id=task.user_id,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id,
                        caption="",
                        reply_to_message_id=task.message_id
                    )
                except Exception:
                    try:
                        await context.bot.copy_message(
                            chat_id=task.user_id,
                            from_chat_id=message.chat_id,
                            message_id=message.message_id,
                            caption=""
                        )
                    except: pass
        await message.delete()
    except Exception as e:
        logger.error(f"Internal task error: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    user = update.effective_user
    text = update.message.text
    chat_type = update.effective_chat.type
    is_private = chat_type == 'private'
    
    # 0. Reminders List View
    if text == "⏰ Нагадування":
        reminders = await scheduler_service.get_active_reminders(update.effective_chat.id)
        if not reminders:
            kb = ReplyKeyboardMarkup([[KeyboardButton("⚙️ Налаштування")]], resize_keyboard=True, is_persistent=True)
            await update.message.reply_text("📭 Список нагадувань порожній.", reply_markup=kb)
            return

        settings = await get_user_model_settings(user.id)
        user_tz_str = settings.get('timezone', BOT_TIMEZONE)
        try: local_tz = zoneinfo.ZoneInfo(user_tz_str)
        except: local_tz = zoneinfo.ZoneInfo("UTC")

        msg = f"<b>📅 Активні нагадування ({user_tz_str}):</b>\n\n"
        keyboard = []
        for rem in reminders:
            trigger_time = rem.trigger_time.replace(tzinfo=timezone.utc) if rem.trigger_time.tzinfo is None else rem.trigger_time
            local_time = trigger_time.astimezone(local_tz).strftime("%d.%m %H:%M")
            short_text = (rem.text[:30] + '..') if len(rem.text) > 30 else rem.text
            msg += f"🕒 <b>{local_time}</b>: {rem.text}\n"
            keyboard.append([InlineKeyboardButton(f"❌ {local_time} | {short_text}", callback_data=f"del_rem_{rem.id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 1. Menu Trigger
    keywords = ["налаштування", "меню", "настройки", "settings", "menu", "⚙️ налаштування"]
    if text.lower().strip() in keywords:
        has_reminders = await scheduler_service.get_reminders_count(update.effective_chat.id) > 0
        buttons_row = [KeyboardButton("⚙️ Налаштування")]
        if has_reminders: buttons_row.insert(0, KeyboardButton("⏰ Нагадування"))
        await update.message.reply_text("⚙️ <b>Налаштування:</b>", reply_markup=ReplyKeyboardMarkup([buttons_row], resize_keyboard=True, is_persistent=True), parse_mode='HTML')
        await update.message.reply_text("Оберіть пункт:", reply_markup=get_main_menu_keyboard())
        return

    # 2. Direct DL (YouTube/Twitter)
    direct_match = DIRECT_REGEX.search(text)
    if direct_match:
        url = direct_match.group(0)
        status_msg = await update.message.reply_text("⏳ Завантажую...", quote=True) if is_private else None
        try:
            media_info = await download_media_direct(url)
            if media_info and os.path.exists(media_info['path']):
                if status_msg: await status_msg.edit_text("📤 Відправляю...")
                if media_info['type'] == 'video':
                    await update.message.reply_video(video=open(media_info['path'], 'rb'), quote=True)
                else:
                    await update.message.reply_document(document=open(media_info['path'], 'rb'), quote=True)
                if status_msg: await status_msg.delete()
                os.remove(media_info['path'])
            elif status_msg: await status_msg.edit_text("❌ Не вдалося завантажити.")
        except:
            if status_msg: await status_msg.edit_text("❌ Помилка завантаження.")
        return

    # 3. Userbot (Insta/TikTok)
    userbot_match = USERBOT_REGEX.search(text)
    if userbot_match:
        async with AsyncSessionLocal() as session:
            session.add(DownloadQueue(user_id=update.effective_chat.id, message_id=update.message.message_id, link=userbot_match.group(0)))
            await session.commit()
        if is_private: await update.message.reply_text(f"🔗 Передав замовлення юзерботу...", quote=True)
        return

    # 4. GPT Response with enhanced Context (Mentions & Replies)
    if should_respond(update, context):
        prompt_text = text
        if update.message.reply_to_message:
            reply_msg = update.message.reply_to_message
            # Отримуємо текст процитованого повідомлення
            quoted_text = reply_msg.text or reply_msg.caption or "[Медіа файл]"
            quoted_author = reply_msg.from_user.full_name if reply_msg.from_user else "Unknown"
            
            # Формуємо розширений промпт
            prompt_text = (
                f"--- QUOTED MESSAGE FROM {quoted_author} ---\n"
                f"{quoted_text}\n"
                f"--- END QUOTE ---\n\n"
                f"USER REQUEST: {text}"
            )
        
        await context_manager.save_message(user.id, 'user', prompt_text)
        await process_gpt_request(update, context, user.id)