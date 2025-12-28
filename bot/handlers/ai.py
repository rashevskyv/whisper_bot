import logging
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider, send_long_message, clean_html, beautify_text
from bot.utils.context import context_manager
from bot.utils.media import download_file, cleanup_files
from bot.handlers.common import get_user_model_settings, update_user_language
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

async def stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=True):
    """
    Генерує відповідь (стрімінг) з урахуванням chat_id для збереження історії.
    """
    full_response = ""
    last_update_len = 0
    is_streaming_active = True
    
    try:
        async for chunk in provider.generate_stream(messages, settings):
            # Обробка зміни мови через інструмент
            if "__SET_LANGUAGE:" in chunk:
                import re
                match = re.search(r"__SET_LANGUAGE:(\w+)__", chunk)
                if match:
                    new_lang = match.group(1)
                    await update_user_language(user_id, new_lang)
                    chunk = chunk.replace(match.group(0), "")
            
            full_response += chunk
            
            # Якщо текст занадто довгий, перестаємо оновлювати повідомлення в реальному часі,
            # щоб не впертися в ліміти Telegram, але продовжуємо генерувати.
            if len(full_response) > 3800:
                is_streaming_active = False
                if last_update_len < 3800:
                     try:
                        await status_msg.edit_text(full_response[:3800] + "...\n(Генерується далі...)")
                        last_update_len = 4000 
                     except: pass
            
            # Оновлюємо повідомлення кожні ~80 символів
            if is_streaming_active and len(full_response) - last_update_len > 80:
                try:
                    await status_msg.edit_text(full_response + " ▌")
                    last_update_len = len(full_response)
                except Exception:
                    pass
        
        # Фіналізація
        if len(full_response) <= 4000:
            try:
                safe_text = clean_html(full_response)
                await status_msg.edit_text(safe_text, parse_mode=ParseMode.HTML)
            except Exception:
                await status_msg.edit_text(full_response)
        else:
            await status_msg.delete()
            await send_long_message(status_msg.chat, full_response, parse_mode=ParseMode.HTML)
            
        if save_to_history:
            # Зберігаємо відповідь тільки в контекст поточного чату
            await context_manager.save_message(user_id, chat_id, 'assistant', full_response)
            
    except Exception as e:
        logger.error(f"AI Error: {e}")
        try:
            await status_msg.edit_text(f"❌ {str(e)}")
        except:
            pass

async def process_gpt_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, manual_text: str = None):
    """Основна функція обробки текстових запитів"""
    provider = await get_ai_provider(user_id)
    if not provider:
        return
        
    chat_id = update.effective_chat.id
    
    if update.callback_query:
        msg_func = update.callback_query.message.reply_text
    else:
        msg_func = update.message.reply_text
        
    status_msg = await msg_func("⏳")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    settings = await get_user_model_settings(user_id)
    settings['user_id'] = user_id
    settings['chat_id'] = chat_id
    
    # Отримуємо контекст, специфічний для цього чату
    messages = await context_manager.get_context(user_id, chat_id, limit=20)
    
    if manual_text:
        messages.append({"role": "user", "content": manual_text})
        
    await stream_response(provider, messages, status_msg, user_id, chat_id, settings)

async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_summarize: str):
    """Функція для кнопки 'Підсумувати'"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    provider = await get_ai_provider(user_id)
    if not provider:
        return
        
    status_msg = await update.callback_query.message.reply_text("📝 Аналізую...")
    
    messages = [
        {"role": "system", "content": DEFAULT_SETTINGS['summary_prompt']},
        {"role": "user", "content": text_to_summarize}
    ]
    
    settings = await get_user_model_settings(user_id)
    settings['allow_search'] = False 
    settings['chat_id'] = chat_id
    
    await stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=False)

async def reword_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_reword: str):
    """Функція для кнопки 'Переформулювати'"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    provider = await get_ai_provider(user_id)
    if not provider:
        return
        
    status_msg = await update.callback_query.message.reply_text("✍️ Переписую...")
    
    messages = [
        {"role": "system", "content": DEFAULT_SETTINGS['reword_prompt']},
        {"role": "user", "content": text_to_reword}
    ]
    
    settings = await get_user_model_settings(user_id)
    settings['allow_search'] = False
    settings['chat_id'] = chat_id
    
    await stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=False)

async def process_photo_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Обробка фото (Опис або OCR) через меню"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    provider = await get_ai_provider(user_id)
    if not provider:
        return
        
    menu_message = update.callback_query.message
    photo_message = menu_message.reply_to_message
    
    if not photo_message:
        await menu_message.reply_text("❌ Помилка: не можу знайти оригінальне фото.")
        return
        
    photo_file_id = None
    if photo_message.photo:
        photo_file_id = photo_message.photo[-1].file_id
    elif photo_message.document:
        photo_file_id = photo_message.document.file_id
        
    if not photo_file_id:
        await menu_message.reply_text("❌ Фото не знайдено.")
        return

    status_msg = await menu_message.reply_text("👀 Дивлюсь...", quote=True)
    
    prompt = "Опиши детально, що на зображенні." if mode == "desc" else "Випиши весь текст з зображення."
    action_label = "[User asked for Photo Description]" if mode == "desc" else "[User asked for OCR]"
    
    temp_files = []
    try:
        tg_file = await context.bot.get_file(photo_file_id)
        image_path = await download_file(tg_file, f"photo_{photo_message.message_id}")
        temp_files.append(image_path)
        
        # Отримуємо контекст чату для розуміння запиту
        messages = await context_manager.get_context(user_id, chat_id, limit=5)
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
                    
        if len(full_response) <= 4000:
            try:
                safe_text = clean_html(full_response)
                await status_msg.edit_text(safe_text, parse_mode=ParseMode.HTML)
            except Exception:
                await status_msg.edit_text(full_response)
        else:
            await status_msg.delete()
            await send_long_message(menu_message.chat, full_response, parse_mode=ParseMode.HTML)
            
        # Зберігаємо дію та результат в історію чату
        await context_manager.save_message(user_id, chat_id, 'user', action_label)
        await context_manager.save_message(user_id, chat_id, 'assistant', full_response)
        
    except Exception as e:
        logger.error(f"Vision error: {e}")
        await status_msg.edit_text(f"❌ Помилка: {e}")
    finally:
        cleanup_files(temp_files)