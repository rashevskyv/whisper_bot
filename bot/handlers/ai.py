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

async def stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=True, reply_to_msg_id=None):
    full_response = ""
    last_update_len = 0
    is_streaming_active = True
    
    try:
        async for chunk in provider.generate_stream(messages, settings):
            if "__SET_LANGUAGE:" in chunk:
                import re
                match = re.search(r"__SET_LANGUAGE:(\w+)__", chunk)
                if match:
                    await update_user_language(user_id, match.group(1))
                    chunk = chunk.replace(match.group(0), "")
            
            full_response += chunk
            if len(full_response) > 3800:
                is_streaming_active = False
                if last_update_len < 3800:
                     try:
                        await status_msg.edit_text(full_response[:3800] + "...\n(Генерується далі...)")
                        last_update_len = 4000 
                     except: pass
            
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
            # ТУТ ВАЖЛИВО: Передаємо ID для реплаю
            await send_long_message(status_msg.chat, full_response, parse_mode=ParseMode.HTML, reply_to_msg_id=reply_to_msg_id)
            
        if save_to_history:
            await context_manager.save_message(user_id, chat_id, 'assistant', full_response)
            
    except Exception as e:
        logger.error(f"AI Error: {e}")
        try: await status_msg.edit_text(f"❌ {str(e)}")
        except: pass

async def process_gpt_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, manual_text: str = None):
    provider = await get_ai_provider(user_id)
    if not provider: return
        
    chat_id = update.effective_chat.id
    
    # Визначаємо ID повідомлення, на яке треба відповідати
    if update.callback_query:
        # Якщо це кнопка, відповідаємо на повідомлення з кнопкою
        reply_to_id = update.callback_query.message.message_id
        msg_func = update.callback_query.message.reply_text
    else:
        # Якщо це текст, відповідаємо на повідомлення користувача
        reply_to_id = update.message.message_id
        msg_func = update.message.reply_text
        
    status_msg = await msg_func("⏳", quote=True)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    settings = await get_user_model_settings(user_id)
    settings['user_id'] = user_id
    settings['chat_id'] = chat_id
    
    messages = await context_manager.get_context(user_id, chat_id, limit=20)
    
    if manual_text:
        messages.append({"role": "user", "content": manual_text})
        
    await stream_response(provider, messages, status_msg, user_id, chat_id, settings, reply_to_msg_id=reply_to_id)

async def summarize_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_summarize: str):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    provider = await get_ai_provider(user_id)
    if not provider: return
        
    reply_id = update.callback_query.message.message_id
    status_msg = await update.callback_query.message.reply_text("📝 Аналізую...", quote=True)
    
    messages = [
        {"role": "system", "content": DEFAULT_SETTINGS['summary_prompt']},
        {"role": "user", "content": text_to_summarize}
    ]
    
    settings = await get_user_model_settings(user_id)
    settings.update({'allow_search': False, 'chat_id': chat_id})
    
    await stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=False, reply_to_msg_id=reply_id)

async def reword_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_reword: str):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    provider = await get_ai_provider(user_id)
    if not provider: return
        
    reply_id = update.callback_query.message.message_id
    status_msg = await update.callback_query.message.reply_text("✍️ Переписую...", quote=True)
    
    messages = [
        {"role": "system", "content": DEFAULT_SETTINGS['reword_prompt']},
        {"role": "user", "content": text_to_reword}
    ]
    
    settings = await get_user_model_settings(user_id)
    settings.update({'allow_search': False, 'chat_id': chat_id})
    
    await stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=False, reply_to_msg_id=reply_id)

async def process_photo_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    provider = await get_ai_provider(user_id)
    if not provider: return
        
    menu_message = update.callback_query.message
    photo_message = menu_message.reply_to_message
    
    if not photo_message:
        await menu_message.reply_text("❌ Помилка: не можу знайти оригінальне фото.")
        return
        
    photo_file_id = photo_message.photo[-1].file_id if photo_message.photo else photo_message.document.file_id
    if not photo_file_id:
        await menu_message.reply_text("❌ Фото не знайдено.")
        return

    status_msg = await menu_message.reply_text("👀 Дивлюсь...", quote=True)
    
    prompt = "Опиши детально." if mode == "desc" else "Випиши текст."
    temp_files = []
    try:
        tg_file = await context.bot.get_file(photo_file_id)
        image_path = await download_file(tg_file, f"photo_{photo_message.message_id}")
        temp_files.append(image_path)
        
        messages = await context_manager.get_context(user_id, chat_id, limit=5)
        full_response = ""
        last_len = 0
        
        async for chunk in provider.analyze_image(image_path, prompt, messages):
            full_response += chunk
            if len(full_response) - last_len > 50:
                try: await status_msg.edit_text(full_response + " ▌"); last_len = len(full_response)
                except: pass
                    
        await status_msg.delete()
        # Реплай на меню (яке є реплаєм на фото), або прямо на фото? 
        # Краще на повідомлення з меню, щоб зберегти ланцюжок.
        await send_long_message(menu_message.chat, full_response, parse_mode=ParseMode.HTML, reply_to_msg_id=menu_message.message_id)
        
        await context_manager.save_message(user_id, chat_id, 'user', f"Action: {mode}")
        await context_manager.save_message(user_id, chat_id, 'assistant', full_response)
        
    except Exception as e:
        logger.error(f"Vision error: {e}")
        await status_msg.edit_text(f"❌ {e}")
    finally:
        cleanup_files(temp_files)