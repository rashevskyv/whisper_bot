import os
import sys
import unittest
import asyncio
import logging
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func
from bot.database.models import Base, ActionDraft, Reminder, MessageCache
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    get_active_action_draft,
    confirm_action_draft,
    cancel_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_EXPIRED,
)
from bot.ai.tools import (
    apply_action_draft_reply,
    format_draft_preview_or_question,
    ToolResult,
)
from bot.handlers.ai import build_draft_reply_markup
from bot.handlers.text import handle_text
from bot.handlers.media import handle_voice_video
from bot.handlers.callbacks import handle_callback, ERROR_TRANSCRIPTION_NOT_FOUND
from config import BOT_TIMEZONE


def make_mock_callback_update(callback_data: str, user_id: int = 123, chat_id: int = 456):
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 999
    query.message.text = "Existing transcription text"
    query.message.reply_text = AsyncMock()
    query.message.edit_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=user_id, first_name="TestUser")
    update.effective_chat = MagicMock(id=chat_id, type="private")
    return update, query


def make_mock_text_update(text: str, user_id: int = 123, chat_id: int = 456, chat_type: str = "private"):
    update = MagicMock()
    message = MagicMock()
    message.message_id = 888
    message.text = text
    message.reply_to_message = None
    message.reply_text = AsyncMock()
    message.delete = AsyncMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock(id=user_id, first_name="TestUser")
    update.effective_chat = MagicMock(id=chat_id, type=chat_type)
    return update, message


class TestSharedClarification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.drafts_patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.drafts_patcher.start()

    async def asyncTearDown(self):
        self.drafts_patcher.stop()
        await self.engine.dispose()

    # 1. Same-row update
    async def test_same_row_update(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        res = await apply_action_draft_reply(
            draft_id=draft.id, user_id=1, chat_id=2, reply_text="Doctor appointment"
        )
        self.assertTrue(res.payload["success"])
        self.assertEqual(res.draft_id, draft.id)
        self.assertEqual(res.payload["draft_id"], draft.id)

        # Confirm no replacement draft created
        async with self.SessionLocal() as session:
            rows = (await session.execute(select(ActionDraft))).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, draft.id)
            self.assertEqual(rows[0].payload.get("text"), "Doctor appointment")

    # 2. One field per reply
    async def test_one_field_per_reply(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        # First reply supplies text
        res = await apply_action_draft_reply(
            draft_id=draft.id, user_id=1, chat_id=2, reply_text="Dentist"
        )
        self.assertTrue(res.payload["success"])
        self.assertEqual(res.payload["missing_fields"], ["iso_time_utc"])
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(res.display_text, "❓ На коли встановити нагадування?")

    # 3. Complete schedule clarification
    async def test_complete_schedule_clarification(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Dentist"}, missing_fields=["iso_time_utc"]
        )
        future_dt_str = "2028-11-20 15:30:00"
        res = await apply_action_draft_reply(
            draft_id=draft.id, user_id=1, chat_id=2, reply_text=future_dt_str, timezone_name="UTC"
        )
        self.assertTrue(res.payload["success"])
        self.assertEqual(res.payload["missing_fields"], [])
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertIn("📋 <b>Підтвердження нагадування:</b>", res.display_text)
        self.assertIn("Dentist", res.display_text)
        self.assertIn("⚠️ <i>Потрібне підтвердження.</i>", res.display_text)

        # Stored datetime in payload is UTC ISO
        async with self.SessionLocal() as session:
            reloaded = await session.get(ActionDraft, draft.id)
            self.assertEqual(reloaded.status, DRAFT_STATUS_PENDING_CONFIRMATION)
            self.assertIn("2028-11-20T15:30:00", reloaded.payload["iso_time_utc"])

    # 4. Invalid schedule time leaves draft awaiting_info and unchanged
    async def test_invalid_schedule_time(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Dentist"}, missing_fields=["iso_time_utc"]
        )
        for bad_time in ["invalid date xyz", "2020-01-01 10:00:00", ""]:
            res = await apply_action_draft_reply(
                draft_id=draft.id, user_id=1, chat_id=2, reply_text=bad_time, timezone_name="UTC"
            )
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.draft_id, draft.id)

            async with self.SessionLocal() as session:
                reloaded = await session.get(ActionDraft, draft.id)
                self.assertEqual(reloaded.status, DRAFT_STATUS_AWAITING_INFO)
                self.assertEqual(reloaded.missing_fields, ["iso_time_utc"])
                self.assertNotIn("iso_time_utc", reloaded.payload)

    # 5. Delete reminder ID parsing
    async def test_delete_reminder_id_parsing(self):
        # 12 works
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="delete_reminder",
            payload={}, missing_fields=["reminder_id"]
        )
        res = await apply_action_draft_reply(
            draft_id=draft.id, user_id=1, chat_id=2, reply_text="12"
        )
        self.assertTrue(res.payload["success"])
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertIn("Нагадування #12", res.display_text)

        # #12 works
        draft2 = await create_action_draft(
            user_id=1, chat_id=2, action_type="delete_reminder",
            payload={}, missing_fields=["reminder_id"]
        )
        res2 = await apply_action_draft_reply(
            draft_id=draft2.id, user_id=1, chat_id=2, reply_text="  #12  "
        )
        self.assertTrue(res2.payload["success"])
        self.assertEqual(res2.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)

        # Zero, negative, prose rejected
        draft3 = await create_action_draft(
            user_id=1, chat_id=2, action_type="delete_reminder",
            payload={}, missing_fields=["reminder_id"]
        )
        for bad_input in ["0", "-12", "#0", "видали зустріч 12 завтра", "abc"]:
            bad_res = await apply_action_draft_reply(
                draft_id=draft3.id, user_id=1, chat_id=2, reply_text=bad_input
            )
            self.assertFalse(bad_res.payload["success"])
            async with self.SessionLocal() as session:
                reloaded = await session.get(ActionDraft, draft3.id)
                self.assertEqual(reloaded.status, DRAFT_STATUS_AWAITING_INFO)
                self.assertEqual(reloaded.missing_fields, ["reminder_id"])
                self.assertNotIn("reminder_id", reloaded.payload)

    # 6. State and ownership rejections
    async def test_state_and_ownership_rejections(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="delete_reminder",
            payload={}, missing_fields=["reminder_id"]
        )
        # Foreign user
        res = await apply_action_draft_reply(draft.id, user_id=999, chat_id=2, reply_text="12")
        self.assertFalse(res.payload["success"])
        self.assertEqual(res.payload["error"], "draft_not_found")

        # Wrong chat
        res = await apply_action_draft_reply(draft.id, user_id=1, chat_id=999, reply_text="12")
        self.assertFalse(res.payload["success"])

        # Cancelled
        await cancel_action_draft(draft.id, user_id=1, chat_id=2)
        res = await apply_action_draft_reply(draft.id, user_id=1, chat_id=2, reply_text="12")
        self.assertFalse(res.payload["success"])
        self.assertEqual(res.payload["error"], "invalid_status")

        # Confirmed
        draft2 = await create_action_draft(
            user_id=1, chat_id=2, action_type="delete_reminder",
            payload={"reminder_id": 5}, missing_fields=[]
        )
        await confirm_action_draft(draft2.id, user_id=1, chat_id=2)
        res2 = await apply_action_draft_reply(draft2.id, user_id=1, chat_id=2, reply_text="12")
        self.assertFalse(res2.payload["success"])
        self.assertEqual(res2.payload["error"], "invalid_status")

    # 7. Side effects prevented
    async def test_side_effects_prevented(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Test"}, missing_fields=["iso_time_utc"]
        )
        with patch("bot.utils.scheduler.scheduler_service.add_reminder", new_callable=AsyncMock) as mock_add, \
             patch("bot.ai.tools.execute_tool", new_callable=AsyncMock) as mock_exec:
            res = await apply_action_draft_reply(
                draft_id=draft.id, user_id=1, chat_id=2,
                reply_text="2028-11-20 15:30:00", timezone_name="UTC"
            )
            self.assertTrue(res.payload["success"])
            mock_add.assert_not_called()
            mock_exec.assert_not_called()

    # 8. HTML escaping of user-controlled text
    async def test_html_escaping(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        xss_text = "<script>alert('xss')</script> & <b>bold</b>"
        res1 = await apply_action_draft_reply(draft.id, 1, 2, reply_text=xss_text)
        self.assertTrue(res1.payload["success"])

        res2 = await apply_action_draft_reply(draft.id, 1, 2, reply_text="2028-11-20 15:30:00", timezone_name="UTC")
        self.assertTrue(res2.payload["success"])
        import html as py_html
        self.assertNotIn("<script>", res2.display_text)
        self.assertIn(py_html.escape(xss_text), res2.display_text)


class TestTextHandlerRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.drafts_patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.drafts_patcher.start()

    async def asyncTearDown(self):
        self.drafts_patcher.stop()
        await self.engine.dispose()

    # 1. Intercepts before menu, DL, vision, AI
    async def test_intercepts_before_menu_dl_vision_ai(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        update, message = make_mock_text_update("Meeting with team", user_id=10, chat_id=20)
        context = MagicMock()

        with patch("bot.handlers.text.process_gpt_request", new_callable=AsyncMock) as mock_gpt, \
             patch("bot.utils.scheduler.scheduler_service.get_active_reminders", new_callable=AsyncMock) as mock_rems, \
             patch("bot.handlers.text.download_media_direct", new_callable=AsyncMock) as mock_dl:
            await handle_text(update, context)

            mock_gpt.assert_not_called()
            mock_rems.assert_not_called()
            mock_dl.assert_not_called()
            message.reply_text.assert_called_once()
            call_args = message.reply_text.call_args[0]
            self.assertIn("❓ На коли встановити нагадування?", call_args[0])

    # 2. Incomplete draft receives Cancel-only markup
    async def test_incomplete_markup_cancel_only(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        update, message = make_mock_text_update("Meeting", user_id=10, chat_id=20)
        await handle_text(update, MagicMock())

        markup = message.reply_text.call_args[1].get("reply_markup")
        self.assertIsNotNone(markup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(len(markup.inline_keyboard[0]), 1)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"draft:no:{draft.id}")

    # 3. Completed draft receives Confirm and Cancel markup
    async def test_completed_markup_confirm_and_cancel(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={"text": "Meeting"}, missing_fields=["iso_time_utc"]
        )
        update, message = make_mock_text_update("2028-12-01 10:00:00", user_id=10, chat_id=20)
        with patch("bot.handlers.text.get_user_model_settings", new_callable=AsyncMock) as mock_settings:
            mock_settings.return_value = {"timezone": "UTC"}
            await handle_text(update, MagicMock())

        markup = message.reply_text.call_args[1].get("reply_markup")
        self.assertIsNotNone(markup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(len(markup.inline_keyboard[0]), 2)
        btn_ok, btn_no = markup.inline_keyboard[0]
        self.assertEqual(btn_ok.callback_data, f"draft:ok:{draft.id}")
        self.assertEqual(btn_no.callback_data, f"draft:no:{draft.id}")

    # 4. Another user or chat cannot consume draft
    async def test_another_user_or_chat_cannot_consume_draft(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        # Foreign user in same group chat
        update, message = make_mock_text_update("Not the owner", user_id=999, chat_id=20, chat_type="group")
        with patch("bot.handlers.text.should_respond", return_value=False):
            await handle_text(update, MagicMock())
            # Not intercepted by draft
            message.reply_text.assert_not_called()

        async with self.SessionLocal() as session:
            reloaded = await session.get(ActionDraft, draft.id)
            self.assertEqual(reloaded.status, DRAFT_STATUS_AWAITING_INFO)
            self.assertEqual(reloaded.missing_fields, ["text", "iso_time_utc"])

    # 5. Race state/expiry does not fall through into general AI
    async def test_race_state_expiry_no_fallthrough(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        update, message = make_mock_text_update("Some text", user_id=10, chat_id=20)
        with patch("bot.handlers.text.apply_action_draft_reply", new_callable=AsyncMock) as mock_apply, \
             patch("bot.handlers.text.process_gpt_request", new_callable=AsyncMock) as mock_gpt:
            mock_apply.return_value = ToolResult(
                payload={"success": False, "error": "state_conflict"},
                display_text="⏳ Термін дії чернетки вичерпано або стан чернетки змінився.",
                stop=True,
                draft_id=draft.id,
            )
            await handle_text(update, MagicMock())
            mock_gpt.assert_not_called()
            message.reply_text.assert_called_once()
            self.assertIn("вичерпано або стан", message.reply_text.call_args[0][0])

    # 6. Full clarification text is absent from captured logs
    async def test_clarification_text_absent_from_logs(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        secret_reply = "TOP_SECRET_CLARIFICATION_TOKEN_xyz123"
        update, message = make_mock_text_update(secret_reply, user_id=10, chat_id=20)

        with self.assertLogs("bot", level=logging.INFO) as captured:
            await handle_text(update, MagicMock())
            for record in captured.records:
                self.assertNotIn(secret_reply, record.getMessage())

    # 7. Apply failure does not fall through into general AI or leak secret
    async def test_text_apply_failure_does_not_fall_through(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={"initial": "val"}, missing_fields=["text", "iso_time_utc"]
        )
        secret_marker = "TEXT_REPLY_SECRET_789"
        secret_reply = "USER_SECRET_CLARIFICATION_REPLY_999"
        update, message = make_mock_text_update(secret_reply, user_id=10, chat_id=20)

        with patch("bot.handlers.text.apply_action_draft_reply", side_effect=RuntimeError(secret_marker)) as mock_apply, \
             patch("bot.handlers.text.process_gpt_request", new_callable=AsyncMock) as mock_gpt, \
             patch("bot.utils.scheduler.scheduler_service.add_reminder", new_callable=AsyncMock) as mock_sched, \
             self.assertLogs("bot", level=logging.INFO) as captured:
            await handle_text(update, MagicMock())

            mock_gpt.assert_not_called()
            mock_sched.assert_not_called()
            message.reply_text.assert_called_once()
            reply_text = message.reply_text.call_args[0][0]
            self.assertEqual(reply_text, "⚠️ Не вдалося обробити уточнення. Спробуйте ще раз або скасуйте дію.")
            self.assertNotIn(secret_marker, reply_text)
            self.assertNotIn(secret_reply, reply_text)

            markup = message.reply_text.call_args[1]["reply_markup"]
            self.assertIsNotNone(markup)
            self.assertEqual(len(markup.inline_keyboard), 1)
            self.assertEqual(markup.inline_keyboard[0][0].text, "❌ Скасувати")

            # Check draft state unchanged
            async with self.SessionLocal() as session:
                reloaded = await session.get(ActionDraft, draft.id)
                self.assertEqual(reloaded.status, DRAFT_STATUS_AWAITING_INFO)
                self.assertEqual(reloaded.payload, {"initial": "val"})
                self.assertEqual(reloaded.missing_fields, ["text", "iso_time_utc"])

            # Check logs do not contain secrets
            for record in captured.records:
                self.assertNotIn(secret_marker, record.getMessage())
                self.assertNotIn(secret_reply, record.getMessage())


class TestTranscriptionRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.drafts_patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.context_patcher = patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal)
        self.drafts_patcher.start()
        self.context_patcher.start()

    async def asyncTearDown(self):
        self.drafts_patcher.stop()
        self.context_patcher.stop()
        await self.engine.dispose()

    # 1. Media output includes clarification button only when awaiting_info draft exists
    async def test_media_output_includes_clarification_button_when_awaiting_info(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )

        update = MagicMock()
        update.message = MagicMock()
        update.message.voice = MagicMock(file_id="voice123", duration=5)
        update.message.video_note = None
        update.message.video = None
        update.message.reply_text = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit_text=AsyncMock()))
        update.effective_user = MagicMock(id=1, first_name="User1")
        update.effective_chat = MagicMock(id=2, type="private")

        context = MagicMock()
        context.bot.get_file = AsyncMock()

        with patch("bot.handlers.media.check_transcription_limit", new_callable=AsyncMock, return_value=(True, "")), \
             patch("bot.handlers.media.record_transcription_usage", new_callable=AsyncMock), \
             patch("bot.handlers.media.download_file", new_callable=AsyncMock, return_value="/tmp/test.ogg"), \
             patch("bot.handlers.media.validate_audio_size"), \
             patch("bot.handlers.media.cleanup_files"), \
             patch("bot.handlers.media.get_ai_provider", new_callable=AsyncMock) as mock_get_provider, \
             patch("bot.handlers.media.beautify_text", new_callable=AsyncMock, return_value=("Remind me to call Mom", "beautify-model")), \
             patch("bot.handlers.media.send_long_message", new_callable=AsyncMock) as mock_send_long:

            mock_prov = MagicMock()
            mock_prov.transcribe = AsyncMock(return_value="Remind me to call Mom")
            mock_get_provider.return_value = mock_prov

            await handle_voice_video(update, context)

            mock_send_long.assert_called_once()
            reply_markup = mock_send_long.call_args[1]["reply_markup"]
            self.assertIsNotNone(reply_markup)

            # Find clarification button
            clarify_btns = [
                btn for row in reply_markup.inline_keyboard for btn in row
                if "Використати як уточнення" in btn.text
            ]
            self.assertEqual(len(clarify_btns), 1)
            btn = clarify_btns[0]
            self.assertTrue(btn.callback_data.startswith(f"draft:fill:{draft.id}:"))
            self.assertLess(len(btn.callback_data.encode("utf-8")), 64)

    # 2. No clarification button when draft is pending_confirmation, terminal, or absent
    async def test_no_clarification_button_when_not_awaiting_info(self):
        # Pending confirmation draft
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Test", "iso_time_utc": "2028-01-01T10:00:00+00:00"},
            missing_fields=[]
        )
        self.assertEqual(draft.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        update = MagicMock()
        update.message = MagicMock()
        update.message.voice = MagicMock(file_id="voice123", duration=5)
        update.message.video_note = None
        update.message.video = None
        update.message.reply_text = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit_text=AsyncMock()))
        update.effective_user = MagicMock(id=1, first_name="User1")
        update.effective_chat = MagicMock(id=2, type="private")

        context = MagicMock()
        context.bot.get_file = AsyncMock()

        with patch("bot.handlers.media.check_transcription_limit", new_callable=AsyncMock, return_value=(True, "")), \
             patch("bot.handlers.media.record_transcription_usage", new_callable=AsyncMock), \
             patch("bot.handlers.media.download_file", new_callable=AsyncMock, return_value="/tmp/test.ogg"), \
             patch("bot.handlers.media.validate_audio_size"), \
             patch("bot.handlers.media.cleanup_files"), \
             patch("bot.handlers.media.get_ai_provider", new_callable=AsyncMock) as mock_get_provider, \
             patch("bot.handlers.media.beautify_text", new_callable=AsyncMock, return_value=("Some text", "m")), \
             patch("bot.handlers.media.send_long_message", new_callable=AsyncMock) as mock_send_long:

            mock_prov = MagicMock()
            mock_prov.transcribe = AsyncMock(return_value="Some text")
            mock_get_provider.return_value = mock_prov

            await handle_voice_video(update, context)

            reply_markup = mock_send_long.call_args[1]["reply_markup"]
            clarify_btns = [
                btn for row in reply_markup.inline_keyboard for btn in row
                if "Використати як уточнення" in btn.text
            ]
            self.assertEqual(len(clarify_btns), 0)

    # 3. Two sequential transcriptions bind exact IDs
    async def test_two_sequential_transcriptions_bind_exact_ids(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg1 = MessageCache(user_id=1, chat_id=2, role="transcription", content="Clean the garage")
            msg2 = MessageCache(user_id=1, chat_id=2, role="transcription", content="Walk the dog")
            session.add_all([msg1, msg2])
            await session.commit()
            await session.refresh(msg1)
            await session.refresh(msg2)

        # Call callback for msg1
        cb_data_1 = f"draft:fill:{draft.id}:{msg1.id}"
        update1, query1 = make_mock_callback_update(cb_data_1, user_id=1, chat_id=2)
        await handle_callback(update1, MagicMock())

        async with self.SessionLocal() as session:
            reloaded = await session.get(ActionDraft, draft.id)
            self.assertEqual(reloaded.payload["text"], "Clean the garage")

    # 4. Malformed, foreign, or terminal callbacks leave original untouched
    async def test_callback_rejections_leave_message_untouched(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={}, missing_fields=["text", "iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg = MessageCache(user_id=1, chat_id=2, role="transcription", content="Sample")
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

        # Malformed callback
        bad_cb = f"draft:fill:{draft.id}:notanumber"
        up_bad, q_bad = make_mock_callback_update(bad_cb, user_id=1, chat_id=2)
        await handle_callback(up_bad, MagicMock())
        q_bad.message.edit_reply_markup.assert_not_called()
        q_bad.answer.assert_called_with("❌ Некоректні дані запиту.", show_alert=True)

        # Foreign user
        cb_valid = f"draft:fill:{draft.id}:{msg.id}"
        up_foreign, q_foreign = make_mock_callback_update(cb_valid, user_id=999, chat_id=2)
        await handle_callback(up_foreign, MagicMock())
        q_foreign.message.edit_reply_markup.assert_not_called()
        q_foreign.answer.assert_called_with("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)

    # 5. Valid fill callback flow
    async def test_valid_fill_callback_flow(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Water plants"}, missing_fields=["iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg = MessageCache(user_id=1, chat_id=2, role="transcription", content="2028-06-15 11:00:00")
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

        cb_data = f"draft:fill:{draft.id}:{msg.id}"
        update, query = make_mock_callback_update(cb_data, user_id=1, chat_id=2)
        with patch("bot.handlers.callbacks.get_user_model_settings", new_callable=AsyncMock) as mock_s:
            mock_s.return_value = {"timezone": "UTC"}
            await handle_callback(update, MagicMock())

        # Old keyboard removed
        query.message.edit_reply_markup.assert_called_once_with(None)
        # Separate reply sent with preview and confirm/cancel buttons
        query.message.reply_text.assert_called_once()
        text_arg = query.message.reply_text.call_args[0][0]
        self.assertIn("📋 <b>Підтвердження нагадування:</b>", text_arg)
        markup_arg = query.message.reply_text.call_args[1]["reply_markup"]
        self.assertEqual(len(markup_arg.inline_keyboard[0]), 2)

    # 6. Invalid transcription value leaves draft awaiting_info and unchanged
    async def test_invalid_transcription_value_leaves_draft_awaiting_info(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Water plants"}, missing_fields=["iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg = MessageCache(user_id=1, chat_id=2, role="transcription", content="not a valid date")
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

        cb_data = f"draft:fill:{draft.id}:{msg.id}"
        update, query = make_mock_callback_update(cb_data, user_id=1, chat_id=2)
        await handle_callback(update, MagicMock())

        # Old keyboard consumed
        query.message.edit_reply_markup.assert_called_once_with(None)
        # Sent correction question
        query.message.reply_text.assert_called_once()
        text_arg = query.message.reply_text.call_args[0][0]
        self.assertIn("коректні дату та час", text_arg)
        markup_arg = query.message.reply_text.call_args[1]["reply_markup"]
        self.assertEqual(len(markup_arg.inline_keyboard[0]), 1)
        self.assertEqual(markup_arg.inline_keyboard[0][0].text, "❌ Скасувати")

    # 7. Media active draft lookup failure keeps transcription and standard buttons without leaking
    async def test_media_active_draft_lookup_failure(self):
        secret_marker = "MEDIA_LOOKUP_SECRET_111"
        update = MagicMock()
        update.message = MagicMock()
        update.message.voice = MagicMock(file_id="voice123", duration=5)
        update.message.video_note = None
        update.message.video = None
        update.message.reply_text = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit_text=AsyncMock()))
        update.effective_user = MagicMock(id=1, first_name="User1")
        update.effective_chat = MagicMock(id=2, type="private")

        context = MagicMock()
        context.bot.get_file = AsyncMock()

        with patch("bot.handlers.media.check_transcription_limit", new_callable=AsyncMock, return_value=(True, "")), \
             patch("bot.handlers.media.record_transcription_usage", new_callable=AsyncMock), \
             patch("bot.handlers.media.download_file", new_callable=AsyncMock, return_value="/tmp/test.ogg"), \
             patch("bot.handlers.media.validate_audio_size"), \
             patch("bot.handlers.media.cleanup_files"), \
             patch("bot.handlers.media.get_ai_provider", new_callable=AsyncMock) as mock_get_provider, \
             patch("bot.handlers.media.beautify_text", new_callable=AsyncMock, return_value=("Some transcription text", "beautify-model")), \
             patch("bot.handlers.media.get_active_action_draft", side_effect=RuntimeError(secret_marker)), \
             patch("bot.handlers.media.send_long_message", new_callable=AsyncMock) as mock_send_long, \
             self.assertLogs("bot", level=logging.INFO) as captured:

            mock_prov = MagicMock()
            mock_prov.transcribe = AsyncMock(return_value="Some transcription text")
            mock_get_provider.return_value = mock_prov

            await handle_voice_video(update, context)

            mock_send_long.assert_called_once()
            call_text = mock_send_long.call_args[0][1]
            self.assertIn("Some transcription text", call_text)
            self.assertNotIn(secret_marker, call_text)

            reply_markup = mock_send_long.call_args[1]["reply_markup"]
            self.assertIsNotNone(reply_markup)

            # Clarification button must NOT be present
            clarify_btns = [
                btn for row in reply_markup.inline_keyboard for btn in row
                if "Використати як уточнення" in btn.text
            ]
            self.assertEqual(len(clarify_btns), 0)

            # Standard buttons must still be present
            button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
            self.assertIn("▶️ Обробити як інструкцію", button_texts)
            self.assertIn("📝 Підсумувати", button_texts)
            self.assertIn("✍️ Переформулювати", button_texts)

            # Check secret marker not in captured logs
            for record in captured.records:
                self.assertNotIn(secret_marker, record.getMessage())

    # 8. Callback: settings failure uses fallback
    async def test_callback_settings_failure_uses_fallback(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Meeting"}, missing_fields=["iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg = MessageCache(user_id=1, chat_id=2, role="transcription", content="2028-11-20 15:30:00")
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

        secret_marker = "SETTINGS_SECRET_PASSWORD_123"
        cb_data = f"draft:fill:{draft.id}:{msg.id}"
        update, query = make_mock_callback_update(cb_data, user_id=1, chat_id=2)

        with patch("bot.handlers.callbacks.get_user_model_settings", side_effect=RuntimeError(secret_marker)), \
             patch("bot.handlers.callbacks.apply_action_draft_reply", wraps=apply_action_draft_reply) as spy_apply, \
             self.assertLogs("bot", level=logging.INFO) as captured:

            await handle_callback(update, MagicMock())

            # Callback does not throw
            query.answer.assert_called()
            query.message.edit_reply_markup.assert_called_once_with(None)

            # apply_action_draft_reply was called with BOT_TIMEZONE fallback
            spy_apply.assert_called_once()
            _, kwargs = spy_apply.call_args
            self.assertEqual(kwargs.get("timezone_name"), BOT_TIMEZONE)

            # User received preview
            query.message.reply_text.assert_called_once()
            sent_text = query.message.reply_text.call_args[0][0]
            self.assertIn("Підтвердження нагадування", sent_text)
            self.assertNotIn(secret_marker, sent_text)

            # Check logs do not leak marker
            for record in captured.records:
                self.assertNotIn(secret_marker, record.getMessage())

    # 9. An invalid saved timezone must use the configured fallback, not UTC.
    async def test_callback_invalid_timezone_uses_fallback(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Meeting"}, missing_fields=["iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg = MessageCache(user_id=1, chat_id=2, role="transcription", content="2028-11-20 15:30:00")
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

        update, query = make_mock_callback_update(f"draft:fill:{draft.id}:{msg.id}", user_id=1, chat_id=2)
        with patch("bot.handlers.callbacks.get_user_model_settings", new_callable=AsyncMock) as mock_settings, \
             patch("bot.handlers.callbacks.apply_action_draft_reply", wraps=apply_action_draft_reply) as spy_apply:
            mock_settings.return_value = {"timezone": "Not/A_Real_Timezone"}
            await handle_callback(update, MagicMock())

        self.assertEqual(spy_apply.call_args.kwargs["timezone_name"], BOT_TIMEZONE)

    # 10. Text clarification applies the same invalid-timezone fallback.
    async def test_text_invalid_timezone_uses_fallback(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={"text": "Meeting"}, missing_fields=["iso_time_utc"]
        )
        update, _ = make_mock_text_update("2028-11-20 15:30:00", user_id=10, chat_id=20)
        with patch("bot.handlers.text.get_user_model_settings", new_callable=AsyncMock) as mock_settings, \
             patch("bot.handlers.text.apply_action_draft_reply", wraps=apply_action_draft_reply) as spy_apply:
            mock_settings.return_value = {"timezone": "Not/A_Real_Timezone"}
            await handle_text(update, MagicMock())

        self.assertEqual(spy_apply.call_args.kwargs["timezone_name"], BOT_TIMEZONE)

    # 9. Callback: apply failure is contained
    async def test_callback_apply_failure_is_contained(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="schedule_reminder",
            payload={"text": "Doctor visit"}, missing_fields=["iso_time_utc"]
        )
        async with self.SessionLocal() as session:
            msg = MessageCache(user_id=1, chat_id=2, role="transcription", content="tomorrow at 10am")
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

        secret_marker = "APPLY_SECRET_TOKEN_456"
        cb_data = f"draft:fill:{draft.id}:{msg.id}"
        update, query = make_mock_callback_update(cb_data, user_id=1, chat_id=2)

        with patch("bot.handlers.callbacks.apply_action_draft_reply", side_effect=RuntimeError(secret_marker)), \
             patch("bot.ai.tools.execute_tool", new_callable=AsyncMock) as mock_exec, \
             patch("bot.utils.scheduler.scheduler_service.add_reminder", new_callable=AsyncMock) as mock_sched, \
             self.assertLogs("bot", level=logging.INFO) as captured:

            await handle_callback(update, MagicMock())

            # Query was answered and keyboard removed
            query.answer.assert_called()
            query.message.edit_reply_markup.assert_called_once_with(None)

            # Stable error message sent
            query.message.reply_text.assert_called_once()
            sent_text = query.message.reply_text.call_args[0][0]
            self.assertEqual(sent_text, "⚠️ Не вдалося обробити уточнення. Спробуйте ще раз або скасуйте дію.")
            self.assertNotIn(secret_marker, sent_text)

            # Cancel-only keyboard attached
            markup = query.message.reply_text.call_args[1]["reply_markup"]
            self.assertIsNotNone(markup)
            self.assertEqual(len(markup.inline_keyboard), 1)
            self.assertEqual(markup.inline_keyboard[0][0].text, "❌ Скасувати")
            self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"draft:no:{draft.id}")

            # Draft remains awaiting_info with unchanged payload and missing_fields
            async with self.SessionLocal() as session:
                reloaded = await session.get(ActionDraft, draft.id)
                self.assertEqual(reloaded.status, DRAFT_STATUS_AWAITING_INFO)
                self.assertEqual(reloaded.payload, {"text": "Doctor visit"})
                self.assertEqual(reloaded.missing_fields, ["iso_time_utc"])

            # No side effects called
            mock_exec.assert_not_called()
            mock_sched.assert_not_called()

            # Logs do not leak secret marker
            for record in captured.records:
                self.assertNotIn(secret_marker, record.getMessage())


class TestEndToEndRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.drafts_patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.context_patcher = patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal)
        self.sched_patcher = patch("bot.utils.scheduler.AsyncSessionLocal", self.SessionLocal)
        self.drafts_patcher.start()
        self.context_patcher.start()
        self.sched_patcher.start()

    async def asyncTearDown(self):
        self.drafts_patcher.stop()
        self.context_patcher.stop()
        self.sched_patcher.stop()
        await self.engine.dispose()

    async def test_full_clarification_and_confirm_lifecycle(self):
        # 1. Draft created incomplete (missing iso_time_utc)
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="schedule_reminder",
            payload={"text": "Submit quarterly report"}, missing_fields=["iso_time_utc"]
        )
        self.assertEqual(draft.status, DRAFT_STATUS_AWAITING_INFO)

        # 2. User clarifies via text message
        update, message = make_mock_text_update("2028-12-31 18:00:00", user_id=10, chat_id=20)
        with patch("bot.handlers.text.get_user_model_settings", new_callable=AsyncMock) as mock_s:
            mock_s.return_value = {"timezone": "UTC"}
            await handle_text(update, MagicMock())

        # Verify draft became pending_confirmation
        async with self.SessionLocal() as session:
            updated_draft = await session.get(ActionDraft, draft.id)
            self.assertEqual(updated_draft.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        # 3. User clicks Confirm button (draft:ok:<same_id>)
        cb_ok = f"draft:ok:{draft.id}"
        update_cb, query_cb = make_mock_callback_update(cb_ok, user_id=10, chat_id=20)
        with patch("bot.utils.scheduler.scheduler_service.add_reminder", new_callable=AsyncMock, return_value=101) as mock_add:
            await handle_callback(update_cb, MagicMock())
            mock_add.assert_called_once()
            call_text = mock_add.call_args[0][2]
            self.assertEqual(call_text, "Submit quarterly report")

        # Verify draft became confirmed
        async with self.SessionLocal() as session:
            final_draft = await session.get(ActionDraft, draft.id)
            self.assertEqual(final_draft.status, DRAFT_STATUS_CONFIRMED)

        # 4. Repeated confirm callback does not execute again
        update_cb2, query_cb2 = make_mock_callback_update(cb_ok, user_id=10, chat_id=20)
        with patch("bot.utils.scheduler.scheduler_service.add_reminder", new_callable=AsyncMock) as mock_add2:
            await handle_callback(update_cb2, MagicMock())
            mock_add2.assert_not_called()
            query_cb2.answer.assert_called_with("⚠️ Цю дію вже підтверджено.", show_alert=True)


if __name__ == "__main__":
    unittest.main()
