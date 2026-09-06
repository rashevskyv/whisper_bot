import os
import sys
import unittest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from bot.database.models import Base, ActionDraft
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    confirm_action_draft,
    cancel_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_EXPIRED,
)
from bot.handlers.ai import build_draft_reply_markup, _build_draft_reply_markup, stream_response
from bot.handlers.callbacks import handle_callback
from bot.ai.tools import ToolResult
from config import BOT_TIMEZONE


def make_mock_update(callback_data: str, user_id: int = 123, chat_id: int = 456):
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 999
    query.message.reply_text = AsyncMock()
    query.message.edit_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=chat_id)
    return update, query


class TestDraftCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.drafts_session_patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.drafts_session_patcher.start()

    async def asyncTearDown(self):
        self.drafts_session_patcher.stop()
        await self.engine.dispose()

    # --- 1. Keyboard rendering tests ---

    async def test_render_keyboard_pending_confirmation(self):
        """Verify pending_confirmation renders both Confirm and Cancel buttons with valid callback_data < 64 bytes."""
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Doctor", "iso_time_utc": "2026-10-10T10:00:00+00:00"},
            missing_fields=[]
        )
        self.assertEqual(draft.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        markup = await build_draft_reply_markup(draft.id, user_id=1, chat_id=2)
        self.assertIsNotNone(markup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        row = markup.inline_keyboard[0]
        self.assertEqual(len(row), 2)

        confirm_btn, cancel_btn = row[0], row[1]
        self.assertEqual(confirm_btn.text, "✅ Підтвердити")
        self.assertEqual(confirm_btn.callback_data, f"draft:ok:{draft.id}")
        self.assertLess(len(confirm_btn.callback_data.encode("utf-8")), 64)

        self.assertEqual(cancel_btn.text, "❌ Скасувати")
        self.assertEqual(cancel_btn.callback_data, f"draft:no:{draft.id}")
        self.assertLess(len(cancel_btn.callback_data.encode("utf-8")), 64)

    async def test_render_keyboard_awaiting_info(self):
        """Verify awaiting_info renders only Cancel button and never Confirm."""
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        self.assertEqual(draft.status, DRAFT_STATUS_AWAITING_INFO)

        markup = await build_draft_reply_markup(draft.id, user_id=1, chat_id=2)
        self.assertIsNotNone(markup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        row = markup.inline_keyboard[0]
        self.assertEqual(len(row), 1)

        cancel_btn = row[0]
        self.assertEqual(cancel_btn.text, "❌ Скасувати")
        self.assertEqual(cancel_btn.callback_data, f"draft:no:{draft.id}")

    async def test_render_keyboard_terminal_or_missing_or_foreign(self):
        """Verify terminal, non-existent, or foreign drafts receive no keyboard."""
        # Non-existent ID
        self.assertIsNone(await build_draft_reply_markup(99999, user_id=1, chat_id=2))
        self.assertIsNone(await build_draft_reply_markup(None, user_id=1, chat_id=2))
        self.assertIsNone(await build_draft_reply_markup("bad_id", user_id=1, chat_id=2))

        # Foreign user / chat
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="delete_reminder",
            payload={"reminder_id": 5}, missing_fields=[]
        )
        self.assertIsNone(await build_draft_reply_markup(draft.id, user_id=999, chat_id=20))
        self.assertIsNone(await build_draft_reply_markup(draft.id, user_id=10, chat_id=999))

        # Confirmed draft -> None
        await confirm_action_draft(draft.id, user_id=10, chat_id=20)
        self.assertIsNone(await build_draft_reply_markup(draft.id, user_id=10, chat_id=20))

        # Cancelled draft -> None
        draft2 = await create_action_draft(
            user_id=10, chat_id=20, action_type="delete_reminder",
            payload={"reminder_id": 6}, missing_fields=[]
        )
        await cancel_action_draft(draft2.id, user_id=10, chat_id=20)
        self.assertIsNone(await build_draft_reply_markup(draft2.id, user_id=10, chat_id=20))

    async def test_stream_response_attaches_markup_and_passes_to_long_message(self):
        """Verify stream_response attaches draft markup on edit_text and passes to send_long_message."""
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={"text": "Dentist", "iso_time_utc": "2026-11-11T11:00:00+00:00"},
            missing_fields=[]
        )

        async def mock_gen(text):
            yield text

        mock_provider = MagicMock()
        mock_status = MagicMock()
        mock_status.edit_text = AsyncMock()
        mock_status.delete = AsyncMock()
        mock_status.chat = MagicMock()

        # Case A: Response <= 4000 characters
        mock_provider.generate_stream = MagicMock(return_value=mock_gen("Short preview"))
        settings = {"_action_draft_id": draft.id}
        with patch("bot.handlers.ai.context_manager.save_message", AsyncMock()):
            await stream_response(mock_provider, [], mock_status, user_id=10, chat_id=20, settings=settings)

        mock_status.edit_text.assert_awaited()
        call_kwargs = mock_status.edit_text.call_args.kwargs
        self.assertIsNotNone(call_kwargs.get("reply_markup"))
        btn_data = call_kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual(btn_data, f"draft:ok:{draft.id}")

        # Case B: Response > 4000 characters -> passes reply_markup to send_long_message
        mock_status.edit_text.reset_mock()
        long_text = "A" * 4500
        mock_provider.generate_stream = MagicMock(return_value=mock_gen(long_text))
        with patch("bot.handlers.ai.context_manager.save_message", AsyncMock()), \
             patch("bot.handlers.ai.send_long_message", AsyncMock()) as mock_send_long:
            await stream_response(mock_provider, [], mock_status, user_id=10, chat_id=20, settings=settings)

            mock_send_long.assert_awaited_once()
            long_kwargs = mock_send_long.call_args.kwargs
            self.assertIsNotNone(long_kwargs.get("reply_markup"))
            long_btn_data = long_kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            self.assertEqual(long_btn_data, f"draft:ok:{draft.id}")

    # --- 2. Authorization and validation tests ---

    async def test_callback_malformed_data_rejected(self):
        """Verify malformed callback_data performs no DB operation and executes no tool."""
        bad_callbacks = [
            "draft",
            "draft:",
            "draft:ok",
            "draft:unknown:123",
            "draft:ok:-5",
            "draft:ok:0",
            "draft:ok:abc",
            "draft:ok:12:extra",
        ]

        for bad_cb in bad_callbacks:
            update, query = make_mock_update(bad_cb, user_id=1, chat_id=2)
            with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
                await handle_callback(update, MagicMock())
                query.answer.assert_awaited_with("❌ Некоректні дані запиту.", show_alert=True)
                mock_exec.assert_not_called()
                query.message.edit_text.assert_not_called()
                query.message.edit_reply_markup.assert_not_called()

    async def test_callback_foreign_user_cannot_confirm_or_cancel(self):
        """Verify another user in the same chat cannot confirm or cancel a draft."""
        draft = await create_action_draft(
            user_id=100, chat_id=200, action_type="schedule_reminder",
            payload={"text": "Private task", "iso_time_utc": "2026-12-12T12:00:00+00:00"},
            missing_fields=[]
        )

        for action in ("ok", "no"):
            update, query = make_mock_update(f"draft:{action}:{draft.id}", user_id=999, chat_id=200)
            with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
                await handle_callback(update, MagicMock())
                query.answer.assert_awaited_with("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)
                mock_exec.assert_not_called()
                query.message.edit_text.assert_not_called()
                query.message.edit_reply_markup.assert_not_called()

        # Draft remains unchanged
        reloaded = await get_action_draft(draft.id, user_id=100, chat_id=200)
        self.assertEqual(reloaded.status, DRAFT_STATUS_PENDING_CONFIRMATION)

    async def test_callback_wrong_chat_cannot_confirm_or_cancel(self):
        """Verify same user in a different chat cannot confirm or cancel a draft."""
        draft = await create_action_draft(
            user_id=100, chat_id=200, action_type="schedule_reminder",
            payload={"text": "Task", "iso_time_utc": "2026-12-12T12:00:00+00:00"},
            missing_fields=[]
        )

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=100, chat_id=999)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
            await handle_callback(update, MagicMock())
            query.answer.assert_awaited_with("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)
            mock_exec.assert_not_called()

    # --- 3. Cancellation tests ---

    async def test_callback_cancel_active_draft(self):
        """Verify active draft is cancelled, message edited, keyboard removed, and execute_tool not called."""
        draft = await create_action_draft(
            user_id=5, chat_id=6, action_type="schedule_reminder",
            payload={"text": "Will cancel", "iso_time_utc": "2026-12-12T12:00:00+00:00"},
            missing_fields=[]
        )

        update, query = make_mock_update(f"draft:no:{draft.id}", user_id=5, chat_id=6)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
            await handle_callback(update, MagicMock())
            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("Дію скасовано.")
            query.message.edit_text.assert_awaited_once_with("❌ Дію скасовано.", reply_markup=None)

        reloaded = await get_action_draft(draft.id, user_id=5, chat_id=6)
        self.assertEqual(reloaded.status, DRAFT_STATUS_CANCELLED)

    async def test_callback_cancel_repeated_is_harmless(self):
        """Verify repeated cancel callback is idempotent and harmless."""
        draft = await create_action_draft(
            user_id=5, chat_id=6, action_type="delete_reminder",
            payload={"reminder_id": 10}, missing_fields=[]
        )
        await cancel_action_draft(draft.id, user_id=5, chat_id=6)

        update, query = make_mock_update(f"draft:no:{draft.id}", user_id=5, chat_id=6)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
            await handle_callback(update, MagicMock())
            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("Дію скасовано.")

    async def test_callback_cancel_confirmed_draft_rejected(self):
        """Verify cancel on a confirmed draft is rejected, does not mutate status, and removes stale markup."""
        draft = await create_action_draft(
            user_id=5, chat_id=6, action_type="delete_reminder",
            payload={"reminder_id": 10}, missing_fields=[]
        )
        await confirm_action_draft(draft.id, user_id=5, chat_id=6)

        update, query = make_mock_update(f"draft:no:{draft.id}", user_id=5, chat_id=6)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
            await handle_callback(update, MagicMock())
            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("⚠️ Цю дію вже підтверджено.", show_alert=True)
            query.message.edit_reply_markup.assert_awaited_once_with(None)

        reloaded = await get_action_draft(draft.id, user_id=5, chat_id=6)
        self.assertEqual(reloaded.status, DRAFT_STATUS_CONFIRMED)

    # --- 4. Confirmation tests ---

    async def test_callback_confirm_pending_draft_schedule_reminder(self):
        """Verify confirmation winner executes schedule_reminder tool with exact payload and updates message."""
        future_iso = "2026-11-20T15:00:00+00:00"
        draft = await create_action_draft(
            user_id=12, chat_id=24, action_type="schedule_reminder",
            payload={"text": "Doctor visit", "iso_time_utc": future_iso},
            missing_fields=[]
        )

        fake_res = ToolResult(
            payload={"success": True, "reminder_id": 77},
            display_text="\n✅ <b>Встановлено:</b> Пт, 20.11 17:00\n📝 <i>Doctor visit</i>",
            stop=True
        )

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=12, chat_id=24)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock(return_value=fake_res)) as mock_exec, \
             patch("bot.handlers.callbacks.get_effective_timezone", AsyncMock(return_value="Europe/Kyiv")):

            await handle_callback(update, MagicMock())

            mock_exec.assert_awaited_once_with(
                "schedule_reminder",
                {"text": "Doctor visit", "iso_time_utc": future_iso},
                user_id=12,
                chat_id=24,
                timezone_name="Europe/Kyiv",
                execute_mutation=True,
            )

            query.message.edit_text.assert_awaited_once_with(
                fake_res.display_text,
                reply_markup=None,
                parse_mode="HTML"
            )

        reloaded = await get_action_draft(draft.id, user_id=12, chat_id=24)
        self.assertEqual(reloaded.status, DRAFT_STATUS_CONFIRMED)

    async def test_callback_confirm_pending_draft_delete_reminder(self):
        """Verify confirmation winner executes delete_reminder and uses concise fixed Ukrainian success text."""
        draft = await create_action_draft(
            user_id=12, chat_id=24, action_type="delete_reminder",
            payload={"reminder_id": 88},
            missing_fields=[]
        )

        fake_res = ToolResult(payload={"success": True}, stop=False)

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=12, chat_id=24)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock(return_value=fake_res)) as mock_exec:
            await handle_callback(update, MagicMock())

            mock_exec.assert_awaited_once_with(
                "delete_reminder",
                {"reminder_id": 88},
                user_id=12,
                chat_id=24,
                timezone_name=BOT_TIMEZONE,
                execute_mutation=True,
            )

            query.message.edit_text.assert_awaited_once_with(
                "🗑 Нагадування успішно видалено.",
                reply_markup=None,
                parse_mode="HTML"
            )

    async def test_callback_confirm_concurrent_or_repeated_calls_execute_once(self):
        """Verify repeated or concurrent confirm callbacks execute tool exactly once."""
        draft = await create_action_draft(
            user_id=33, chat_id=44, action_type="delete_reminder",
            payload={"reminder_id": 101},
            missing_fields=[]
        )

        fake_res = ToolResult(payload={"success": True}, stop=False)

        mock_exec = AsyncMock(return_value=fake_res)
        with patch("bot.handlers.callbacks.execute_tool", mock_exec):
            update1, query1 = make_mock_update(f"draft:ok:{draft.id}", user_id=33, chat_id=44)
            update2, query2 = make_mock_update(f"draft:ok:{draft.id}", user_id=33, chat_id=44)

            # Concurrent confirmation attempts
            await asyncio.gather(
                handle_callback(update1, MagicMock()),
                handle_callback(update2, MagicMock())
            )

            # Exactly one execution occurred
            self.assertEqual(mock_exec.await_count, 1)

            # A subsequent third call also does not execute
            update3, query3 = make_mock_update(f"draft:ok:{draft.id}", user_id=33, chat_id=44)
            await handle_callback(update3, MagicMock())
            self.assertEqual(mock_exec.await_count, 1)
            query3.answer.assert_awaited_with("⚠️ Цю дію вже підтверджено.", show_alert=True)

    async def test_callback_confirm_awaiting_info_rejected_keeps_cancel(self):
        """Verify confirm on awaiting_info is rejected without execution and leaves cancel available."""
        draft = await create_action_draft(
            user_id=33, chat_id=44, action_type="schedule_reminder",
            payload={"text": "Incomplete"},
            missing_fields=["iso_time_utc"]
        )

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=33, chat_id=44)
        with patch("bot.handlers.callbacks.execute_tool", AsyncMock()) as mock_exec:
            await handle_callback(update, MagicMock())
            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("⚠️ Недостатньо даних для виконання дії.", show_alert=True)
            # Keyboard must NOT be removed so user can cancel
            query.message.edit_reply_markup.assert_not_called()
            query.message.edit_text.assert_not_called()

        reloaded = await get_action_draft(draft.id, user_id=33, chat_id=44)
        self.assertEqual(reloaded.status, DRAFT_STATUS_AWAITING_INFO)

    async def test_callback_confirm_execution_failure_non_retryable_and_no_leak(self):
        """Verify tool execution failure displays stable message, logs no sensitive data, and prevents retry."""
        draft = await create_action_draft(
            user_id=50, chat_id=60, action_type="schedule_reminder",
            payload={"text": "Fail test", "iso_time_utc": "2026-11-20T15:00:00+00:00"},
            missing_fields=[]
        )

        secret_err = "CRITICAL_DB_CREDENTIAL_LEAK_SENTINEL"
        mock_exec = AsyncMock(side_effect=RuntimeError(secret_err))

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=50, chat_id=60)
        with patch("bot.handlers.callbacks.execute_tool", mock_exec), \
             self.assertLogs("bot.handlers.callbacks", level="ERROR") as cm:
            await handle_callback(update, MagicMock())

            mock_exec.assert_awaited_once()
            # Verified failure message does not leak secret error
            query.message.edit_text.assert_awaited_once()
            fail_text = query.message.edit_text.call_args[0][0]
            self.assertNotIn(secret_err, fail_text)
            self.assertIn("⚠️ Дію підтверджено, але не вдалося виконати", fail_text)

            # Assert log records do NOT contain the exception sentinel
            for log_record in cm.output:
                self.assertNotIn(secret_err, log_record)
            # Assert log contains safe identifiers
            self.assertTrue(any(
                str(draft.id) in log_rec and "schedule_reminder" in log_rec and "user 50" in log_rec and "chat 60" in log_rec
                for log_rec in cm.output
            ))

        # Draft remains confirmed
        reloaded = await get_action_draft(draft.id, user_id=50, chat_id=60)
        self.assertEqual(reloaded.status, DRAFT_STATUS_CONFIRMED)

        # Repeated callback must NOT retry execution
        update2, query2 = make_mock_update(f"draft:ok:{draft.id}", user_id=50, chat_id=60)
        with patch("bot.handlers.callbacks.execute_tool", mock_exec):
            await handle_callback(update2, MagicMock())
            self.assertEqual(mock_exec.await_count, 1)

    async def test_callback_confirm_settings_failure_protected_and_no_leak(self):
        """Verify get_effective_timezone failure is caught, logs no secret, removes keyboard, and prevents execution."""
        draft = await create_action_draft(
            user_id=70, chat_id=80, action_type="schedule_reminder",
            payload={"text": "Settings fail test", "iso_time_utc": "2026-11-20T15:00:00+00:00"},
            missing_fields=[]
        )

        secret_settings_err = "SETTINGS_DB_PASSWORD_LEAK_SENTINEL"
        mock_settings = AsyncMock(side_effect=RuntimeError(secret_settings_err))
        mock_exec = AsyncMock()

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=70, chat_id=80)
        with patch("bot.handlers.callbacks.get_effective_timezone", mock_settings), \
             patch("bot.handlers.callbacks.execute_tool", mock_exec), \
             self.assertLogs("bot.handlers.callbacks", level="ERROR") as cm:

            await handle_callback(update, MagicMock())

            # execute_tool must NOT have been called
            mock_exec.assert_not_called()

            # Stable failure text shown and keyboard removed
            query.message.edit_text.assert_awaited_once()
            fail_text = query.message.edit_text.call_args[0][0]
            call_kwargs = query.message.edit_text.call_args.kwargs
            self.assertIsNone(call_kwargs.get("reply_markup"))
            self.assertIn("⚠️ Дію підтверджено, але не вдалося виконати", fail_text)
            self.assertNotIn(secret_settings_err, fail_text)

            # Sentinel absent from logs, safe identifiers present
            for log_rec in cm.output:
                self.assertNotIn(secret_settings_err, log_rec)
            self.assertTrue(any(
                str(draft.id) in log_rec and "schedule_reminder" in log_rec for log_rec in cm.output
            ))

        # Draft is confirmed in DB
        reloaded = await get_action_draft(draft.id, user_id=70, chat_id=80)
        self.assertEqual(reloaded.status, DRAFT_STATUS_CONFIRMED)

        # Repeated callback does not execute tool
        update2, query2 = make_mock_update(f"draft:ok:{draft.id}", user_id=70, chat_id=80)
        with patch("bot.handlers.callbacks.execute_tool", mock_exec):
            await handle_callback(update2, MagicMock())
            mock_exec.assert_not_called()
            query2.answer.assert_awaited_with("⚠️ Цю дію вже підтверджено.", show_alert=True)

    async def test_callback_confirm_race_transitioned_false_cancelled(self):
        """Verify confirm seeing a concurrently cancelled draft reports cancelled, removes keyboard, and does not execute."""
        draft = await create_action_draft(
            user_id=81, chat_id=91, action_type="schedule_reminder",
            payload={"text": "Race test", "iso_time_utc": "2026-11-20T15:00:00+00:00"},
            missing_fields=[]
        )

        cancelled_mock_draft = MagicMock(status=DRAFT_STATUS_CANCELLED)
        mock_exec = AsyncMock()

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=81, chat_id=91)
        with patch("bot.handlers.callbacks.confirm_action_draft", AsyncMock(return_value=(cancelled_mock_draft, False))), \
             patch("bot.handlers.callbacks.execute_tool", mock_exec):

            await handle_callback(update, MagicMock())

            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("❌ Цю дію було скасовано.", show_alert=True)
            query.message.edit_text.assert_awaited_once_with("❌ Дію скасовано.", reply_markup=None)

    async def test_callback_confirm_race_transitioned_false_expired(self):
        """Verify confirm seeing a concurrently expired draft reports expired, removes keyboard, and does not execute."""
        draft = await create_action_draft(
            user_id=82, chat_id=92, action_type="schedule_reminder",
            payload={"text": "Race test 2", "iso_time_utc": "2026-11-20T15:00:00+00:00"},
            missing_fields=[]
        )

        expired_mock_draft = MagicMock(status=DRAFT_STATUS_EXPIRED)
        mock_exec = AsyncMock()

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=82, chat_id=92)
        with patch("bot.handlers.callbacks.confirm_action_draft", AsyncMock(return_value=(expired_mock_draft, False))), \
             patch("bot.handlers.callbacks.execute_tool", mock_exec):

            await handle_callback(update, MagicMock())

            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("⏳ Термін дії чернетки вичерпано.", show_alert=True)
            query.message.edit_text.assert_awaited_once_with("⏳ Термін дії чернетки вичерпано.", reply_markup=None)

    async def test_callback_confirm_race_transitioned_false_awaiting_info(self):
        """Verify confirm seeing awaiting_info when transitioned=False keeps cancel control available."""
        draft = await create_action_draft(
            user_id=83, chat_id=93, action_type="schedule_reminder",
            payload={},
            missing_fields=["text"]
        )

        awaiting_mock_draft = MagicMock(status=DRAFT_STATUS_AWAITING_INFO)
        mock_exec = AsyncMock()

        update, query = make_mock_update(f"draft:ok:{draft.id}", user_id=83, chat_id=93)
        # Bypass initial draft check by patching get_action_draft to pretend it was pending initially
        fake_initial = MagicMock(status=DRAFT_STATUS_PENDING_CONFIRMATION)
        with patch("bot.handlers.callbacks.get_action_draft", AsyncMock(return_value=fake_initial)), \
             patch("bot.handlers.callbacks.confirm_action_draft", AsyncMock(return_value=(awaiting_mock_draft, False))), \
             patch("bot.handlers.callbacks.execute_tool", mock_exec):

            await handle_callback(update, MagicMock())

            mock_exec.assert_not_called()
            query.answer.assert_awaited_with("⚠️ Недостатньо даних для виконання дії.", show_alert=True)
            # Keyboard must NOT be removed
            query.message.edit_reply_markup.assert_not_called()
            query.message.edit_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
