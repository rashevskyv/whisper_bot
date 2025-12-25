import logging
import re
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from bot.database.session import AsyncSessionLocal
from bot.database.models import DownloadQueue
from bot.utils.context import context_manager
from bot.utils.downloader import download_media_direct
from bot.handlers.settings import get_main_menu_keyboard
from bot.handlers.common import should_respond
from bot.handlers.ai import process_gpt_request

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
    
    # 1. Меню
    keywords = ["налаштування", "меню", "настройки", "settings", "menu", "⚙️ налаштування"]
    if text.lower().strip() in keywords:
        # Оновлюємо і нижню клавіатуру теж, щоб вона "прилипла"
        menu_button = KeyboardButton("⚙️ Налаштування")
        reply_keyboard = ReplyKeyboardMarkup([[menu_button]], resize_keyboard=True, is_persistent=True)
        
        await update.message.reply_text(
            "⚙️ <b>Головні налаштування:</b>", 
            reply_markup=reply_keyboard, # Спочатку оновлюємо нижню
            parse_mode='HTML'
        )
        # Потім шлемо інлайн меню
        await update.message.reply_text(
            "Оберіть пункт:",
            reply_markup=get_main_menu_keyboard()
        )
        return

    # 2. Пряме завантаження (YouTube/Twitter)
    direct_match = DIRECT_REGEX.search(text)
    if direct_match:
        url = direct_match.group(0)
        status_msg = None
        if is_private:
            status_msg = await update.message.reply_text("⏳ Завантажую (yt-dlp)...", reply_to_message_id=update.message.message_id)
        
        try:
            media_info = await download_media_direct(url)
            
            if media_info and os.path.exists(media_info['path']):
                if status_msg: await status_msg.edit_text("📤 Відправляю...")
                
                try:
                    if media_info['type'] == 'video':
                        await update.message.reply_video(
                            video=open(media_info['path'], 'rb'),
                            caption=None, 
                            reply_to_message_id=update.message.message_id
                        )
                    else:
                        await update.message.reply_document(
                            document=open(media_info['path'], 'rb'),
                            caption=None, 
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
            logger.error(f"Direct DL error: {e}")
            if is_private and status_msg: await status_msg.edit_text("❌ Помилка.")
        return

    # 3. Userbot
    userbot_match = USERBOT_REGEX.search(text)
    if userbot_match:
        link = userbot_match.group(0)
        async with AsyncSessionLocal() as session:
            queue_item = DownloadQueue(user_id=update.effective_chat.id, message_id=update.message.message_id, link=link, status="pending")
            session.add(queue_item)
            await session.commit()
        if is_private:
            await update.message.reply_text(f"🔗 Качаю через Userbot...", reply_to_message_id=update.message.message_id)
        return

    # 4. GPT
    if should_respond(update, context):
        prompt_text = text
        if update.message.reply_to_message:
            reply_msg = update.message.reply_to_message
            if not reply_msg.photo and not (reply_msg.document and reply_msg.document.mime_type and reply_msg.document.mime_type.startswith('image')):
                reply_content = reply_msg.text or reply_msg.caption or "[Media/File]"
                prompt_text = f"CONTEXT (User replied to this message):\n{reply_content}\n\nUSER REQUEST:\n{text}"
        
        await context_manager.save_message(user.id, 'user', prompt_text)
        await process_gpt_request(update, context, user.id)