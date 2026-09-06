import logging
import re
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ContextTypes
from bot.utils.helpers import get_ai_provider, send_long_message, clean_html, beautify_text
from bot.utils.context import context_manager
from bot.utils.media import download_file, cleanup_files
from bot.utils.action_drafts import (
    get_action_draft,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_AWAITING_INFO,
)
from bot.ai.tools import format_shopping_list_view
from bot.utils.lists import (
    get_user_list,
    list_list_items,
)
from bot.handlers.common import get_user_model_settings, update_user_language, get_effective_timezone
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

async def build_draft_reply_markup(draft_id: Optional[int], user_id: int, chat_id: int) -> Optional[InlineKeyboardMarkup]:
    """Будує inline-кнопки на основі актуального стану збереженої ActionDraft."""
    if not draft_id or not isinstance(draft_id, int):
        return None
    try:
        draft = await get_action_draft(draft_id, user_id, chat_id)
        if not draft:
            return None
        if draft.status == DRAFT_STATUS_PENDING_CONFIRMATION:
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Підтвердити", callback_data=f"draft:ok:{draft.id}"),
                    InlineKeyboardButton("❌ Скасувати", callback_data=f"draft:no:{draft.id}"),
                ]
            ])
        elif draft.status == DRAFT_STATUS_AWAITING_INFO:
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❌ Скасувати", callback_data=f"draft:no:{draft.id}"),
                ]
            ])
    except Exception:
        logger.error(
            f"Failed to build draft reply markup: draft_id={draft_id}, user_id={user_id}, chat_id={chat_id}"
        )
    return None

_build_draft_reply_markup = build_draft_reply_markup


async def build_shopping_list_view(
    list_id: int,
    chat_id: int,
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    """Будує актуальний текст і inline-кнопки для існуючого списку покупок у заданому чаті."""
    if not isinstance(list_id, int) or isinstance(list_id, bool) or list_id <= 0:
        return None, None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        return None, None

    try:
        user_list = await get_user_list(list_id, chat_id)
        if user_list is None:
            return None, None

        items = await list_list_items(list_id, chat_id)
        if items is None:
            return None, None

        text = format_shopping_list_view(user_list.name, items)

        keyboard: list[list[InlineKeyboardButton]] = []
        # ponytail: first 30 item controls; add pagination only when large-list UX needs it.
        for it in items[:30]:
            clean_item_text = re.sub(r"\s+", " ", str(it.text or "")).strip()
            if len(clean_item_text) > 25:
                clean_item_text = clean_item_text[:25].rstrip() + "…"

            label_suffix = f" {clean_item_text}" if clean_item_text else ""
            if it.is_done:
                toggle_btn = InlineKeyboardButton(
                    f"↩️ #{it.id}{label_suffix}",
                    callback_data=f"list:undo:{list_id}:{it.id}",
                )
            else:
                toggle_btn = InlineKeyboardButton(
                    f"✅ #{it.id}{label_suffix}",
                    callback_data=f"list:done:{list_id}:{it.id}",
                )
            del_btn = InlineKeyboardButton(
                "🗑",
                callback_data=f"list:del:{list_id}:{it.id}",
            )
            keyboard.append([toggle_btn, del_btn])

        if any(it.is_done for it in items):
            keyboard.append([
                InlineKeyboardButton("🧹 Очистити куплені", callback_data=f"list:clear:{list_id}")
            ])

        markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        return text, markup
    except Exception:
        logger.error(f"Database error in build_shopping_list_view: list_id={list_id}, chat_id={chat_id}")
        return None, None

async def stream_response(provider, messages, status_msg, user_id, chat_id, settings, save_to_history=True, reply_to_msg_id=None):
    full_response = ""
    last_update_len = 0
    is_streaming_active = True

    if settings.get('show_model_name', False):
        model_name = settings.get('model', 'unknown')
        full_response = f"[{model_name}] "
        last_update_len = len(full_response)

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

        reply_markup = await build_draft_reply_markup(settings.get('_action_draft_id'), user_id, chat_id)
        if reply_markup is None:
            shopping_list_id = settings.get('_shopping_list_id')
            if (
                isinstance(shopping_list_id, int)
                and not isinstance(shopping_list_id, bool)
                and shopping_list_id > 0
            ):
                try:
                    _, shopping_markup = await build_shopping_list_view(shopping_list_id, chat_id)
                    reply_markup = shopping_markup
                except Exception:
                    logger.error(
                        f"Failed to attach shopping markup in stream_response: list_id={shopping_list_id}, chat_id={chat_id}"
                    )
                    reply_markup = None

        if len(full_response) <= 4000:
            try:
                safe_text = clean_html(full_response)
                await status_msg.edit_text(safe_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except Exception:
                await status_msg.edit_text(full_response, reply_markup=reply_markup)
        else:
            await status_msg.delete()
            await send_long_message(
                status_msg.chat,
                full_response,
                parse_mode=ParseMode.HTML,
                reply_to_msg_id=reply_to_msg_id,
                reply_markup=reply_markup
            )

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

    if update.callback_query:
        reply_to_id = update.callback_query.message.message_id
        msg_func = update.callback_query.message.reply_text
    else:
        reply_to_id = update.message.message_id
        msg_func = update.message.reply_text

    status_msg = await msg_func("⏳", quote=True)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    settings = await get_user_model_settings(user_id)
    settings['timezone'] = await get_effective_timezone(user_id, chat_id)
    settings['user_id'] = user_id
    settings['chat_id'] = chat_id
    settings['source_message_id'] = reply_to_id

    messages = await context_manager.get_context(user_id, chat_id, limit=20)

    if manual_text:
        messages.append({"role": "user", "content": manual_text})

    # --- ВИЗНАЧЕННЯ СТАТУСУ КОРИСТУВАЧА ---
    # Це потрібно для персони "Вельможа" (і потенційно інших)
    user_status_label = "CHELIAD (COMMONER)"

    # 1. Якщо це приватний чат - користувач завжди "Пан" (Admin of his own chat)
    if update.effective_chat.type == 'private':
        user_status_label = "PAN (ADMIN)"
    else:
        # 2. Якщо група - перевіряємо адміна
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ['administrator', 'creator']:
                user_status_label = "PAN (ADMIN)"
        except: pass

    # Додаємо системну інструкцію про статус в кінець повідомлень
    # Це невидимо для користувача, але видно для ШІ
    messages.append({
        "role": "system",
        "content": f"[SYSTEM INFO] Current Speaker Status: {user_status_label}. React accordingly to your persona."
    })

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
    settings.update({'allow_search': False, 'disable_tools': True, 'chat_id': chat_id})

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
    settings.update({'allow_search': False, 'disable_tools': True, 'chat_id': chat_id})

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
        settings = await get_user_model_settings(user_id)

        full_response = ""
        last_len = 0

        async for chunk in provider.analyze_image(image_path, prompt, messages, settings):
            full_response += chunk
            if len(full_response) - last_len > 50:
                try: await status_msg.edit_text(full_response + " ▌"); last_len = len(full_response)
                except: pass

        await status_msg.delete()

        if settings.get('show_model_name', False):
            model_name = settings.get('model', 'unknown')
            full_response = f"[{model_name}]\n{full_response}"

        await send_long_message(menu_message.chat, full_response, parse_mode=ParseMode.HTML, reply_to_msg_id=menu_message.message_id)

        await context_manager.save_message(user_id, chat_id, 'user', f"Action: {mode}")
        await context_manager.save_message(user_id, chat_id, 'assistant', full_response)

    except Exception as e:
        logger.error(f"Vision error: {e}")
        await status_msg.edit_text(f"❌ {e}")
    finally:
        cleanup_files(temp_files)