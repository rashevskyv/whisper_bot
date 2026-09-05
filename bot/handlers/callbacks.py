import logging
import re
import zoneinfo
from datetime import datetime, timezone
from telegram import Update, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from bot.utils.context import context_manager
from bot.handlers.ai import (
    process_gpt_request,
    summarize_text,
    reword_text,
    process_photo_analysis,
    build_draft_reply_markup,
    build_shopping_list_view,
)
from bot.utils.lists import (
    get_user_list,
    get_list_item,
    set_list_item_done,
    delete_list_item,
    clear_done_list_items,
)
from bot.utils.scheduler import scheduler_service
from bot.ai.tools import execute_tool, apply_action_draft_reply
from bot.utils.action_drafts import (
    get_action_draft,
    confirm_action_draft,
    cancel_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_EXPIRED,
)
from bot.handlers.common import get_user_model_settings
from bot.utils.scheduled_tasks import (
    transition_task_occurrence_terminal,
    snooze_task_occurrence,
    OCCURRENCE_STATUS_DONE,
    OCCURRENCE_STATUS_SKIPPED,
    OCCURRENCE_STATUS_SNOOZED,
    OCCURRENCE_STATUS_MISSED,
    OCCURRENCE_STATUS_SCHEDULED,
    OCCURRENCE_STATUS_DELIVERED,
)
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

ERROR_TRANSCRIPTION_NOT_FOUND = "❌ Транскрипцію не знайдено або вона вам не належить."

async def _get_transcription_from_callback(query_data: str, prefix: str, user_id: int, chat_id: int) -> str | None:
    expected = f"{prefix}:"
    if not query_data or not query_data.startswith(expected):
        return None
    raw_id = query_data[len(expected):]
    if not raw_id.isdigit():
        return None
    try:
        transcription_id = int(raw_id)
    except (ValueError, TypeError):
        return None
    return await context_manager.get_transcription_by_id(transcription_id, user_id, chat_id)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    if query.data == "delete_msg":
        await query.message.delete()
        
    elif query.data == "close_menu":
        try: await query.message.delete()
        except: pass
            
    elif query.data.startswith("del_rem_"):
        rem_id = int(query.data.split("_")[2])
        await scheduler_service.delete_reminder_by_id(rem_id, chat_id=chat_id)
        await query.answer("Нагадування видалено!")
        
        active_rems = await scheduler_service.get_active_reminders(chat_id)
        
        if not active_rems:
            await query.message.edit_text("📭 Список нагадувань порожній.")
        else:
            settings = await get_user_model_settings(user.id)
            user_tz_str = settings.get('timezone', BOT_TIMEZONE)
            try: local_tz = zoneinfo.ZoneInfo(user_tz_str)
            except: local_tz = zoneinfo.ZoneInfo("UTC")

            msg = f"<b>📅 Активні нагадування ({user_tz_str}):</b>\n\n"
            keyboard = []
            days = {"Monday":"Пн","Tuesday":"Вт","Wednesday":"Ср","Thursday":"Чт","Friday":"Пт","Saturday":"Сб","Sunday":"Нд"}
            
            for rem in active_rems:
                # Конвертація часу для відображення
                trigger_time = rem.trigger_time
                if trigger_time.tzinfo is None:
                    trigger_time = trigger_time.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
                
                l_dt = trigger_time.astimezone(local_tz)
                d_name = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
                local_time = l_dt.strftime(f"{d_name}, %d.%m %H:%M")
                
                short_text = (rem.text[:25] + '..') if len(rem.text) > 25 else rem.text
                
                msg += f"🕒 <b>{local_time}</b>: {rem.text}\n"
                keyboard.append([InlineKeyboardButton(f"❌ {local_time} | {short_text}", callback_data=f"del_rem_{rem.id}")])
            
            keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])
            await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif query.data == "run_gpt" or (query.data and query.data.startswith("run_gpt:")):
        transcription_text = await _get_transcription_from_callback(query.data, "run_gpt", user.id, chat_id)
        if not transcription_text:
            await query.answer(ERROR_TRANSCRIPTION_NOT_FOUND)
            if query.message:
                try: await query.message.reply_text(ERROR_TRANSCRIPTION_NOT_FOUND)
                except: pass
            return

        await query.answer("Відправляю боту...")
        # Прибираємо кнопки, щоб не натиснути двічі (тільки після перевірки валідності транскрипції)
        try: await query.message.edit_reply_markup(None)
        except: pass

        # 2. "Легалізуємо" текст: зберігаємо як повідомлення користувача
        await context_manager.save_message(user.id, chat_id, 'user', transcription_text)

        # 3. Запускаємо ШІ
        await process_gpt_request(update, context, user.id, manual_text=None)

    elif query.data == "summarize" or (query.data and query.data.startswith("summarize:")):
        transcription_text = await _get_transcription_from_callback(query.data, "summarize", user.id, chat_id)
        if not transcription_text:
            await query.answer(ERROR_TRANSCRIPTION_NOT_FOUND)
            if query.message:
                try: await query.message.reply_text(ERROR_TRANSCRIPTION_NOT_FOUND)
                except: pass
            return

        await query.answer("Роблю вижимку...")
        await summarize_text(update, context, transcription_text)

    elif query.data == "reword" or (query.data and query.data.startswith("reword:")):
        transcription_text = await _get_transcription_from_callback(query.data, "reword", user.id, chat_id)
        if not transcription_text:
            await query.answer(ERROR_TRANSCRIPTION_NOT_FOUND)
            if query.message:
                try: await query.message.reply_text(ERROR_TRANSCRIPTION_NOT_FOUND)
                except: pass
            return

        await query.answer("Переписую...")
        await reword_text(update, context, transcription_text)
            
    elif query.data == "photo_desc":
        await query.answer("Описую...")
        await process_photo_analysis(update, context, "desc")
    elif query.data == "photo_read":
        await query.answer("Читаю...")
        await process_photo_analysis(update, context, "read")
    elif query.data and query.data.startswith("draft:fill:"):
        parts = query.data.split(":")
        if (
            len(parts) != 4
            or parts[0] != "draft"
            or parts[1] != "fill"
            or not parts[2].isdigit()
            or int(parts[2]) <= 0
            or not parts[3].isdigit()
            or int(parts[3]) <= 0
        ):
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        try:
            draft_id = int(parts[2])
            transcription_id = int(parts[3])
        except (ValueError, TypeError):
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        draft = await get_action_draft(draft_id, user.id, chat_id)
        if not draft:
            await query.answer("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)
            return

        if draft.status != DRAFT_STATUS_AWAITING_INFO:
            if draft.status == DRAFT_STATUS_CONFIRMED:
                await query.answer("⚠️ Цю дію вже підтверджено.", show_alert=True)
            elif draft.status == DRAFT_STATUS_CANCELLED:
                await query.answer("❌ Цю дію було скасовано.", show_alert=True)
            elif draft.status == DRAFT_STATUS_EXPIRED:
                await query.answer("⏳ Термін дії чернетки вичерпано.", show_alert=True)
            elif draft.status == DRAFT_STATUS_PENDING_CONFIRMATION:
                await query.answer("⚠️ Дія вже очікує на підтвердження.", show_alert=True)
            else:
                await query.answer("⚠️ Чернетка недоступна для оновлення.", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        exp = (
            draft.expires_at.replace(tzinfo=timezone.utc)
            if draft.expires_at and draft.expires_at.tzinfo is None
            else draft.expires_at
        )
        if exp and exp <= now:
            await query.answer("⏳ Термін дії чернетки вичерпано.", show_alert=True)
            return

        transcription_text = await context_manager.get_transcription_by_id(
            transcription_id,
            user.id,
            chat_id,
        )
        if not transcription_text:
            await query.answer(ERROR_TRANSCRIPTION_NOT_FOUND, show_alert=True)
            return

        # Consume the explicit clarification attempt: remove old action keyboard
        if query.message:
            try:
                await query.message.edit_reply_markup(None)
            except Exception:
                pass

        await query.answer()

        user_tz = BOT_TIMEZONE
        try:
            settings = await get_user_model_settings(user.id)
            if isinstance(settings, dict) and settings.get("timezone"):
                tz_val = str(settings["timezone"]).strip()
                if tz_val:
                    try:
                        zoneinfo.ZoneInfo(tz_val)
                        user_tz = tz_val
                    except Exception:
                        user_tz = BOT_TIMEZONE
        except Exception:
            logger.error(
                f"Failed to get user settings for clarification: draft_id={draft.id}, user_id={user.id}, chat_id={chat_id}"
            )
            user_tz = BOT_TIMEZONE

        try:
            tool_result = await apply_action_draft_reply(
                draft_id=draft.id,
                user_id=user.id,
                chat_id=chat_id,
                reply_text=transcription_text,
                timezone_name=user_tz,
            )
        except Exception:
            logger.error(
                f"Failed to apply clarification reply: draft_id={draft.id}, user_id={user.id}, chat_id={chat_id}"
            )
            fallback_markup = await build_draft_reply_markup(draft.id, user.id, chat_id)
            if query.message:
                await query.message.reply_text(
                    "⚠️ Не вдалося обробити уточнення. Спробуйте ще раз або скасуйте дію.",
                    reply_markup=fallback_markup,
                )
            return

        target_draft_id = tool_result.draft_id or draft.id
        markup = None
        if target_draft_id:
            markup = await build_draft_reply_markup(target_draft_id, user.id, chat_id)

        display_text = tool_result.display_text or "⏳ Термін дії чернетки вичерпано або стан чернетки змінився."
        has_formatting = bool(re.search(r"</?(b|i|code|a|pre)\b", display_text))
        if query.message:
            await query.message.reply_text(
                display_text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML if has_formatting else None,
            )
        return

    elif query.data and query.data.startswith("draft"):
        parts = query.data.split(":")
        if len(parts) != 3 or parts[0] != "draft" or parts[1] not in ("ok", "no") or not parts[2].isdigit() or int(parts[2]) <= 0:
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        action = parts[1]
        try:
            draft_id = int(parts[2])
        except (ValueError, TypeError):
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        draft = await get_action_draft(draft_id, user.id, chat_id)
        if not draft:
            await query.answer("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)
            return

        if action == "no":
            cancelled_draft = await cancel_action_draft(draft_id, user.id, chat_id)
            if not cancelled_draft:
                await query.answer("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)
                return

            if cancelled_draft.status == DRAFT_STATUS_CONFIRMED:
                await query.answer("⚠️ Цю дію вже підтверджено.", show_alert=True)
                if query.message:
                    try:
                        await query.message.edit_reply_markup(None)
                    except Exception:
                        pass
                return

            if cancelled_draft.status == DRAFT_STATUS_EXPIRED:
                await query.answer("⏳ Термін дії чернетки вичерпано.", show_alert=True)
                if query.message:
                    try:
                        await query.message.edit_text("⏳ Термін дії чернетки вичерпано.", reply_markup=None)
                    except Exception:
                        pass
                return

            await query.answer("Дію скасовано.")
            if query.message:
                try:
                    await query.message.edit_text("❌ Дію скасовано.", reply_markup=None)
                except Exception:
                    pass
            return

        elif action == "ok":
            if draft.status == DRAFT_STATUS_AWAITING_INFO:
                await query.answer("⚠️ Недостатньо даних для виконання дії.", show_alert=True)
                return

            if draft.status == DRAFT_STATUS_EXPIRED:
                await query.answer("⏳ Термін дії чернетки вичерпано.", show_alert=True)
                if query.message:
                    try:
                        await query.message.edit_text("⏳ Термін дії чернетки вичерпано.", reply_markup=None)
                    except Exception:
                        pass
                return

            if draft.status == DRAFT_STATUS_CANCELLED:
                await query.answer("❌ Цю дію було скасовано.", show_alert=True)
                if query.message:
                    try:
                        await query.message.edit_text("❌ Дію скасовано.", reply_markup=None)
                    except Exception:
                        pass
                return

            confirmed_draft, transitioned = await confirm_action_draft(draft_id, user.id, chat_id)
            if not confirmed_draft:
                await query.answer("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)
                return

            if not transitioned:
                if confirmed_draft.status == DRAFT_STATUS_CONFIRMED:
                    await query.answer("⚠️ Цю дію вже підтверджено.", show_alert=True)
                    if query.message:
                        try:
                            await query.message.edit_reply_markup(None)
                        except Exception:
                            pass
                elif confirmed_draft.status == DRAFT_STATUS_CANCELLED:
                    await query.answer("❌ Цю дію було скасовано.", show_alert=True)
                    if query.message:
                        try:
                            await query.message.edit_text("❌ Дію скасовано.", reply_markup=None)
                        except Exception:
                            pass
                elif confirmed_draft.status == DRAFT_STATUS_EXPIRED:
                    await query.answer("⏳ Термін дії чернетки вичерпано.", show_alert=True)
                    if query.message:
                        try:
                            await query.message.edit_text("⏳ Термін дії чернетки вичерпано.", reply_markup=None)
                        except Exception:
                            pass
                elif confirmed_draft.status == DRAFT_STATUS_AWAITING_INFO:
                    await query.answer("⚠️ Недостатньо даних для виконання дії.", show_alert=True)
                else:
                    await query.answer("⚠️ Неможливо виконати дію у поточному стані.", show_alert=True)
                    if query.message:
                        try:
                            await query.message.edit_reply_markup(None)
                        except Exception:
                            pass
                return

            await query.answer("Виконую...")

            tool_result = None
            # ponytail: confirmed is the single execution claim; add a durable outbox only when crash-retry guarantees are required.
            try:
                settings = await get_user_model_settings(user.id)
                user_tz_str = settings.get("timezone", BOT_TIMEZONE) if isinstance(settings, dict) else BOT_TIMEZONE
                tool_result = await execute_tool(
                    confirmed_draft.action_type,
                    dict(confirmed_draft.payload),
                    user_id=user.id,
                    chat_id=chat_id,
                    timezone_name=user_tz_str,
                    execute_mutation=True,
                )
            except Exception:
                logger.error(
                    f"Execution error for draft {confirmed_draft.id}, action {confirmed_draft.action_type}, user {user.id}, chat {chat_id}"
                )
                tool_result = None

            if tool_result and tool_result.payload.get("success"):
                if confirmed_draft.action_type == "schedule_reminder":
                    display_text = tool_result.display_text or "✅ Нагадування встановлено!"
                elif confirmed_draft.action_type == "delete_reminder":
                    display_text = "🗑 Нагадування успішно видалено."
                else:
                    display_text = tool_result.display_text or "✅ Дію успішно виконано."

                if query.message:
                    try:
                        await query.message.edit_text(display_text, reply_markup=None, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
            else:
                fail_msg = "⚠️ Дію підтверджено, але не вдалося виконати. Будь ласка, створіть її знову."
                if query.message:
                    try:
                        await query.message.edit_text(fail_msg, reply_markup=None)
                    except Exception:
                        pass
            return
    elif query.data.startswith("occ:"):
        parts = query.data.split(":")
        if len(parts) != 3:
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        _, action, raw_occ_id = parts
        if action not in ("done", "skip", "s15", "s30"):
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        if not raw_occ_id.isdigit():
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        try:
            occurrence_id = int(raw_occ_id)
        except (ValueError, TypeError):
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        if occurrence_id <= 0:
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        if not user or not isinstance(user.id, int) or user.id <= 0:
            await query.answer("❌ Некоректні дані користувача.", show_alert=True)
            return

        if not chat_id or not isinstance(chat_id, int):
            await query.answer("❌ Некоректні дані чату.", show_alert=True)
            return

        if not query.message or not isinstance(query.message.message_id, int) or query.message.message_id <= 0:
            await query.answer("❌ Повідомлення недоступне.", show_alert=True)
            return

        delivery_msg_id = query.message.message_id

        if action in ("done", "skip"):
            target_status = OCCURRENCE_STATUS_DONE if action == "done" else OCCURRENCE_STATUS_SKIPPED
            occ, transitioned = await transition_task_occurrence_terminal(
                occurrence_id=occurrence_id,
                user_id=user.id,
                chat_id=chat_id,
                telegram_message_id=delivery_msg_id,
                target_status=target_status,
            )
            if transitioned:
                if action == "done":
                    await query.answer("✅ Виконано!")
                    status_line = "✅ Позначено виконаним."
                else:
                    await query.answer("⏭ Пропущено")
                    status_line = "⏭ Подію пропущено."

                if query.message:
                    base_text = getattr(query.message, "text_html", None) or getattr(query.message, "text", "")
                    new_text = f"{base_text}\n\n{status_line}" if base_text else status_line
                    try:
                        await query.message.edit_text(new_text, parse_mode=ParseMode.HTML, reply_markup=None)
                    except Exception:
                        try:
                            await query.message.edit_reply_markup(reply_markup=None)
                        except Exception:
                            pass
                return

        elif action in ("s15", "s30"):
            minutes = 15 if action == "s15" else 30
            now = datetime.now(timezone.utc)
            occ, transitioned = await snooze_task_occurrence(
                occurrence_id=occurrence_id,
                user_id=user.id,
                chat_id=chat_id,
                telegram_message_id=delivery_msg_id,
                minutes=minutes,
                now=now,
            )
            if transitioned:
                sched_success = False
                try:
                    scheduler_service.schedule_task_occurrence(occ)
                    sched_success = True
                except Exception:
                    logger.error(
                        f"Failed to register snooze job: occurrence_id={occurrence_id}, "
                        f"user_id={user.id}, chat_id={chat_id}, minutes={minutes}"
                    )

                if sched_success:
                    await query.answer(f"⏰ Відкладено на {minutes} хв")
                    status_line = f"⏰ Відкладено на {minutes} хв."
                else:
                    await query.answer(
                        f"⏰ Відкладено на {minutes} хв (буде відновлено після перезапуску)",
                        show_alert=True,
                    )
                    status_line = f"⏰ Відкладено на {minutes} хв (буде відновлено після перезапуску)."

                if query.message:
                    base_text = getattr(query.message, "text_html", None) or getattr(query.message, "text", "")
                    new_text = f"{base_text}\n\n{status_line}" if base_text else status_line
                    try:
                        await query.message.edit_text(new_text, parse_mode=ParseMode.HTML, reply_markup=None)
                    except Exception:
                        try:
                            await query.message.edit_reply_markup(reply_markup=None)
                        except Exception:
                            pass
                return

        # Losing/stale callback handling (transitioned == False)
        if occ is None:
            # Foreign user/chat or nonexistent occurrence: never remove keyboard
            await query.answer("❌ Завдання не знайдено або воно вам не належить.", show_alert=True)
            return

        if occ.status == OCCURRENCE_STATUS_DONE:
            await query.answer("✅ Це завдання вже відзначено як виконане.", show_alert=True)
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        elif occ.status == OCCURRENCE_STATUS_SKIPPED:
            await query.answer("⏭ Це завдання вже пропущено.", show_alert=True)
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        elif occ.status == OCCURRENCE_STATUS_SNOOZED:
            await query.answer("⏰ Це завдання вже відкладено.", show_alert=True)
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        elif occ.status == OCCURRENCE_STATUS_MISSED:
            await query.answer("⚠️ Термін виконання цього завдання минув.", show_alert=True)
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        elif occ.status == OCCURRENCE_STATUS_SCHEDULED:
            await query.answer("⏳ Дія наразі недоступна для цього завдання.", show_alert=True)
        elif occ.status == OCCURRENCE_STATUS_DELIVERED and occ.telegram_message_id != delivery_msg_id:
            await query.answer("⚠️ Дія недоступна для цього повідомлення.", show_alert=True)
        else:
            await query.answer("⚠️ Дія наразі недоступна.", show_alert=True)
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        return
    elif query.data and query.data.startswith("list:"):
        parts = query.data.split(":")
        valid = False
        action = parts[1] if len(parts) > 1 else None
        list_id = None
        item_id = None

        if len(parts) == 4 and action in ("done", "undo", "del"):
            if parts[2].isdigit() and parts[3].isdigit():
                list_id = int(parts[2])
                item_id = int(parts[3])
                if list_id > 0 and item_id > 0:
                    valid = True
        elif len(parts) == 3 and action == "clear":
            if parts[2].isdigit():
                list_id = int(parts[2])
                if list_id > 0:
                    valid = True

        if (
            not valid
            or not user
            or not isinstance(user.id, int)
            or isinstance(user.id, bool)
            or user.id <= 0
            or not chat_id
            or not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or chat_id == 0
            or not query.message
        ):
            await query.answer("❌ Некоректні дані запиту.", show_alert=True)
            return

        try:
            target_list = await get_user_list(list_id, chat_id)
            if not target_list:
                await query.answer("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
                return

            skip_item_mutation = False
            if action in ("done", "undo", "del"):
                item = await get_list_item(item_id, chat_id)
                if item is None or item.list_id != list_id:
                    if action in ("done", "undo"):
                        await query.answer("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
                        return
                    else:
                        skip_item_mutation = True
                        await query.answer("ℹ️ Пункт уже видалено або недоступний.", show_alert=True)

            if action == "done":
                item_res, changed = await set_list_item_done(
                    item_id=item_id,
                    chat_id=chat_id,
                    actor_user_id=user.id,
                    is_done=True,
                )
                if item_res is None:
                    await query.answer("ℹ️ Пункт уже видалено або не знайдено.", show_alert=True)
                elif changed:
                    await query.answer("✅ Позначено купленим.")
                else:
                    await query.answer("ℹ️ Пункт уже позначено купленим.", show_alert=True)

            elif action == "undo":
                item_res, changed = await set_list_item_done(
                    item_id=item_id,
                    chat_id=chat_id,
                    actor_user_id=user.id,
                    is_done=False,
                )
                if item_res is None:
                    await query.answer("ℹ️ Пункт уже видалено або не знайдено.", show_alert=True)
                elif changed:
                    await query.answer("↩️ Повернуто до активних.")
                else:
                    await query.answer("ℹ️ Пункт уже є активним.", show_alert=True)

            elif action == "del":
                if not skip_item_mutation:
                    deleted = await delete_list_item(
                        item_id=item_id,
                        chat_id=chat_id,
                        actor_user_id=user.id,
                    )
                    if deleted:
                        await query.answer("🗑 Пункт видалено.")
                    else:
                        await query.answer("ℹ️ Пункт уже видалено або недоступний.", show_alert=True)

            elif action == "clear":
                cleared_count = await clear_done_list_items(
                    list_id=list_id,
                    chat_id=chat_id,
                    actor_user_id=user.id,
                )
                if cleared_count is None:
                    await query.answer("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
                    return
                elif cleared_count > 0:
                    await query.answer(f"🧹 Видалено куплених пунктів: {cleared_count}.")
                else:
                    await query.answer("ℹ️ Куплених пунктів немає.", show_alert=True)
        except Exception:
            logger.error(
                f"DB error in list callback: action={action}, list_id={list_id}, item_id={item_id}, chat_id={chat_id}, user_id={user.id}"
            )
            await query.answer("⚠️ Не вдалося оновити список через помилку бази даних. Спробуйте ще раз.", show_alert=True)
            return

        # UI refresh from actual DB
        try:
            new_text, new_markup = await build_shopping_list_view(list_id, chat_id)
            if new_text is not None:
                try:
                    await query.message.edit_text(
                        new_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=new_markup,
                    )
                except Exception as ex:
                    err_msg = str(ex).lower()
                    if "message is not modified" not in err_msg:
                        logger.error(
                            f"Failed to edit shopping message: list_id={list_id}, chat_id={chat_id}"
                        )
                        try:
                            await query.message.reply_text(
                                "⚠️ Дію виконано, але не вдалося оновити повідомлення. Будь ласка, відкрийте список знову."
                            )
                        except Exception:
                            pass
            else:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        except Exception:
            logger.error(
                f"UI refresh exception in list callback: list_id={list_id}, chat_id={chat_id}"
            )
            try:
                await query.message.reply_text(
                    "⚠️ Дію виконано, але не вдалося оновити повідомлення. Будь ласка, відкрийте список знову."
                )
            except Exception:
                pass
        return
    else:
        await query.answer()
