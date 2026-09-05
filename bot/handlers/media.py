import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider, send_long_message, beautify_text
from bot.utils.context import context_manager
from bot.utils.media import download_file, extract_audio, cleanup_files, validate_audio_size
from bot.utils.limits import check_transcription_limit, record_transcription_usage
from bot.utils.action_drafts import (
    get_active_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
)
from bot.handlers.common import should_respond, get_user_model_settings, MEDIA_GROUP_CACHE

logger = logging.getLogger(__name__)

def get_log_user(user, chat_id):
    return f"[User: {user.id} ({user.first_name}) | Chat: {chat_id}]"

async def process_vision_request_from_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_message, prompt_text: str):
    """
    Експортована функція для обробки Vision запитів з інших хендлерів (наприклад, з text.py).
    Приймає update (текст запиту) та photo_message (об'єкт повідомлення з фото).
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_log = get_log_user(update.effective_user, chat_id)

    # 1. Визначаємо ID файлу з об'єкта повідомлення (це може бути фото або документ)
    photo_file_id = None
    if photo_message.photo:
        # Беремо останнє (найбільше) фото
        photo_file_id = photo_message.photo[-1].file_id
    elif photo_message.document:
        photo_file_id = photo_message.document.file_id

    if not photo_file_id:
        await update.message.reply_text("❌ Помилка: Не знайдено зображення для аналізу.")
        return

    # 2. Повідомляємо користувача про початок обробки
    status_msg = await update.message.reply_text("👀 Дивлюсь...", quote=True)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    provider = await get_ai_provider(user_id)
    if not provider:
        await status_msg.edit_text("❌ Немає доступу до AI (відсутній провайдер).")
        return

    temp_files = []
    try:
        # 3. Завантаження файлу
        tg_file = await context.bot.get_file(photo_file_id)
        # Генеруємо унікальне ім'я
        image_path = await download_file(tg_file, f"vis_reply_{photo_message.message_id}")
        temp_files.append(image_path)

        # 4. Підготовка контексту
        messages = await context_manager.get_context(user_id, chat_id, limit=5)
        settings = await get_user_model_settings(user_id)

        full_response = ""
        last_len = 0

        # 5. Виклик Vision API
        async for chunk in provider.analyze_image(image_path, prompt_text, messages, settings):
            full_response += chunk
            # Оновлюємо статус не надто часто
            if len(full_response) - last_len > 50:
                try:
                    await status_msg.edit_text(full_response + " ▌")
                    last_len = len(full_response)
                except: pass

        await status_msg.delete()

        # 6. Відправка фінальної відповіді
        # Додаємо назву моделі, якщо увімкнено дебаг
        if settings.get('show_model_name', False):
            model_name = settings.get('model', 'unknown')
            full_response = f"[{model_name}]\n{full_response}"

        await send_long_message(update.message, full_response, parse_mode=ParseMode.HTML, reply_to_msg_id=update.message.message_id)

        # 7. Збереження в історію
        await context_manager.save_message(user_id, chat_id, 'user', f"[Vision Reply to {photo_message.message_id}]: {prompt_text}")
        await context_manager.save_message(user_id, chat_id, 'assistant', full_response)

        logger.info(f"✅ {user_log} Vision response sent via Reply scenario.")

    except Exception as e:
        logger.error(f"❌ {user_log} Vision Error (External): {e}")
        await status_msg.edit_text(f"❌ Помилка обробки зображення: {e}")
    finally:
        cleanup_files(temp_files)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Стандартна обробка фото (якщо воно надіслане як фото)."""
    if not update.message: return

    # У групах реагуємо тільки на реплаї або явні тригери, якщо це не приват
    if not should_respond(update, context) and update.effective_chat.type != 'private':
        return

    message = update.message
    caption = message.caption
    media_group_id = message.media_group_id
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_log = get_log_user(update.effective_user, chat_id)

    # Кешування для альбомів
    if media_group_id:
        if caption: MEDIA_GROUP_CACHE[media_group_id] = caption
        elif media_group_id in MEDIA_GROUP_CACHE: caption = MEDIA_GROUP_CACHE[media_group_id]

    # Якщо є підпис (Caption) — це запит до фото
    if caption:
        logger.info(f"📸 {user_log} Vision Request with Caption: '{caption[:20]}...'")
        provider = await get_ai_provider(user_id)
        if not provider: return

        final_prompt = caption
        # Якщо це реплай на інше повідомлення, додаємо його текст у контекст
        if message.reply_to_message:
            reply_msg = message.reply_to_message
            final_prompt = f"CONTEXT (User replied to): {reply_msg.text or reply_msg.caption or '[Media]'}\n\nPROMPT: {caption}"

        status_msg = await message.reply_text("👀 Дивлюсь...", quote=True)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        temp_files = []
        try:
            photo_file = await message.photo[-1].get_file()
            image_path = await download_file(photo_file, f"vis_{message.message_id}")
            temp_files.append(image_path)

            messages = await context_manager.get_context(user_id, chat_id, limit=5)
            settings = await get_user_model_settings(user_id)

            full_response = ""
            last_len = 0

            async for chunk in provider.analyze_image(image_path, final_prompt, messages, settings):
                full_response += chunk
                if len(full_response) - last_len > 50:
                    try:
                        await status_msg.edit_text(full_response + " ▌")
                        last_len = len(full_response)
                    except: pass

            await status_msg.delete()

            if settings.get('show_model_name', False):
                model_name = settings.get('model', 'unknown')
                full_response = f"[{model_name}]\n{full_response}"

            await send_long_message(message, full_response, parse_mode=ParseMode.HTML, reply_to_msg_id=message.message_id)

            await context_manager.save_message(user_id, chat_id, 'user', f"[Vision Caption]: {final_prompt}")
            await context_manager.save_message(user_id, chat_id, 'assistant', full_response)
            logger.info(f"✅ {user_log} Vision response sent.")

        except Exception as e:
            logger.error(f"❌ {user_log} Vision error: {e}")
            await status_msg.edit_text(f"❌ Помилка: {e}")
        finally:
            cleanup_files(temp_files)
    else:
        # Фото без підпису: в приваті показуємо меню
        logger.info(f"📸 {user_log} Photo without caption.")
        if update.effective_chat.type == 'private':
            kb = [
                [InlineKeyboardButton("🖼 Описати", callback_data="photo_desc"), InlineKeyboardButton("📄 Текст (OCR)", callback_data="photo_read")],
                [InlineKeyboardButton("💬 Запит до зображення", callback_data="ask_photo_prompt")],
                [InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")]
            ]
            await update.message.reply_text("Дії із зображенням:", reply_markup=InlineKeyboardMarkup(kb), quote=True)

async def handle_voice_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка аудіо та відео-кружечків"""
    if not update.message: return
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_log = get_log_user(user, chat_id)

    if update.message.video and update.effective_chat.type != 'private':
        if not should_respond(update, context): return

    media_type = "Voice"
    if update.message.voice: file_obj = update.message.voice; is_video = False
    elif update.message.video_note: file_obj = update.message.video_note; is_video = True; media_type = "Video Note"
    elif update.message.video: file_obj = update.message.video; is_video = True; media_type = "Video File"
    else: return

    # Перевірка щоденного ліміту транскрибації перед завантаженням/викликом API
    duration = getattr(file_obj, 'duration', 0) or 0
    can_transcribe, limit_msg = await check_transcription_limit(user.id, duration)
    if not can_transcribe:
        if update.effective_chat.type == 'private' or should_respond(update, context):
            await update.message.reply_text(limit_msg)
        return

    logger.info(f"🎙 {user_log} Received {media_type}. Processing...")

    provider = await get_ai_provider(user.id, for_transcription=True)
    if not provider:
        if update.effective_chat.type == 'private': await update.message.reply_text("⚠️ Немає ключа API.")
        return

    status = await update.message.reply_text("📥 Завантажую...", reply_to_message_id=update.message.message_id)
    temp_files = []
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        input_path = await download_file(tg_file, file_obj.file_id)
        temp_files.append(input_path)

        if is_video:
            audio_path = await extract_audio(input_path)
            temp_files.append(audio_path)
        else:
            audio_path = input_path
            validate_audio_size(audio_path)

        if status: await status.edit_text("🎙 Розпізнаю...")
        settings = await get_user_model_settings(chat_id)

        # 1. Транскрибація
        logger.info(f"   -> Sending to Transcribe Model (gpt-transcribe)...")
        raw_text = await provider.transcribe(
            audio_path,
            language=settings.get('language', 'uk'),
            prompt=settings.get('transcription_prompt'),
            keywords=settings.get('transcription_keywords')
        )
        transcription_model = "gpt-transcribe"

        if not raw_text or not raw_text.strip():
            if status: await status.edit_text("⚠️ Не вдалося розпізнати мову або аудіо порожнє.")
            return

        # Фіксуємо використання ліміту тільки після успішного розпізнавання
        await record_transcription_usage(user.id, duration)

        # Debug вивід raw тексту
        if settings.get('show_model_name', False):
            try:
                await send_long_message(update.message, f"[{transcription_model}] <b>Raw:</b>\n{raw_text}", parse_mode=ParseMode.HTML, reply_to_msg_id=update.message.message_id)
            except: pass

        # 2. Оформлення (Beautify)
        if status: await status.edit_text("✨ Оформлюю...")
        clean_text, beautify_model = await beautify_text(user.id, raw_text)

        if status: await status.delete()

        if clean_text:
            transcription_id = await context_manager.save_message(user.id, chat_id, 'transcription', clean_text)

            active_draft = None
            if transcription_id is not None and isinstance(transcription_id, int) and transcription_id > 0:
                try:
                    active_draft = await get_active_action_draft(user.id, chat_id)
                except Exception:
                    logger.error(
                        f"Failed to check active action draft: transcription_id={transcription_id}, user_id={user.id}, chat_id={chat_id}"
                    )
                    active_draft = None

            has_clarification = (
                active_draft is not None
                and active_draft.status == DRAFT_STATUS_AWAITING_INFO
                and isinstance(active_draft.id, int)
                and active_draft.id > 0
            )

            kb = None
            if update.effective_chat.type == 'private':
                buttons = []
                if transcription_id is not None:
                    if has_clarification:
                        buttons.append([
                            InlineKeyboardButton(
                                "↩️ Використати як уточнення",
                                callback_data=f"draft:fill:{active_draft.id}:{transcription_id}"
                            )
                        ])
                    buttons.append([InlineKeyboardButton("▶️ Обробити як інструкцію", callback_data=f"run_gpt:{transcription_id}")])
                    buttons.append([
                        InlineKeyboardButton("📝 Підсумувати", callback_data=f"summarize:{transcription_id}"),
                        InlineKeyboardButton("✍️ Переформулювати", callback_data=f"reword:{transcription_id}")
                    ])
                buttons.append([InlineKeyboardButton("🗑 Видалити", callback_data="delete_msg")])
                kb = InlineKeyboardMarkup(buttons)
            else:
                if has_clarification and transcription_id is not None:
                    kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "↩️ Використати як уточнення",
                                callback_data=f"draft:fill:{active_draft.id}:{transcription_id}"
                            )
                        ]
                    ])

            final_output = clean_text
            if settings.get('show_model_name', False):
                final_output = f"[{beautify_model}]\n{clean_text}"

            await send_long_message(
                update.message,
                f"<code>{final_output}</code>",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                reply_to_msg_id=update.message.message_id
            )
            logger.info(f"✅ {user_log} Transcription sent.")

    except Exception as e:
        logger.error(f"❌ {user_log} Media error: {e}")
        if status: await status.edit_text(f"❌ {e}")
    finally: cleanup_files(temp_files)