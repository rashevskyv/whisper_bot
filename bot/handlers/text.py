import logging
import re
import os
import zoneinfo
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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

USERBOT_REGEX = re.compile(r'(https?://(?:www\.|vm\.|vt\.|m\.|mobile\.)?(?:instagram\.com|tiktok\.com|pin\.it|pinterest\.com)/[\w\d\-_./?=]+)')
DIRECT_REGEX = re.compile(r'https?://(?:[\w-]+\.)?(youtube\.com|youtu\.be|twitter\.com|x\.com)/[^\s]+')

def get_log_user(user, chat_id):
    return f"[User: {user.id} ({user.first_name}) | Chat: {chat_id}]"

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перевіряє права адміністратора локально"""
    if update.effective_chat.type == 'private':
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"Admin check error: {e}")
        return False

async def remove_keyboard_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Примусово видаляє клавіатуру у користувача в групі.
    Відправляє сервісне повідомлення з ReplyKeyboardRemove і одразу видаляє його.
    """
    if update.effective_chat.type == 'private':
        return

    try:
        # Відправляємо пусте повідомлення, щоб знести клавіатуру
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔄 Оновлення інтерфейсу...",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
            reply_to_message_id=update.message.message_id
        )
        # Одразу видаляємо його, щоб не смітити
        await msg.delete()
    except Exception as e:
        logger.error(f"Failed to remove keyboard: {e}")

async def handle_internal_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""

    if not caption.startswith("task_id:"): return

    try:
        task_id = int(caption.split(":")[1])
        logger.info(f"📥 [MainBot] Received Internal Task Result. ID: {task_id}")

        async with AsyncSessionLocal() as session:
            task = await session.get(DownloadQueue, task_id)

            if task and task.status in ["failed_by_donor", "timeout", "error"]:
                link_to_download = task.link.split("###")[-1] if "USERBOT_ERROR:" in task.link else task.link
                error_prefix = f"⚠️ <b>Userbot Failed</b>: "
                if "USERBOT_ERROR:" in task.link:
                    parts = task.link.split(":")
                    bot_name = parts[1]
                    error_msg = parts[2].split("###")[0]
                    error_prefix += f"@{bot_name} Error: {error_msg}\n"
                else:
                    error_prefix += f"Timeout/General Error with {link_to_download}\n"

                logger.warning(f"🔄 [MainBot] Userbot failed for {link_to_download}. Attempting yt-dlp fallback.")
                status_msg = await context.bot.send_message(task.user_id, f"{error_prefix}⏳ Завантажую через yt-dlp...", reply_to_message_id=task.message_id, parse_mode="HTML")

                try:
                    media_info = await download_media_direct(link_to_download)
                    if media_info and os.path.exists(media_info['path']):
                        await status_msg.edit_text("📤 Відправляю через yt-dlp...")
                        if media_info['type'] == 'video':
                            await context.bot.send_video(task.user_id, video=open(media_info['path'], 'rb'), reply_to_message_id=task.message_id)
                        else:
                            await context.bot.send_document(task.user_id, document=open(media_info['path'], 'rb'), reply_to_message_id=task.message_id)
                        await status_msg.delete()
                        os.remove(media_info['path'])
                        logger.info(f"✅ [MainBot] yt-dlp fallback success for Task {task_id}.")
                    else:
                        await status_msg.edit_text(f"{error_prefix}❌ yt-dlp також не зміг завантажити.")
                        logger.warning(f"❌ [MainBot] yt-dlp also failed for {link_to_download}.")
                except Exception as dl_err:
                    logger.error(f"❌ [MainBot] yt-dlp fatal error: {dl_err}")
                    await status_msg.edit_text(f"{error_prefix}❌ Невідома помилка yt-dlp.")

                await message.delete()
                return

            if task:
                logger.info(f"   -> Delivering to User {task.user_id} (Reply to {task.message_id})...")
                await context.bot.copy_message(chat_id=task.user_id, from_chat_id=message.chat_id, message_id=message.message_id, caption="", reply_to_message_id=task.message_id)
                logger.info(f"✅ [MainBot] Delivery Success.")
            else:
                logger.warning(f"⚠️ [MainBot] Task {task_id} not found.")

        await message.delete()

    except Exception as e:
        logger.error(f"❌ [MainBot] Internal Handler Error: {e}")

async def _process_text_as_vision_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_message, prompt_text: str):
    from bot.handlers.media import process_vision_request_from_text_handler
    await process_vision_request_from_text_handler(update, context, photo_message, prompt_text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return

    message = update.message
    user = update.effective_user
    text = message.text.strip()
    chat_id = update.effective_chat.id
    is_private = update.effective_chat.type == 'private'
    user_log = get_log_user(user, chat_id)

    # 0. Сценарій 1 (Реплай на фото)
    if (message.reply_to_message and
        (message.reply_to_message.photo or
         (message.reply_to_message.document and message.reply_to_message.document.mime_type and message.reply_to_message.document.mime_type.startswith('image')))):

        if not is_private and not should_respond(update, context): return

        logger.info(f"👁️ {user_log} Scenario 1: Text is Reply to Photo. Forcing Vision.")
        await _process_text_as_vision_prompt(update, context, message.reply_to_message, text)
        return

    logger.info(f"📩 {user_log} Message: '{text}'")

    # --- СЦЕНАРІЙ 2 ---
    if should_respond(update, context):
        try:
            history = []
            async for msg in context.application.bot.get_chat_history(chat_id=chat_id, limit=3):
                history.append(msg)

            media_message = None
            for msg in history:
                is_photo = msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('image'))
                if is_photo and msg.from_user.id == user.id:
                    time_diff = abs((message.date.astimezone(timezone.utc) - msg.date.astimezone(timezone.utc)).total_seconds())
                    if time_diff < 5:
                        media_message = msg
                        logger.info(f"   -> Found adjacent photo (Scen 2). Time diff: {time_diff}s.")
                        break

            if media_message:
                await _process_text_as_vision_prompt(update, context, media_message, text)
                return
        except Exception as e:
            logger.debug(f"ℹ️ {user_log} Scen 2 check skipped/failed: {e}")

    # 1. Reminders
    if text == "⏰ Нагадування":
        logger.info(f"🔘 {user_log} Clicked 'Reminders'")

        # ВИПРАВЛЕНО: Примусове видалення кнопок у групі
        if not is_private:
            await remove_keyboard_in_group(update, context)

        reminders = await scheduler_service.get_active_reminders(chat_id)
        if not reminders:
            await update.message.reply_text("📭 Немає активних нагадувань.", quote=True)
            return
        settings = await get_user_model_settings(user.id)
        user_tz_str = settings.get('timezone', BOT_TIMEZONE)
        try: local_tz = zoneinfo.ZoneInfo(user_tz_str)
        except: local_tz = zoneinfo.ZoneInfo("UTC")
        days_map = {"Monday":"Пн","Tuesday":"Вт","Wednesday":"Ср","Thursday":"Чт","Friday":"Пт","Saturday":"Сб","Sunday":"Нд"}
        msg = f"<b>📅 Активні нагадування ({user_tz_str}):</b>\n\n"
        keyboard = []
        for rem in reminders:
            t_utc = rem.trigger_time.replace(tzinfo=timezone.utc) if rem.trigger_time.tzinfo is None else rem.trigger_time
            l_dt = t_utc.astimezone(local_tz)
            d_name = days_map.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
            l_time_str = l_dt.strftime(f"{d_name}, %d.%m %H:%M")
            msg += f"🕒 <b>{l_time_str}</b>: {rem.text}\n"
            keyboard.append([InlineKeyboardButton(f"❌ {l_time_str} | {rem.text[:20]}", callback_data=f"del_rem_{rem.id}")])
        keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML", quote=True)
        return

    # 2. Menu
    if text.lower() in ["налаштування", "меню", "настройки", "settings", "menu", "⚙️ налаштування"]:
        # ВИПРАВЛЕНО: Примусове видалення кнопок у групі
        if not is_private:
            await remove_keyboard_in_group(update, context)

        # Перевірка прав
        if not await is_admin(update, context):
            await update.message.reply_text("🔒 Налаштування доступні лише адміністраторам групи.", quote=True)
            return

        logger.info(f"🔘 {user_log} Opened Menu")
        await update.message.reply_text("⚙️ <b>Меню налаштувань:</b>", reply_markup=get_main_menu_keyboard(), parse_mode='HTML', quote=True)
        return

    # 3. Userbot (TikTok/Insta)
    userbot_match = USERBOT_REGEX.search(text)
    if userbot_match:
        chat_settings = await get_user_model_settings(chat_id)
        if chat_settings.get('video_repost', True):
            link = userbot_match.group(0)
            logger.info(f"🔗 {user_log} Userbot Link: {link}")
            try:
                async with AsyncSessionLocal() as session:
                    queue_item = DownloadQueue(user_id=chat_id, message_id=update.message.message_id, link=link, status="pending")
                    session.add(queue_item)
                    await session.commit()
                    logger.info(f"💾 {user_log} Task Saved (ID: {queue_item.id})")
                if is_private: await update.message.reply_text(f"🔗 Передав юзерботу...", quote=True)
            except Exception as db_err:
                logger.error(f"❌ {user_log} DB Error: {db_err}")
                if is_private: await update.message.reply_text(f"❌ Помилка БД.", quote=True)
            return
        else:
            logger.info(f"⏭️ {user_log} Video Repost disabled for chat {chat_id}. Skipping Userbot download.")

    # 4. Direct DL
    direct_match = DIRECT_REGEX.search(text)
    if direct_match:
        chat_settings = await get_user_model_settings(chat_id)
        if chat_settings.get('video_repost', True):
            url = direct_match.group(0)
            logger.info(f"🔗 {user_log} Direct DL Link: {url}")
            status_msg = await update.message.reply_text("⏳ Завантажую...", quote=True) if is_private else None
            try:
                media_info = await download_media_direct(url)
                if media_info and os.path.exists(media_info['path']):
                    logger.info(f"✅ {user_log} Download OK. Sending...")
                    if status_msg: await status_msg.edit_text("📤 Відправляю...")
                    if media_info['type'] == 'video':
                        await update.message.reply_video(video=open(media_info['path'], 'rb'), reply_to_message_id=update.message.message_id)
                    else:
                        await update.message.reply_document(document=open(media_info['path'], 'rb'), reply_to_message_id=update.message.message_id)
                    if status_msg: await status_msg.delete()
                    os.remove(media_info['path'])
                else:
                    if status_msg: await status_msg.edit_text("❌ Не вдалося.")
            except Exception as e:
                logger.error(f"❌ {user_log} DL Error: {e}")
                if status_msg: await status_msg.edit_text("❌ Помилка.")
            return
        else:
            logger.info(f"⏭️ {user_log} Video Repost disabled for chat {chat_id}. Skipping Direct DL.")

    # 5. AI
    if should_respond(update, context):
        logger.info(f"🤖 {user_log} AI Request: '{text[:20]}...'")
        final_prompt = text
        if update.message.reply_to_message:
            r = update.message.reply_to_message
            author = r.from_user.full_name if r.from_user else "User"
            final_prompt = f"--- QUOTED MESSAGE FROM {author} ---\n{r.text or r.caption or '[Text/Media]'}\n--- END ---\n\nUSER REQUEST: {text}"
            logger.info(f"   -> Added reply context from {author}")

        await context_manager.save_message(user.id, chat_id, 'user', final_prompt)
        await process_gpt_request(update, context, user.id)
    else:
        logger.info(f"🔇 {user_log} Ignored (Group Mode)")