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

# ВИПРАВЛЕНО: Додано підтримку піддоменів (vm., vt., m. тощо)
USERBOT_REGEX = re.compile(r'https?://(?:[\w-]+\.)?(instagram\.com|tiktok\.com|pin\.it|pinterest\.com)/[^\s]+')
DIRECT_REGEX = re.compile(r'https?://(?:[\w-]+\.)?(youtube\.com|youtu\.be|twitter\.com|x\.com)/[^\s]+')

async def handle_internal_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє медіа, які переслав Userbot"""
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
                    await context.bot.copy_message(
                        chat_id=task.user_id,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id,
                        caption=""
                    )
        await message.delete()
    except Exception as e:
        logger.error(f"Internal task error: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основний обробник тексту"""
    if not update.message or not update.message.text:
        return
        
    user = update.effective_user
    text = update.message.text
    chat_id = update.effective_chat.id
    is_private = update.effective_chat.type == 'private'
    
    # 1. Список Нагадувань
    if text == "⏰ Нагадування":
        reminders = await scheduler_service.get_active_reminders(chat_id)
        if not reminders:
            kb = ReplyKeyboardMarkup([[KeyboardButton("⚙️ Налаштування")]], resize_keyboard=True, is_persistent=True)
            await update.message.reply_text("📭 Немає активних нагадувань.", reply_markup=kb)
            return

        settings = await get_user_model_settings(user.id)
        user_tz_str = settings.get('timezone', BOT_TIMEZONE)
        try: local_tz = zoneinfo.ZoneInfo(user_tz_str)
        except: local_tz = zoneinfo.ZoneInfo("UTC")

        days_map = {
            "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср", 
            "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"
        }

        msg = f"<b>📅 Активні нагадування ({user_tz_str}):</b>\n\n"
        keyboard = []
        for rem in reminders:
            t_utc = rem.trigger_time.replace(tzinfo=timezone.utc) if rem.trigger_time.tzinfo is None else rem.trigger_time
            local_dt = t_utc.astimezone(local_tz)
            
            weekday_name = local_dt.strftime("%A")
            short_weekday = days_map.get(weekday_name, weekday_name[:2])
            local_time_str = local_dt.strftime(f"{short_weekday}, %d.%m %H:%M")
            
            short_text = (rem.text[:25] + '..') if len(rem.text) > 25 else rem.text
            
            msg += f"🕒 <b>{local_time_str}</b>: {rem.text}\n"
            keyboard.append([InlineKeyboardButton(f"❌ {local_time_str} | {short_text}", callback_data=f"del_rem_{rem.id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # 2. Меню Налаштувань
    keywords = ["налаштування", "меню", "настройки", "settings", "menu", "⚙️ налаштування"]
    if text.lower().strip() in keywords:
        has_reminders = await scheduler_service.get_reminders_count(chat_id) > 0
        buttons_row = [KeyboardButton("⚙️ Налаштування")]
        if has_reminders:
            buttons_row.insert(0, KeyboardButton("⏰ Нагадування"))

        reply_keyboard = ReplyKeyboardMarkup([buttons_row], resize_keyboard=True, is_persistent=True)
        
        await update.message.reply_text("⚙️ <b>Меню:</b>", reply_markup=reply_keyboard, parse_mode='HTML')
        await update.message.reply_text("Оберіть пункт:", reply_markup=get_main_menu_keyboard())
        return

    # 3. Userbot (Instagram, TikTok) - ПРІОРИТЕТ ВИЩЕ
    # Перевіряємо TikTok/Insta ДО Youtube/Twitter, щоб випадково не сплутати
    userbot_match = USERBOT_REGEX.search(text)
    if userbot_match:
        link = userbot_match.group(0)
        async with AsyncSessionLocal() as session:
            queue_item = DownloadQueue(
                user_id=chat_id, 
                message_id=update.message.message_id, 
                link=link, 
                status="pending"
            )
            session.add(queue_item)
            await session.commit()
        if is_private:
            await update.message.reply_text(f"🔗 Передав юзерботу...", quote=True)
        return

    # 4. Пряме завантаження (YouTube, Twitter)
    direct_match = DIRECT_REGEX.search(text)
    if direct_match:
        url = direct_match.group(0)
        status_msg = None
        if is_private:
            status_msg = await update.message.reply_text("⏳ Завантажую...", quote=True)
        
        try:
            media_info = await download_media_direct(url)
            if media_info and os.path.exists(media_info['path']):
                if status_msg: await status_msg.edit_text("📤 Відправляю...")
                
                try:
                    if media_info['type'] == 'video':
                        await update.message.reply_video(
                            video=open(media_info['path'], 'rb'),
                            reply_to_message_id=update.message.message_id
                        )
                    else:
                        await update.message.reply_document(
                            document=open(media_info['path'], 'rb'),
                            reply_to_message_id=update.message.message_id
                        )
                except Exception:
                    pass 
                
                if status_msg: await status_msg.delete()
                try: os.remove(media_info['path'])
                except: pass
            else:
                if is_private and status_msg: await status_msg.edit_text("❌ Не вдалося завантажити.")
        except Exception as e:
            logger.error(f"Direct download error: {e}")
            if is_private and status_msg: await status_msg.edit_text("❌ Помилка.")
        return

    # 5. AI Chat
    if should_respond(update, context):
        final_prompt = text
        
        # Обробка цитування (Reply)
        if update.message.reply_to_message:
            reply_msg = update.message.reply_to_message
            quoted_text = reply_msg.text or reply_msg.caption or "[Медіа файл]"
            quoted_author = reply_msg.from_user.full_name if reply_msg.from_user else "Unknown"
            
            final_prompt = (
                f"--- QUOTED MESSAGE FROM {quoted_author} ---\n"
                f"{quoted_text}\n"
                f"--- END QUOTE ---\n\n"
                f"USER REQUEST: {text}"
            )
        
        # Зберігаємо повідомлення в контекст поточного чату
        await context_manager.save_message(user.id, chat_id, 'user', final_prompt)
        await process_gpt_request(update, context, user.id)