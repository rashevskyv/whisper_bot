import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.database.models import Base, MessageCache
from bot.utils.context import ContextManager
from bot.handlers.callbacks import handle_callback, ERROR_TRANSCRIPTION_NOT_FOUND
from bot.handlers.media import handle_voice_video
from bot.utils.helpers import beautify_text


def make_mock_update(callback_data: str, user_id: int = 123, chat_id: int = 456):
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=chat_id)
    return update, query


class TestVoiceActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create in-memory SQLite engine for isolated DB tests
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.context_mgr = ContextManager()

    async def asyncTearDown(self):
        await self.engine.dispose()

    # 1. save_message returns the created row ID
    async def test_save_message_returns_created_row_id(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal):
            msg_id = await self.context_mgr.save_message(
                user_id=1, chat_id=10, role="transcription", content="voice note content"
            )
            self.assertIsInstance(msg_id, int)
            self.assertGreater(msg_id, 0)

            # Verify row actually persisted in DB with matching id
            async with self.SessionLocal() as session:
                row = await session.get(MessageCache, msg_id)
                self.assertIsNotNone(row)
                self.assertEqual(row.content, "voice note content")
                self.assertEqual(row.role, "transcription")
                self.assertEqual(row.user_id, 1)
                self.assertEqual(row.chat_id, 10)

    async def test_save_message_returns_none_on_db_error(self):
        with patch("bot.utils.context.AsyncSessionLocal", side_effect=Exception("DB connection error")):
            res = await self.context_mgr.save_message(
                user_id=1, chat_id=10, role="transcription", content="fail note"
            )
            self.assertIsNone(res)

    # 2. Exact lookup returns the correct transcription
    async def test_exact_lookup_returns_correct_transcription(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal):
            msg_id = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="transcription", content="correct speech"
            )
            retrieved = await self.context_mgr.get_transcription_by_id(msg_id, user_id=10, chat_id=20)
            self.assertEqual(retrieved, "correct speech")

    # 3. Exact lookup rejects the same ID when user_id is wrong
    async def test_exact_lookup_rejects_wrong_user(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal):
            msg_id = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="transcription", content="user private note"
            )
            retrieved = await self.context_mgr.get_transcription_by_id(msg_id, user_id=999, chat_id=20)
            self.assertIsNone(retrieved)

    # 4. Exact lookup rejects the same ID when chat_id is wrong
    async def test_exact_lookup_rejects_wrong_chat(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal):
            msg_id = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="transcription", content="chat private note"
            )
            retrieved = await self.context_mgr.get_transcription_by_id(msg_id, user_id=10, chat_id=999)
            self.assertIsNone(retrieved)

    # 5. Exact lookup rejects a row whose role is not transcription
    async def test_exact_lookup_rejects_non_transcription_role(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal):
            user_msg_id = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="user", content="regular user message"
            )
            asst_msg_id = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="assistant", content="bot response"
            )
            self.assertIsNone(await self.context_mgr.get_transcription_by_id(user_msg_id, 10, 20))
            self.assertIsNone(await self.context_mgr.get_transcription_by_id(asst_msg_id, 10, 20))

    # 6. Two stored transcriptions can be retrieved independently by ID
    async def test_independent_transcription_retrieval_by_id(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal):
            id1 = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="transcription", content="first voice instruction"
            )
            id2 = await self.context_mgr.save_message(
                user_id=10, chat_id=20, role="transcription", content="second voice instruction"
            )
            self.assertNotEqual(id1, id2)

            # Retrieve id1 - must be first voice instruction, NOT second
            res1 = await self.context_mgr.get_transcription_by_id(id1, 10, 20)
            self.assertEqual(res1, "first voice instruction")

            # Retrieve id2 - must be second voice instruction
            res2 = await self.context_mgr.get_transcription_by_id(id2, 10, 20)
            self.assertEqual(res2, "second voice instruction")

    # 7. A valid run_gpt:<id> callback uses the exact stored text
    async def test_run_gpt_callback_uses_exact_stored_text(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal), \
             patch("bot.handlers.callbacks.context_manager", self.context_mgr):
            user_id = 42
            chat_id = 100
            id1 = await self.context_mgr.save_message(
                user_id=user_id, chat_id=chat_id, role="transcription", content="Task A: do laundry"
            )
            id2 = await self.context_mgr.save_message(
                user_id=user_id, chat_id=chat_id, role="transcription", content="Task B: buy milk"
            )

            update, query = make_mock_update(f"run_gpt:{id1}", user_id=user_id, chat_id=chat_id)
            context = MagicMock()

            with patch("bot.handlers.callbacks.process_gpt_request", new_callable=AsyncMock) as mock_process_gpt:
                await handle_callback(update, context)

                # Process GPT request called
                mock_process_gpt.assert_awaited_once_with(update, context, user_id, manual_text=None)
                # Keyboard removed
                query.message.edit_reply_markup.assert_awaited_once_with(None)

                # Verified Task A (not Task B) was saved as user message
                async with self.SessionLocal() as session:
                    stmt = select(MessageCache).where(
                        and_(MessageCache.user_id == user_id, MessageCache.chat_id == chat_id, MessageCache.role == "user")
                    )
                    res = await session.execute(stmt)
                    user_msgs = res.scalars().all()
                    self.assertEqual(len(user_msgs), 1)
                    self.assertEqual(user_msgs[0].content, "Task A: do laundry")

    # 8. Malformed or unknown callback IDs do not call process_gpt_request
    async def test_malformed_or_unknown_callback_ids_rejected(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal), \
             patch("bot.handlers.callbacks.context_manager", self.context_mgr):
            user_id = 42
            chat_id = 100
            valid_id = await self.context_mgr.save_message(
                user_id=user_id, chat_id=chat_id, role="transcription", content="secret voice"
            )

            test_cases = [
                ("run_gpt", user_id, chat_id),
                ("run_gpt:", user_id, chat_id),
                ("run_gpt:abc", user_id, chat_id),
                ("run_gpt:-5", user_id, chat_id),
                ("run_gpt:99999", user_id, chat_id),
                (f"run_gpt:{valid_id}", 999, chat_id),
                (f"run_gpt:{valid_id}", user_id, 999),
            ]

            for cb_data, uid, cid in test_cases:
                with self.subTest(cb_data=cb_data, uid=uid, cid=cid):
                    update, query = make_mock_update(cb_data, user_id=uid, chat_id=cid)
                    context = MagicMock()

                    with patch("bot.handlers.callbacks.process_gpt_request", new_callable=AsyncMock) as mock_gpt:
                        await handle_callback(update, context)

                        mock_gpt.assert_not_called()
                        query.message.edit_reply_markup.assert_not_called()
                        error_found = (
                            (query.answer.call_args and ERROR_TRANSCRIPTION_NOT_FOUND in str(query.answer.call_args)) or
                            (query.message.reply_text.call_args and ERROR_TRANSCRIPTION_NOT_FOUND in str(query.message.reply_text.call_args))
                        )
                        self.assertTrue(error_found, f"Expected error for {cb_data}")

    # 9. summarize:<id> and reword:<id> use the selected transcription rather than the latest one
    async def test_summarize_and_reword_use_selected_transcription(self):
        with patch("bot.utils.context.AsyncSessionLocal", self.SessionLocal), \
             patch("bot.handlers.callbacks.context_manager", self.context_mgr):
            user_id = 50
            chat_id = 60
            id1 = await self.context_mgr.save_message(
                user_id=user_id, chat_id=chat_id, role="transcription", content="First Audio Content"
            )
            id2 = await self.context_mgr.save_message(
                user_id=user_id, chat_id=chat_id, role="transcription", content="Second Audio Content"
            )

            # Summarize on first audio
            upd_sum, q_sum = make_mock_update(f"summarize:{id1}", user_id=user_id, chat_id=chat_id)
            context_sum = MagicMock()
            with patch("bot.handlers.callbacks.summarize_text", new_callable=AsyncMock) as mock_sum:
                await handle_callback(upd_sum, context_sum)
                mock_sum.assert_awaited_once_with(upd_sum, context_sum, "First Audio Content")

            # Reword on first audio
            upd_rew, q_rew = make_mock_update(f"reword:{id1}", user_id=user_id, chat_id=chat_id)
            context_rew = MagicMock()
            with patch("bot.handlers.callbacks.reword_text", new_callable=AsyncMock) as mock_rew:
                await handle_callback(upd_rew, context_rew)
                mock_rew.assert_awaited_once_with(upd_rew, context_rew, "First Audio Content")

            # Malformed summarize / reword IDs do not execute handlers
            upd_bad_sum, _ = make_mock_update("summarize:bad", user_id=user_id, chat_id=chat_id)
            with patch("bot.handlers.callbacks.summarize_text", new_callable=AsyncMock) as mock_sum:
                await handle_callback(upd_bad_sum, context_sum)
                mock_sum.assert_not_called()

            upd_bad_rew, _ = make_mock_update("reword:99999", user_id=user_id, chat_id=chat_id)
            with patch("bot.handlers.callbacks.reword_text", new_callable=AsyncMock) as mock_rew:
                await handle_callback(upd_bad_rew, context_rew)
                mock_rew.assert_not_called()

    # 10. beautify_text passes both disable_tools=True and allow_search=False to the provider
    async def test_beautify_text_passes_safety_flags(self):
        async def mock_stream(messages, settings):
            yield "Formatted speech"

        mock_provider = MagicMock()
        mock_provider.generate_stream = MagicMock(side_effect=mock_stream)

        with patch("bot.utils.helpers.get_ai_provider", new_callable=AsyncMock, return_value=mock_provider), \
             patch("bot.utils.helpers.AsyncSessionLocal", self.SessionLocal):
            cleaned, model = await beautify_text(user_id=123, text="raw voice transcription")
            self.assertEqual(cleaned, "Formatted speech")

            mock_provider.generate_stream.assert_called_once()
            args, _ = mock_provider.generate_stream.call_args
            settings = args[1]
            self.assertIs(settings.get("disable_tools"), True)
            self.assertIs(settings.get("allow_search"), False)
            self.assertEqual(settings.get("temperature"), 0.0)

    # UI / Keyboard tests for handle_voice_video
    async def test_handle_voice_video_binds_exact_id_and_renames_button(self):
        with patch("bot.handlers.media.context_manager.save_message", new_callable=AsyncMock, return_value=777) as mock_save, \
             patch("bot.handlers.media.send_long_message", new_callable=AsyncMock) as mock_send, \
             patch("bot.handlers.media.get_ai_provider", new_callable=AsyncMock) as mock_get_provider, \
             patch("bot.handlers.media.check_transcription_limit", new_callable=AsyncMock, return_value=(True, "")), \
             patch("bot.handlers.media.record_transcription_usage", new_callable=AsyncMock), \
             patch("bot.handlers.media.download_file", new_callable=AsyncMock, return_value="/tmp/test.ogg"), \
             patch("bot.handlers.media.validate_audio_size"), \
             patch("bot.handlers.media.beautify_text", new_callable=AsyncMock, return_value=("Cleaned text", "gpt-4o-mini")), \
             patch("bot.handlers.media.get_user_model_settings", new_callable=AsyncMock, return_value={}), \
             patch("bot.handlers.media.cleanup_files"):

            mock_provider = MagicMock()
            mock_provider.transcribe = AsyncMock(return_value="raw text")
            mock_get_provider.return_value = mock_provider

            update = MagicMock()
            update.message.voice = MagicMock(file_id="voice_123", duration=5)
            update.message.video_note = None
            update.message.video = None
            update.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), delete=AsyncMock()))
            update.effective_user.id = 111
            update.effective_chat.id = 222
            update.effective_chat.type = "private"

            context = MagicMock()
            context.bot.get_file = AsyncMock()

            await handle_voice_video(update, context)

            mock_save.assert_awaited_once_with(111, 222, "transcription", "Cleaned text")
            mock_send.assert_awaited_once()
            kb = mock_send.call_args.kwargs.get("reply_markup")
            self.assertIsNotNone(kb)
            button_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
            button_texts = [btn.text for row in kb.inline_keyboard for btn in row]

            self.assertIn("run_gpt:777", button_data)
            self.assertIn("summarize:777", button_data)
            self.assertIn("reword:777", button_data)
            self.assertIn("delete_msg", button_data)
            self.assertIn("▶️ Обробити як інструкцію", button_texts)
            self.assertNotIn("🤖 Відправити боту", button_texts)

    async def test_handle_voice_video_omits_action_buttons_on_save_failure(self):
        with patch("bot.handlers.media.context_manager.save_message", new_callable=AsyncMock, return_value=None), \
             patch("bot.handlers.media.send_long_message", new_callable=AsyncMock) as mock_send, \
             patch("bot.handlers.media.get_ai_provider", new_callable=AsyncMock) as mock_get_provider, \
             patch("bot.handlers.media.check_transcription_limit", new_callable=AsyncMock, return_value=(True, "")), \
             patch("bot.handlers.media.record_transcription_usage", new_callable=AsyncMock), \
             patch("bot.handlers.media.download_file", new_callable=AsyncMock, return_value="/tmp/test.ogg"), \
             patch("bot.handlers.media.validate_audio_size"), \
             patch("bot.handlers.media.beautify_text", new_callable=AsyncMock, return_value=("Cleaned text", "gpt-4o-mini")), \
             patch("bot.handlers.media.get_user_model_settings", new_callable=AsyncMock, return_value={}), \
             patch("bot.handlers.media.cleanup_files"):

            mock_provider = MagicMock()
            mock_provider.transcribe = AsyncMock(return_value="raw text")
            mock_get_provider.return_value = mock_provider

            update = MagicMock()
            update.message.voice = MagicMock(file_id="voice_123", duration=5)
            update.message.video_note = None
            update.message.video = None
            update.message.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), delete=AsyncMock()))
            update.effective_user.id = 111
            update.effective_chat.id = 222
            update.effective_chat.type = "private"

            context = MagicMock()
            context.bot.get_file = AsyncMock()

            await handle_voice_video(update, context)

            mock_send.assert_awaited_once()
            kb = mock_send.call_args.kwargs.get("reply_markup")
            self.assertIsNotNone(kb)
            button_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
            self.assertEqual(button_data, ["delete_msg"])
            self.assertIn("Cleaned text", mock_send.call_args.args[1])

    async def test_summarize_and_reword_disable_tools(self):
        """Verify summarize_text and reword_text set disable_tools=True in settings."""
        from bot.handlers.ai import summarize_text, reword_text

        # 1. summarize_text
        update_sum = MagicMock()
        update_sum.effective_user.id = 123
        update_sum.effective_chat.id = 456
        update_sum.callback_query.message.message_id = 99
        update_sum.callback_query.message.reply_text = AsyncMock()

        mock_provider = MagicMock()
        mock_stream = AsyncMock()
        with patch("bot.handlers.ai.get_ai_provider", AsyncMock(return_value=mock_provider)), \
             patch("bot.handlers.ai.get_user_model_settings", AsyncMock(return_value={})), \
             patch("bot.handlers.ai.stream_response", mock_stream):

            await summarize_text(update_sum, MagicMock(), "Text to summarize")
            mock_stream.assert_awaited_once()
            settings = mock_stream.call_args[0][5]
            self.assertTrue(settings.get("disable_tools"))
            self.assertFalse(settings.get("allow_search"))

        # 2. reword_text
        update_rew = MagicMock()
        update_rew.effective_user.id = 123
        update_rew.effective_chat.id = 456
        update_rew.callback_query.message.message_id = 99
        update_rew.callback_query.message.reply_text = AsyncMock()

        mock_stream_rew = AsyncMock()
        with patch("bot.handlers.ai.get_ai_provider", AsyncMock(return_value=mock_provider)), \
             patch("bot.handlers.ai.get_user_model_settings", AsyncMock(return_value={})), \
             patch("bot.handlers.ai.stream_response", mock_stream_rew):

            await reword_text(update_rew, MagicMock(), "Text to reword")
            mock_stream_rew.assert_awaited_once()
            settings_rew = mock_stream_rew.call_args[0][5]
            self.assertTrue(settings_rew.get("disable_tools"))
            self.assertFalse(settings_rew.get("allow_search"))


if __name__ == "__main__":
    unittest.main()
