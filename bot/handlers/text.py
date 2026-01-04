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

# ОНОВЛЕНИЙ REGEX: Більш "жадібний" та точний для різних піддоменів
# Ловить: https://vm.tiktok.com/ABC, https://www.instagram.com/reel/XYZ, тощо.
USERBOT_REGEX = re.compile(r'(https?://(?:www\.|vm\.|vt\.|m\.|mobile\.)?(?:instagram\.com|tiktok\.com|pin\.it|pinterest\.com)/[\w\d\-_./?=]+)')
DIRECT_REGEX = re.compile(r'https?://(?:[\w-]+\.)?(youtube\.com|youtu\.be|twitter\.com|x\.com)/[^\s]+')

def get_log_user(user, chat_id):
    return f"[User: {user.id} ({user.first_name}) | Chat: {chat_id}]"

async def handle_internal_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    
    if not caption.startswith("task_id:"): return

    try:
        task_id = int(caption.split(":")[1])
        logger.info(f"📥 [MainBot] Received Task Result ID: {task_id}")

        async with AsyncSessionLocal() as session:
            task = await session.get(DownloadQueue, task_id)
            if task:
                logger.info(f"   -> Forwarding to User {task.user_id}...")
                try:
                    await context.bot.copy_message(
                        chat_id=task.user_id,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id,
                        caption="", 
                        reply_to_message_id=task.message_id
                    )
                    logger.info(f"✅ [MainBot] Delivery Success.")
                except Exception as e:
                    logger.error(f"❌ [MainBot] Forward Error: {e}")
                    # Fallback
                    try: await context.bot.copy_message(chat_id=task.user_id, from_chat_id=message.chat_id, message_id=message.message_id, caption="")
                    except: pass
            else:
                logger.warning(f"⚠️ [MainBot] Task {task_id} not found in DB.")
        await message.delete()
    except Exception as e:
        logger.error(f"❌ [MainBot] Internal Handler Error: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user = update.effective_user
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    is_private = update.effective_chat.type == 'private'
    user_log = get_log_user(user, chat_id)
    
    logger.info(f"📩 {user_log} Message: '{text}'")
    
    # 1. Reminders
    if text == "⏰ Нагадування":
        rems = await scheduler_service.get_active_reminders(chat_id)
        if not rems:
            await update.message.reply_text("📭 Немає активних нагадувань.", quote=True)
            return
        settings = await get_user_model_settings(user.id)
        tz_str = settings.get('timezone', BOT_TIMEZONE)
        try: l_tz = zoneinfo.ZoneInfo(tz_str)
        except: l_tz = zoneinfo.ZoneInfo("UTC")
        msg = f"<b>📅 Активні нагадування ({tz_str}):</b>\n\n"
        kb = []
        days = {"Monday":"Пн","Tuesday":"Вт","Wednesday":"Ср","Thursday":"Чт","Friday":"Пт","Saturday":"Сб","Sunday":"Нд"}
        for r in rems:
            t = r.trigger_time.replace(tzinfo=timezone.utc) if r.trigger_time.tzinfo is None else r.trigger_time
            l_dt = t.astimezone(l_tz)
            d = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
            s = l_dt.strftime(f"{d}, %d.%m %H:%M")
            msg += f"🕒 <b>{s}</b>: {r.text}\n"
            kb.append([InlineKeyboardButton(f"❌ {s}", callback_data=f"del_rem_{r.id}")])
        kb.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML", quote=True)
        return

    # 2. Menu
    if text.lower() in ["налаштування", "меню", "настройки", "settings", "menu", "⚙️ налаштування"]:
        has_r = await scheduler_service.get_reminders_count(chat_id) > 0
        btn = [KeyboardButton("⚙️ Налаштування")]
        if has_r: btn.insert(0, KeyboardButton("⏰ Нагадування"))
        await update.message.reply_text("⚙️ <b>Меню:</b>", reply_markup=ReplyKeyboardMarkup([btn], resize_keyboard=True), parse_mode='HTML', quote=True)
        await update.message.reply_text("Оберіть пункт:", reply_markup=get_main_menu_keyboard(), quote=True)
        return

    # 3. Userbot (TikTok/Insta) - ПРІОРИТЕТ 1
    # Шукаємо лінк
    userbot_match = USERBOT_REGEX.search(text)
    if userbot_match:
        link = userbot_match.group(0)
        logger.info(f"🔗 {user_log} Userbot Link Detected: {link}")
        
        try:
            async with AsyncSessionLocal() as session:
                queue_item = DownloadQueue(
                    user_id=chat_id, 
                    message_id=update.message.message_id, 
                    link=link, 
                    status="pending"
                )
                session.add(queue_item)
                await session.commit()
                # Примусово рефрешимо, щоб переконатися, що ID створено
                await session.refresh(queue_item)
                logger.info(f"💾 {user_log} Task Saved to DB (ID: {queue_item.id}). Link: {link}")
            
            if is_private:
                await update.message.reply_text(f"🔗 Передав юзерботу...", quote=True)
                
        except Exception as db_err:
            logger.error(f"❌ {user_log} DB Error while saving task: {db_err}")
            if is_private: await update.message.reply_text(f"❌ Помилка бази даних: {db_err}", quote=True)
        
        return

    # 4. Direct DL
    direct_match = DIRECT_REGEX.search(text)
    if direct_match:
        url = direct_match.group(0)
        logger.info(f"🔗 {user_log} Direct DL Link: {url}")
        status = await update.message.reply_text("⏳ Завантажую...", quote=True) if is_private else None
        try:
            media = await download_media_direct(url)
            if media and os.path.exists(media['path']):
                if status: await status.edit_text("📤 Відправляю...")
                try:
                    if media['type'] == 'video': await update.message.reply_video(video=open(media['path'], 'rb'), reply_to_message_id=update.message.message_id)
                    else: await update.message.reply_document(document=open(media['path'], 'rb'), reply_to_message_id=update.message.message_id)
                except: pass
                if status: await status.delete()
                os.remove(media['path'])
            else:
                if status: await status.edit_text("❌ Не вдалося.")
        except Exception as e:
            logger.error(f"DL Error: {e}")
            if status: await status.edit_text("❌ Помилка.")
        return

    # 5. AI
    if should_respond(update, context):
        p = text
        if update.message.reply_to_message:
            r = update.message.reply_to_message
            name = r.from_user.full_name if r.from_user else "User"
            p = f"--- QUOTE ({name}) ---\n{r.text or r.caption}\n--- END ---\n\nUSER: {text}"
        await context_manager.save_message(user.id, chat_id, 'user', p)
        await process_gpt_request(update, context, user.id)