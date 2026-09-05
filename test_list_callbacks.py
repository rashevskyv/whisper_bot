import asyncio
import logging
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, text

from bot.database.models import Base, UserList, ListItem, ActionDraft
from bot.ai.tools import (
    ToolResult,
    execute_tool,
    format_shopping_list_view,
    _format_shopping_list_view,
)
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.openrouter_provider import OpenRouterProvider
from bot.ai.google_provider import GoogleProvider
from bot.handlers.ai import (
    build_draft_reply_markup,
    build_shopping_list_view,
    stream_response,
)
from bot.handlers.callbacks import handle_callback
from bot.utils.lists import (
    LIST_TYPE_SHOPPING,
    DEFAULT_SHOPPING_LIST_NAME,
    create_or_get_user_list,
    get_user_list,
    get_list_item,
    list_list_items,
    list_user_lists,
    add_list_items,
    set_list_item_done,
    delete_list_item,
    clear_done_list_items,
)
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    DRAFT_STATUS_PENDING_CONFIRMATION,
)
from telegram.constants import ParseMode


def make_mock_update(callback_data: str, user_id: int = 123, chat_id: int = 456, message_id: int = 999):
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = message_id
    query.message.reply_text = AsyncMock()
    query.message.edit_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=chat_id)
    return update, query


class MockAsyncStream:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class TestListCallbacksBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.lists.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.common.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()


# ==============================================================================
# A. ToolResult / Provider / Stream metadata (1-12)
# ==============================================================================

class TestListMetadataAndStream(TestListCallbacksBase):
    # 1. ToolResult.shopping_list_id за замовчуванням None.
    def test_01_tool_result_shopping_list_id_default_none(self):
        res = ToolResult(payload={"ok": True})
        self.assertIsNone(res.shopping_list_id)

    # 2. Persisted show_shopping_list повертає exact shopping_list_id.
    async def test_02_show_shopping_list_persisted_returns_shopping_list_id(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        res = await execute_tool("show_shopping_list", {}, user_id=1, chat_id=100)
        self.assertTrue(res.payload.get("success"))
        self.assertEqual(res.shopping_list_id, ul.id)

    # 3. Virtual empty show повертає shopping_list_id=None і не створює DB list.
    async def test_03_show_shopping_list_virtual_returns_none_and_no_db_list(self):
        res = await execute_tool("show_shopping_list", {}, user_id=1, chat_id=200)
        self.assertTrue(res.payload.get("success"))
        self.assertIsNone(res.shopping_list_id)
        # Check no list created in chat 200
        lists = await list_user_lists(200)
        self.assertEqual(len(lists), 0)

    # 4. OpenAI provider переносить ID у settings["_shopping_list_id"].
    async def test_04_openai_provider_propagates_shopping_list_id(self):
        provider = OpenAIProvider(api_key="test-key")
        settings = {"source_message_id": 1}

        # Mock client and tool call in stream
        mock_choice = MagicMock()
        mock_choice.delta.content = ""
        mock_choice.delta.tool_calls = [
            MagicMock(
                index=0,
                id="call_1",
                function=MagicMock(name="show_shopping_list", arguments="{}")
            )
        ]
        mock_chunk = MagicMock(choices=[mock_choice])

        async def mock_create(*args, **kwargs):
            return MockAsyncStream([mock_chunk])

        provider.client = MagicMock()
        provider.client.chat.completions.create = mock_create

        fake_res = ToolResult(
            payload={"success": True, "list_id": 42},
            display_text="🛒 Список",
            stop=True,
            shopping_list_id=42,
        )

        with patch("bot.ai.openai_provider.execute_tool", AsyncMock(return_value=fake_res)):
            chunks = [c async for c in provider.generate_stream([{"role": "user", "content": "покажи список"}], settings)]

        self.assertEqual(settings.get("_shopping_list_id"), 42)

    # 5. OpenRouter provider переносить ID.
    async def test_05_openrouter_provider_propagates_shopping_list_id(self):
        provider = OpenRouterProvider(api_key="test-key")
        settings = {"source_message_id": 1}

        mock_choice = MagicMock()
        mock_choice.delta.content = ""
        mock_choice.delta.tool_calls = [
            MagicMock(
                index=0,
                id="call_1",
                function=MagicMock(name="show_shopping_list", arguments="{}")
            )
        ]
        mock_chunk = MagicMock(choices=[mock_choice])

        async def mock_create(*args, **kwargs):
            return MockAsyncStream([mock_chunk])

        provider.client = MagicMock()
        provider.client.chat.completions.create = mock_create

        fake_res = ToolResult(
            payload={"success": True, "list_id": 55},
            display_text="🛒 Список",
            stop=True,
            shopping_list_id=55,
        )

        with patch("bot.ai.openrouter_provider.execute_tool", AsyncMock(return_value=fake_res)):
            chunks = [c async for c in provider.generate_stream([{"role": "user", "content": "список"}], settings)]

        self.assertEqual(settings.get("_shopping_list_id"), 55)

    # 6. Google provider переносить ID.
    async def test_06_google_provider_propagates_shopping_list_id(self):
        provider = GoogleProvider(api_key="test-key")
        settings = {"source_message_id": 1}

        fc_part = MagicMock()
        fc_part.name = "show_shopping_list"
        fc_part.args = {}
        part = MagicMock()
        part.function_call = fc_part
        candidate = MagicMock()
        candidate.content.parts = [part]
        chunk = MagicMock(candidates=[candidate], text=None)

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([chunk]))

        fake_res = ToolResult(
            payload={"success": True, "list_id": 88},
            display_text="🛒 Список",
            stop=True,
            shopping_list_id=88,
        )

        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.google_provider.execute_tool", AsyncMock(return_value=fake_res)):
            chunks = [c async for c in provider.generate_stream([{"role": "user", "content": "список"}], settings)]

        self.assertEqual(settings.get("_shopping_list_id"), 88)

    # 7. Shopping metadata очищає transient draft metadata.
    async def test_07_shopping_metadata_clears_transient_draft_metadata(self):
        provider = OpenAIProvider(api_key="test-key")
        settings = {"_action_draft_id": 999}

        mock_choice = MagicMock()
        mock_choice.delta.content = ""
        mock_choice.delta.tool_calls = [
            MagicMock(index=0, id="call_1", function=MagicMock(name="show_shopping_list", arguments="{}"))
        ]
        mock_chunk = MagicMock(choices=[mock_choice])

        async def mock_create(*args, **kwargs):
            return MockAsyncStream([mock_chunk])

        provider.client = MagicMock()
        provider.client.chat.completions.create = mock_create

        fake_res = ToolResult(
            payload={"success": True, "list_id": 77},
            display_text="🛒 Список",
            stop=True,
            shopping_list_id=77,
        )

        with patch("bot.ai.openai_provider.execute_tool", AsyncMock(return_value=fake_res)):
            _ = [c async for c in provider.generate_stream([{"role": "user", "content": "список"}], settings)]

        self.assertEqual(settings.get("_shopping_list_id"), 77)
        self.assertNotIn("_action_draft_id", settings)

    # 8. Draft metadata очищає transient shopping metadata.
    async def test_08_draft_metadata_clears_transient_shopping_metadata(self):
        provider = OpenAIProvider(api_key="test-key")
        settings = {"_shopping_list_id": 77}

        mock_choice = MagicMock()
        mock_choice.delta.content = ""
        mock_choice.delta.tool_calls = [
            MagicMock(index=0, id="call_1", function=MagicMock(name="add_shopping_items", arguments="{}"))
        ]
        mock_chunk = MagicMock(choices=[mock_choice])

        async def mock_create(*args, **kwargs):
            return MockAsyncStream([mock_chunk])

        provider.client = MagicMock()
        provider.client.chat.completions.create = mock_create

        fake_res = ToolResult(
            payload={"success": True, "draft_id": 123},
            display_text="Прев'ю чернетки",
            stop=True,
            draft_id=123,
        )

        with patch("bot.ai.openai_provider.execute_tool", AsyncMock(return_value=fake_res)):
            _ = [c async for c in provider.generate_stream([{"role": "user", "content": "купи хліб"}], settings)]

        self.assertEqual(settings.get("_action_draft_id"), 123)
        self.assertNotIn("_shopping_list_id", settings)

    # 9. stream_response прикріплює shopping markup до persisted-list response.
    async def test_09_stream_response_attaches_shopping_markup(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        await add_list_items(ul.id, 100, 1, ["Яблука"])

        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        status_msg.chat = MagicMock()

        class MockProvider:
            async def generate_stream(self, messages, settings):
                yield "Ось ваш список покупок:"

        provider = MockProvider()
        settings = {"_shopping_list_id": ul.id}

        with patch("bot.handlers.ai.context_manager.save_message", AsyncMock()):
            await stream_response(provider, [], status_msg, user_id=1, chat_id=100, settings=settings)

        status_msg.edit_text.assert_awaited()
        call_kwargs = status_msg.edit_text.call_args.kwargs
        reply_markup = call_kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        # Check that markup contains list buttons
        button_callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
        self.assertTrue(any(cb.startswith("list:done:") for cb in button_callbacks))

    # 10. Draft markup має пріоритет над shopping markup.
    async def test_10_draft_markup_has_priority_over_shopping(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        draft = await create_action_draft(
            user_id=1, chat_id=100, action_type="schedule_reminder",
            payload={"text": "Тест", "iso_time_utc": "2026-10-10T10:00:00+00:00"},
            missing_fields=[]
        )

        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()

        class MockProvider:
            async def generate_stream(self, messages, settings):
                yield "Відповідь моделі"

        provider = MockProvider()
        settings = {"_action_draft_id": draft.id, "_shopping_list_id": ul.id}

        with patch("bot.handlers.ai.context_manager.save_message", AsyncMock()):
            await stream_response(provider, [], status_msg, user_id=1, chat_id=100, settings=settings)

        status_msg.edit_text.assert_awaited()
        reply_markup = status_msg.edit_text.call_args.kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
        self.assertIn(f"draft:ok:{draft.id}", callbacks)
        self.assertFalse(any(cb.startswith("list:") for cb in callbacks))

    # 11. Unrelated response не отримує shopping markup.
    async def test_11_unrelated_response_no_shopping_markup(self):
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()

        class MockProvider:
            async def generate_stream(self, messages, settings):
                yield "Звичайний текст"

        provider = MockProvider()
        settings = {}

        with patch("bot.handlers.ai.context_manager.save_message", AsyncMock()):
            await stream_response(provider, [], status_msg, user_id=1, chat_id=100, settings=settings)

        status_msg.edit_text.assert_awaited()
        reply_markup = status_msg.edit_text.call_args.kwargs.get("reply_markup")
        self.assertIsNone(reply_markup)

    # 12. Long-message path прикріплює markup лише до останньої частини.
    async def test_12_long_message_shopping_markup_only_on_last_part(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        await add_list_items(ul.id, 100, 1, ["Хліб"])

        status_msg = MagicMock()
        status_msg.delete = AsyncMock()
        status_msg.chat = MagicMock()

        # > 4000 chars response
        long_text = "A" * 4500

        class MockProvider:
            async def generate_stream(self, messages, settings):
                yield long_text

        provider = MockProvider()
        settings = {"_shopping_list_id": ul.id}

        with patch("bot.handlers.ai.send_long_message", AsyncMock()) as mock_send_long, \
             patch("bot.handlers.ai.context_manager.save_message", AsyncMock()):
            await stream_response(provider, [], status_msg, user_id=1, chat_id=100, settings=settings)

        status_msg.delete.assert_awaited_once()
        mock_send_long.assert_awaited_once()
        passed_markup = mock_send_long.call_args.kwargs.get("reply_markup")
        self.assertIsNotNone(passed_markup)
        callbacks = [btn.callback_data for row in passed_markup.inline_keyboard for btn in row]
        self.assertTrue(any(cb.startswith("list:done:") for cb in callbacks))


# ==============================================================================
# B. Keyboard Builder (13-23)
# ==============================================================================

class TestShoppingKeyboardBuilder(TestListCallbacksBase):
    # 13. Active item має list:done:<list_id>:<item_id>.
    async def test_13_active_item_done_callback(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Молоко"])
        item = items[0]

        _, markup = await build_shopping_list_view(ul.id, 100)
        self.assertIsNotNone(markup)
        toggle_btn = markup.inline_keyboard[0][0]
        self.assertEqual(toggle_btn.callback_data, f"list:done:{ul.id}:{item.id}")
        self.assertIn("✅", toggle_btn.text)
        self.assertIn(f"#{item.id}", toggle_btn.text)

    # 14. Done item має list:undo:<list_id>:<item_id>.
    async def test_14_done_item_undo_callback(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Молоко"])
        item = items[0]
        await set_list_item_done(item.id, 100, 1, True)

        _, markup = await build_shopping_list_view(ul.id, 100)
        self.assertIsNotNone(markup)
        toggle_btn = markup.inline_keyboard[0][0]
        self.assertEqual(toggle_btn.callback_data, f"list:undo:{ul.id}:{item.id}")
        self.assertIn("↩️", toggle_btn.text)
        self.assertIn(f"#{item.id}", toggle_btn.text)

    # 15. Кожен item має delete button list:del:<list_id>:<item_id>.
    async def test_15_each_item_has_delete_button(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        _, markup = await build_shopping_list_view(ul.id, 100)
        self.assertIsNotNone(markup)
        del_btn = markup.inline_keyboard[0][1]
        self.assertEqual(del_btn.text, "🗑")
        self.assertEqual(del_btn.callback_data, f"list:del:{ul.id}:{item.id}")

    # 16. Clear button є лише коли існує done item.
    async def test_16_clear_button_only_when_done_item_exists(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб", "Масло"])

        # No done items
        _, markup_no_done = await build_shopping_list_view(ul.id, 100)
        callbacks_no_done = [b.callback_data for r in markup_no_done.inline_keyboard for b in r]
        self.assertFalse(any(cb.startswith("list:clear:") for cb in callbacks_no_done))

        # Mark one done
        await set_list_item_done(items[0].id, 100, 1, True)
        _, markup_done = await build_shopping_list_view(ul.id, 100)
        clear_rows = [r for r in markup_done.inline_keyboard if any(b.callback_data == f"list:clear:{ul.id}" for b in r)]
        self.assertEqual(len(clear_rows), 1)
        clear_btn = clear_rows[0][0]
        self.assertIn("🧹", clear_btn.text)

    # 17. Empty persisted list дає text і markup=None.
    async def test_17_empty_persisted_list_text_and_no_markup(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        text, markup = await build_shopping_list_view(ul.id, 100)
        self.assertIsNotNone(text)
        self.assertIn("Список порожній", text)
        self.assertIsNone(markup)

    # 18. Foreign-chat list дає (None, None).
    async def test_18_foreign_chat_list_returns_none_none(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        text, markup = await build_shopping_list_view(ul.id, 200)
        self.assertIsNone(text)
        self.assertIsNone(markup)

    # 19. Item ordering стабільний: active перед done, ID ascending.
    async def test_19_item_ordering_active_before_done_id_ascending(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Пункт 1", "Пункт 2", "Пункт 3", "Пункт 4"])
        # Mark 1 and 3 done
        await set_list_item_done(items[0].id, 100, 1, True)
        await set_list_item_done(items[2].id, 100, 1, True)

        _, markup = await build_shopping_list_view(ul.id, 100)
        # First two rows must be items 2 and 4 (active)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"list:done:{ul.id}:{items[1].id}")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, f"list:done:{ul.id}:{items[3].id}")
        # Next two rows must be items 1 and 3 (done)
        self.assertEqual(markup.inline_keyboard[2][0].callback_data, f"list:undo:{ul.id}:{items[0].id}")
        self.assertEqual(markup.inline_keyboard[3][0].callback_data, f"list:undo:{ul.id}:{items[2].id}")

    # 20. Button labels обрізані та не містять newline.
    async def test_20_button_labels_trimmed_and_no_newlines(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        long_item = "Дуже довгий текст пункт\nз переносом рядка і багатьма словами"
        items = await add_list_items(ul.id, 100, 1, [long_item])
        item = items[0]

        _, markup = await build_shopping_list_view(ul.id, 100)
        label = markup.inline_keyboard[0][0].text
        self.assertNotIn("\n", label)
        self.assertTrue(label.endswith("…"))
        self.assertIn(f"#{item.id}", label)

    # 21. Keyboard має максимум 30 item rows.
    async def test_21_keyboard_maximum_30_item_rows(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        batch = [f"Item {i}" for i in range(40)]
        await add_list_items(ul.id, 100, 1, batch)

        _, markup = await build_shopping_list_view(ul.id, 100)
        # ponytail: first 30 item controls; add pagination only when large-list UX needs it.
        item_rows = [r for r in markup.inline_keyboard if r[0].callback_data.startswith("list:done:")]
        self.assertEqual(len(item_rows), 30)

    # 22. Усі callback payloads для 64-bit IDs коротші за 64 UTF-8 bytes.
    def test_22_callback_payloads_fit_64_bytes(self):
        max_id = 9223372036854775807
        cb_done = f"list:done:{max_id}:{max_id}"
        cb_undo = f"list:undo:{max_id}:{max_id}"
        cb_del = f"list:del:{max_id}:{max_id}"
        cb_clear = f"list:clear:{max_id}"

        for cb in (cb_done, cb_undo, cb_del, cb_clear):
            self.assertLessEqual(len(cb.encode("utf-8")), 64)

    # 23. DB exception не виходить із builder і не містить raw exception/content у логах.
    async def test_23_db_exception_handled_and_sanitized(self):
        with patch("bot.handlers.ai.get_user_list", AsyncMock(side_effect=Exception("RAW_SQL_SECRET"))), \
             self.assertLogs("bot.handlers.ai", level="ERROR") as log_capture:
            text, markup = await build_shopping_list_view(1, 100)

        self.assertIsNone(text)
        self.assertIsNone(markup)
        joined_logs = "\n".join(log_capture.output)
        self.assertNotIn("RAW_SQL_SECRET", joined_logs)


# ==============================================================================
# C. Parser / Security (24-29)
# ==============================================================================

class TestListCallbacksParserAndSecurity(TestListCallbacksBase):
    # 24. Відхили всі malformed callbacks.
    async def test_24_rejects_malformed_callbacks(self):
        malformed_list = [
            "list:",
            "list:done",
            "list:done:1",
            "list:done:1:2:3",
            "list:unknown:1:2",
            "list:done:a:2",
            "list:done:1:b",
            "list:done:0:1",
            "list:done:1:0",
            "list:done:-1:2",
            "list:clear",
            "list:clear:1:2",
            "list:clear:abc",
        ]

        context = MagicMock()
        for cb in malformed_list:
            update, query = make_mock_update(cb)
            await handle_callback(update, context)
            query.answer.assert_awaited_with("❌ Некоректні дані запиту.", show_alert=True)
            query.message.edit_text.assert_not_awaited()
            query.message.edit_reply_markup.assert_not_awaited()

    # 25. Malformed callbacks роблять 0 DB calls.
    async def test_25_malformed_callbacks_make_zero_db_calls(self):
        context = MagicMock()
        update, query = make_mock_update("list:done:abc:123")

        with patch("bot.handlers.callbacks.get_user_list", AsyncMock()) as mock_list:
            await handle_callback(update, context)
            mock_list.assert_not_awaited()

    # 26. Invalid user/chat/message робить 0 mutations.
    async def test_26_invalid_user_chat_message_zero_mutations(self):
        context = MagicMock()

        # Invalid user id
        update1, query1 = make_mock_update("list:done:1:1", user_id=-5)
        await handle_callback(update1, context)
        query1.answer.assert_awaited_with("❌ Некоректні дані запиту.", show_alert=True)

        # Chat id == 0
        update2, query2 = make_mock_update("list:done:1:1", chat_id=0)
        await handle_callback(update2, context)
        query2.answer.assert_awaited_with("❌ Некоректні дані запиту.", show_alert=True)

        # query.message is None
        update3, query3 = make_mock_update("list:done:1:1")
        query3.message = None
        await handle_callback(update3, context)
        query3.answer.assert_awaited_with("❌ Некоректні дані запиту.", show_alert=True)

    # 27. Foreign list/chat не мутує і не редагує message.
    async def test_27_foreign_list_chat_no_mutation_no_edit(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        # Request from chat 200 for list in chat 100
        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=1, chat_id=200)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
        query.message.edit_text.assert_not_awaited()
        query.message.edit_reply_markup.assert_not_awaited()

        # Item remains active
        db_item = await get_list_item(item.id, 100)
        self.assertFalse(db_item.is_done)

    # 28. Foreign item не розкриває існування.
    async def test_28_foreign_item_does_not_disclose_existence(self):
        ul200, _ = await create_or_get_user_list(1, 200, LIST_TYPE_SHOPPING, "Покупки")
        items200 = await add_list_items(ul200.id, 200, 1, ["Секретний товар"])
        item200 = items200[0]

        ul100, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")

        # In chat 100, callback tries to manipulate item from chat 200
        update, query = make_mock_update(f"list:done:{ul100.id}:{item200.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)

    # 29. Item іншого list у тому самому chat не мутує.
    async def test_29_item_of_another_list_in_same_chat_no_mutation(self):
        ul1, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 1")
        ul2, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 2")

        items2 = await add_list_items(ul2.id, 100, 1, ["Товар зі списку 2"])
        item2 = items2[0]

        # Callback passes ul1.id with item2.id
        update, query = make_mock_update(f"list:done:{ul1.id}:{item2.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
        # Verify item2 in ul2 is not mutated
        db_item2 = await get_list_item(item2.id, 100)
        self.assertFalse(db_item2.is_done)


# ==============================================================================
# D. State Callbacks (30-36)
# ==============================================================================

class TestListStateCallbacks(TestListCallbacksBase):
    # 30. done атомарно переводить active item у done.
    async def test_30_done_transitions_active_to_done(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("✅ Позначено купленим.")
        db_item = await get_list_item(item.id, 100)
        self.assertTrue(db_item.is_done)

    # 31. updated_by_user_id стає ID callback actor.
    async def test_31_updated_by_user_id_records_actor(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=99, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        db_item = await get_list_item(item.id, 100)
        self.assertEqual(db_item.updated_by_user_id, 99)

    # 32. Повторний done є idempotent і не падає.
    async def test_32_repeated_done_is_idempotent(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]
        await set_list_item_done(item.id, 100, 1, True)

        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("ℹ️ Пункт уже позначено купленим.", show_alert=True)
        db_item = await get_list_item(item.id, 100)
        self.assertTrue(db_item.is_done)

    # 33. undo переводить done item в active.
    async def test_33_undo_transitions_done_to_active(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]
        await set_list_item_done(item.id, 100, 1, True)

        update, query = make_mock_update(f"list:undo:{ul.id}:{item.id}", user_id=2, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("↩️ Повернуто до активних.")
        db_item = await get_list_item(item.id, 100)
        self.assertFalse(db_item.is_done)
        self.assertEqual(db_item.updated_by_user_id, 2)

    # 34. Повторний undo є idempotent.
    async def test_34_repeated_undo_is_idempotent(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        update, query = make_mock_update(f"list:undo:{ul.id}:{item.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("ℹ️ Пункт уже є активним.", show_alert=True)
        db_item = await get_list_item(item.id, 100)
        self.assertFalse(db_item.is_done)

    # 35. Після done/undo message text і markup відповідають DB state.
    async def test_35_done_undo_refreshes_message_text_and_markup(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.message.edit_text.assert_awaited_once()
        args, kwargs = query.message.edit_text.call_args
        text = args[0] if args else kwargs.get("text")
        markup = kwargs.get("reply_markup")

        # Now marked done, so text has ✅ #item.id
        self.assertIn(f"✅ #{item.id} Хліб", text)
        # Button toggle is undo
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"list:undo:{ul.id}:{item.id}")

    # 36. Group member, відмінний від creator, може змінити shared chat list.
    async def test_36_group_member_different_from_creator_can_update(self):
        group_chat_id = -10012345
        ul, _ = await create_or_get_user_list(user_id=10, chat_id=group_chat_id, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, group_chat_id, 10, ["Чай"])
        item = items[0]

        # Other user 25 in same group chat clicks
        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=25, chat_id=group_chat_id)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("✅ Позначено купленим.")
        db_item = await get_list_item(item.id, group_chat_id)
        self.assertTrue(db_item.is_done)
        self.assertEqual(db_item.updated_by_user_id, 25)


# ==============================================================================
# E. Delete / Clear Callbacks (37-45)
# ==============================================================================

class TestListDeleteAndClearCallbacks(TestListCallbacksBase):
    # 37. Delete видаляє exact current-chat/current-list item.
    async def test_37_delete_removes_exact_item(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        update, query = make_mock_update(f"list:del:{ul.id}:{item.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("🗑 Пункт видалено.")
        self.assertIsNone(await get_list_item(item.id, 100))

    # 38. Повторний/stale delete не падає і refresh-ить list.
    async def test_38_repeated_stale_delete_refreshes_list(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        update, query = make_mock_update(f"list:del:{ul.id}:9999", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("ℹ️ Пункт уже видалено або недоступний.", show_alert=True)
        query.message.edit_text.assert_awaited_once()

    # 39. Delete не може видалити item іншого list через підміну list_id.
    async def test_39_delete_cannot_delete_item_of_another_list(self):
        ul1, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 1")
        ul2, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 2")
        items2 = await add_list_items(ul2.id, 100, 1, ["Цільовий пункт 2"])
        item2 = items2[0]

        update, query = make_mock_update(f"list:del:{ul1.id}:{item2.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("ℹ️ Пункт уже видалено або недоступний.", show_alert=True)
        self.assertIsNotNone(await get_list_item(item2.id, 100))
        query.message.edit_text.assert_awaited_once()

    # 40. Clear видаляє лише done items exact list.
    async def test_40_clear_deletes_only_done_items(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Активний 1", "Куплений 1", "Куплений 2"])
        await set_list_item_done(items[1].id, 100, 1, True)
        await set_list_item_done(items[2].id, 100, 1, True)

        update, query = make_mock_update(f"list:clear:{ul.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("🧹 Видалено куплених пунктів: 2.")
        remaining = await list_list_items(ul.id, 100)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, items[0].id)

    # 41. Active items після clear залишаються.
    async def test_41_active_items_remain_after_clear(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб", "Молоко"])
        await set_list_item_done(items[0].id, 100, 1, True)

        update, query = make_mock_update(f"list:clear:{ul.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        remaining = await list_list_items(ul.id, 100)
        self.assertEqual([it.text for it in remaining], ["Молоко"])

    # 42. Clear не зачіпає інший list того самого chat.
    async def test_42_clear_does_not_affect_another_list_in_same_chat(self):
        ul1, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 1")
        ul2, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 2")

        items1 = await add_list_items(ul1.id, 100, 1, ["Пункт 1"])
        await set_list_item_done(items1[0].id, 100, 1, True)

        items2 = await add_list_items(ul2.id, 100, 1, ["Пункт 2"])
        await set_list_item_done(items2[0].id, 100, 1, True)

        update, query = make_mock_update(f"list:clear:{ul1.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        # ul2 items intact
        ul2_items = await list_list_items(ul2.id, 100)
        self.assertEqual(len(ul2_items), 1)
        self.assertTrue(ul2_items[0].is_done)

    # 43. Clear не зачіпає інший chat.
    async def test_43_clear_does_not_affect_another_chat(self):
        ul100, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        ul200, _ = await create_or_get_user_list(1, 200, LIST_TYPE_SHOPPING, "Покупки")

        items100 = await add_list_items(ul100.id, 100, 1, ["Чат 100"])
        await set_list_item_done(items100[0].id, 100, 1, True)

        items200 = await add_list_items(ul200.id, 200, 1, ["Чат 200"])
        await set_list_item_done(items200[0].id, 200, 1, True)

        update, query = make_mock_update(f"list:clear:{ul100.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        # ul200 items intact
        ul200_items = await list_list_items(ul200.id, 200)
        self.assertEqual(len(ul200_items), 1)

    # 44. Повторний clear з count=0 є idempotent success.
    async def test_44_repeated_clear_with_count_zero_is_idempotent(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        await add_list_items(ul.id, 100, 1, ["Активний"])

        update, query = make_mock_update(f"list:clear:{ul.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.answer.assert_awaited_with("ℹ️ Куплених пунктів немає.", show_alert=True)

    # 45. Після delete/clear text і keyboard перебудовані з актуальної DB.
    async def test_45_after_delete_clear_text_and_keyboard_rebuilt_from_db(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб", "Масло"])
        await set_list_item_done(items[0].id, 100, 1, True)

        update, query = make_mock_update(f"list:clear:{ul.id}", user_id=1, chat_id=100)
        context = MagicMock()
        await handle_callback(update, context)

        query.message.edit_text.assert_awaited_once()
        args, kwargs = query.message.edit_text.call_args
        text = args[0] if args else kwargs.get("text")
        markup = kwargs.get("reply_markup")

        self.assertNotIn("Хліб", text)
        self.assertIn("Масло", text)
        # Clear button should be gone now that no done items exist
        callbacks = [b.callback_data for r in markup.inline_keyboard for b in r]
        self.assertFalse(any(cb.startswith("list:clear:") for cb in callbacks))


# ==============================================================================
# F. Failure / Race Behavior (46-50)
# ==============================================================================

class TestListFailureAndRaceBehavior(TestListCallbacksBase):
    # 46. DB exception до mutation: stable alert, message не редагується, raw marker відсутній.
    async def test_46_db_exception_before_mutation_safe(self):
        update, query = make_mock_update("list:done:1:1", user_id=1, chat_id=100)
        context = MagicMock()

        with patch("bot.handlers.callbacks.get_user_list", AsyncMock(side_effect=Exception("RAW_SENSITIVE_DB_ERROR"))), \
             self.assertLogs("bot.handlers.callbacks", level="ERROR") as log_capture:
            await handle_callback(update, context)

        query.answer.assert_awaited_with("⚠️ Не вдалося оновити список через помилку бази даних. Спробуйте ще раз.", show_alert=True)
        query.message.edit_text.assert_not_awaited()
        joined_logs = "\n".join(log_capture.output)
        self.assertNotIn("RAW_SENSITIVE_DB_ERROR", joined_logs)

    # 47. UI refresh exception після successful mutation: DB збережена, mutation викликана 1 раз, raw marker відсутній.
    async def test_47_ui_refresh_exception_after_successful_mutation(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        update, query = make_mock_update(f"list:del:{ul.id}:{item.id}", user_id=1, chat_id=100)
        query.message.edit_text.side_effect = Exception("TELEGRAM_NETWORK_GLITCH")
        context = MagicMock()

        with self.assertLogs("bot.handlers.callbacks", level="ERROR") as log_capture:
            await handle_callback(update, context)

        # DB mutation persisted
        self.assertIsNone(await get_list_item(item.id, 100))
        # User informed to reopen list
        query.message.reply_text.assert_awaited_with(
            "⚠️ Дію виконано, але не вдалося оновити повідомлення. Будь ласка, відкрийте список знову."
        )
        joined_logs = "\n".join(log_capture.output)
        self.assertNotIn("TELEGRAM_NETWORK_GLITCH", joined_logs)

    # 48. Item зникає між validation і atomic mutation: callback не падає, list refresh виконується, stale button зникає.
    async def test_48_item_disappears_between_validation_and_mutation(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Хліб"])
        item = items[0]

        # Concurrently delete before set_list_item_done executes
        real_get = get_list_item
        async def race_get(i_id, c_id):
            res = await real_get(i_id, c_id)
            # simulate race delete
            await delete_list_item(i_id, c_id, 1)
            return res

        update, query = make_mock_update(f"list:done:{ul.id}:{item.id}", user_id=1, chat_id=100)
        context = MagicMock()

        with patch("bot.handlers.callbacks.get_list_item", side_effect=race_get):
            await handle_callback(update, context)

        query.answer.assert_awaited_with("ℹ️ Пункт уже видалено або не знайдено.", show_alert=True)
        query.message.edit_text.assert_awaited_once()


class TestListConcurrencyWAL(unittest.IsolatedAsyncioTestCase):
    """File-backed SQLite WAL concurrency tests for Scenarios 49 & 50."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp_dir.name, "list_cb_concurrency.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
            await conn.execute(text("PRAGMA busy_timeout=5000;"))
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patchers = [
            patch("bot.utils.lists.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.common.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    # 49. Два одночасні done callbacks: рівно один changed=True на DB-рівні, final state done, немає exception.
    async def test_49_concurrent_done_callbacks_single_winner(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Спільний сир"])
        item_id = items[0].id

        task1 = set_list_item_done(item_id=item_id, chat_id=100, actor_user_id=10, is_done=True)
        task2 = set_list_item_done(item_id=item_id, chat_id=100, actor_user_id=20, is_done=True)

        res1, res2 = await asyncio.gather(task1, task2)
        changed_flags = [res1[1], res2[1]]

        self.assertEqual(changed_flags.count(True), 1)
        self.assertEqual(changed_flags.count(False), 1)

        db_item = await get_list_item(item_id, 100)
        self.assertTrue(db_item.is_done)

    # 50. Два одночасні delete callbacks: рівно один видаляє, final item absent, другий є safe stale outcome.
    async def test_50_concurrent_delete_callbacks_single_winner(self):
        ul, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Покупки")
        items = await add_list_items(ul.id, 100, 1, ["Видаляємий сир"])
        item_id = items[0].id

        task1 = delete_list_item(item_id=item_id, chat_id=100, actor_user_id=10)
        task2 = delete_list_item(item_id=item_id, chat_id=100, actor_user_id=20)

        del1, del2 = await asyncio.gather(task1, task2)
        delete_flags = [del1, del2]

        self.assertEqual(delete_flags.count(True), 1)
        self.assertEqual(delete_flags.count(False), 1)

        db_item = await get_list_item(item_id, 100)
        self.assertIsNone(db_item)


# ==============================================================================
# G. End-to-End Shopping Flow (Scenario G 1-15)
# ==============================================================================

class TestEndToEndShoppingFlow(TestListCallbacksBase):
    async def test_end_to_end_shopping_flow(self):
        user_id = 777
        chat_id = 888
        context = MagicMock()

        # 1. execute_tool("add_shopping_items", execute_mutation=False) створює ActionDraft
        draft_result = await execute_tool(
            "add_shopping_items",
            {"items": ["Хліб", "Масло"]},
            user_id=user_id,
            chat_id=chat_id,
            execute_mutation=False,
        )
        self.assertIsNotNone(draft_result.draft_id)
        draft_id = draft_result.draft_id

        # 2. draft:ok:<id> через реальний handle_callback підтверджує його
        confirm_update, confirm_query = make_mock_update(f"draft:ok:{draft_id}", user_id=user_id, chat_id=chat_id)
        await handle_callback(confirm_update, context)

        # 3. default Покупки створюється один раз
        lists = await list_user_lists(chat_id, LIST_TYPE_SHOPPING)
        self.assertEqual(len(lists), 1)
        shopping_list = lists[0]
        self.assertEqual(shopping_list.name, DEFAULT_SHOPPING_LIST_NAME)

        # 4. batch items створюються один раз
        items = await list_list_items(shopping_list.id, chat_id)
        self.assertEqual(len(items), 2)
        item_bread = next(it for it in items if it.text == "Хліб")
        item_butter = next(it for it in items if it.text == "Масло")

        # 5. повторний draft:ok не створює дублікатів
        dup_update, dup_query = make_mock_update(f"draft:ok:{draft_id}", user_id=user_id, chat_id=chat_id)
        await handle_callback(dup_update, context)
        dup_query.answer.assert_awaited_with("⚠️ Цю дію вже підтверджено.", show_alert=True)
        items_after_dup = await list_list_items(shopping_list.id, chat_id)
        self.assertEqual(len(items_after_dup), 2)

        # 6. show_shopping_list повертає persisted shopping_list_id
        show_result = await execute_tool("show_shopping_list", {}, user_id=user_id, chat_id=chat_id)
        self.assertEqual(show_result.shopping_list_id, shopping_list.id)

        # 7. handler builder формує inline keyboard
        view_text, markup = await build_shopping_list_view(shopping_list.id, chat_id)
        self.assertIsNotNone(markup)
        self.assertIn("Хліб", view_text)
        self.assertIn("Масло", view_text)

        # 8. list:done позначає item купленим
        done_update, done_query = make_mock_update(f"list:done:{shopping_list.id}:{item_bread.id}", user_id=user_id, chat_id=chat_id)
        await handle_callback(done_update, context)
        done_query.answer.assert_awaited_with("✅ Позначено купленим.")
        bread_db = await get_list_item(item_bread.id, chat_id)
        self.assertTrue(bread_db.is_done)

        # 9. list:undo повертає його
        undo_update, undo_query = make_mock_update(f"list:undo:{shopping_list.id}:{item_bread.id}", user_id=user_id, chat_id=chat_id)
        await handle_callback(undo_update, context)
        undo_query.answer.assert_awaited_with("↩️ Повернуто до активних.")
        bread_db = await get_list_item(item_bread.id, chat_id)
        self.assertFalse(bread_db.is_done)

        # 10. list:done повторно позначає купленим
        done2_update, done2_query = make_mock_update(f"list:done:{shopping_list.id}:{item_bread.id}", user_id=user_id, chat_id=chat_id)
        await handle_callback(done2_update, context)
        done2_query.answer.assert_awaited_with("✅ Позначено купленим.")

        # 11. list:clear видаляє done item
        clear_update, clear_query = make_mock_update(f"list:clear:{shopping_list.id}", user_id=user_id, chat_id=chat_id)
        await handle_callback(clear_update, context)
        clear_query.answer.assert_awaited_with("🧹 Видалено куплених пунктів: 1.")

        # 12. active item залишається
        remaining_items = await list_list_items(shopping_list.id, chat_id)
        self.assertEqual(len(remaining_items), 1)
        self.assertEqual(remaining_items[0].id, item_butter.id)
        self.assertFalse(remaining_items[0].is_done)

        # 13. final rendered text/markup відповідає DB
        final_text, final_markup = await build_shopping_list_view(shopping_list.id, chat_id)
        self.assertNotIn("Хліб", final_text)
        self.assertIn("Масло", final_text)
        # 1 row for butter (active), no clear button row
        self.assertEqual(len(final_markup.inline_keyboard), 1)
        self.assertEqual(final_markup.inline_keyboard[0][0].callback_data, f"list:done:{shopping_list.id}:{item_butter.id}")

        # 14. жодна inline list action не створює ActionDraft
        # Only the initial draft from step 1 should exist
        async with self.SessionLocal() as session:
            draft_count = await session.scalar(select(func.count(ActionDraft.id)).where(ActionDraft.chat_id == chat_id))
            self.assertEqual(draft_count, 1)

        # 15. foreign chat не може змінити final list
        foreign_update, foreign_query = make_mock_update(f"list:del:{shopping_list.id}:{item_butter.id}", user_id=user_id, chat_id=999)
        await handle_callback(foreign_update, context)
        foreign_query.answer.assert_awaited_with("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
        # Butter still exists
        butter_db = await get_list_item(item_butter.id, chat_id)
        self.assertIsNotNone(butter_db)


# ==============================================================================
# H. Security Oracle Elimination Regressions (51-54)
# ==============================================================================

class TestListSecurityOracleElimination(TestListCallbacksBase):
    # 51. Відсутність direct DB access у bot/handlers/callbacks.py
    def test_51_no_direct_db_access_in_callbacks(self):
        import bot.handlers.callbacks as cb_mod
        self.assertFalse(hasattr(cb_mod, "AsyncSessionLocal"))
        self.assertFalse(hasattr(cb_mod, "ListItem"))

        with open(cb_mod.__file__, "r", encoding="utf-8") as f:
            content = f.read()

        # Check exact import/use patterns
        self.assertIsNone(re.search(r"\bfrom\s+bot\.database\.session\s+import\s+AsyncSessionLocal\b", content))
        self.assertIsNone(re.search(r"\bfrom\s+bot\.database\.models\s+import\s+ListItem\b", content))
        self.assertIsNone(re.search(r"\bsession\.get\(ListItem\b", content))
        self.assertIsNone(re.search(r"\bAsyncSessionLocal\b", content))

    # 52. Delete oracle regression: foreign-chat item vs nonexistent item indistinguishable
    async def test_52_delete_oracle_indistinguishable(self):
        # 1. current-chat target list
        ul_current, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Мій список")
        current_items = await add_list_items(ul_current.id, 100, 1, ["Поточний товар"])

        # 2. foreign-chat list & item
        ul_foreign, _ = await create_or_get_user_list(2, 200, LIST_TYPE_SHOPPING, "Чужий секретний список")
        foreign_items = await add_list_items(ul_foreign.id, 200, 2, ["Секретний чужий товар"])
        foreign_item = foreign_items[0]

        # 3. nonexistent item ID
        nonexistent_item_id = 999999

        current_items_before = await list_list_items(ul_current.id, 100)

        cb_logger = logging.getLogger("bot.handlers.callbacks")
        captured_logs = []
        class LogCapturer(logging.Handler):
            def emit(self, record):
                captured_logs.append(self.format(record))
        handler = LogCapturer()
        cb_logger.addHandler(handler)

        try:
            with patch("bot.handlers.callbacks.delete_list_item", AsyncMock(wraps=delete_list_item)) as mock_delete:
                # Callback 1: foreign item against current list
                update_foreign, query_foreign = make_mock_update(
                    f"list:del:{ul_current.id}:{foreign_item.id}", user_id=1, chat_id=100
                )
                await handle_callback(update_foreign, MagicMock())

                # Callback 2: nonexistent item against current list
                update_nonexistent, query_nonexistent = make_mock_update(
                    f"list:del:{ul_current.id}:{nonexistent_item_id}", user_id=1, chat_id=100
                )
                await handle_callback(update_nonexistent, MagicMock())

            # Обидва повертають абсолютно однаковий query.answer text та show_alert
            self.assertEqual(query_foreign.answer.await_count, 1)
            self.assertEqual(query_nonexistent.answer.await_count, 1)
            f_args, f_kwargs = query_foreign.answer.call_args
            n_args, n_kwargs = query_nonexistent.answer.call_args
            self.assertEqual(f_args, n_args)
            self.assertEqual(f_kwargs, n_kwargs)
            self.assertEqual(f_args[0], "ℹ️ Пункт уже видалено або недоступний.")
            self.assertTrue(f_kwargs.get("show_alert"))

            # Обидва однаково refresh-ять message
            query_foreign.message.edit_text.assert_awaited_once()
            query_nonexistent.message.edit_text.assert_awaited_once()
            f_edit_args, _ = query_foreign.message.edit_text.call_args
            n_edit_args, _ = query_nonexistent.message.edit_text.call_args
            self.assertEqual(f_edit_args, n_edit_args)

            # delete_list_item не викликається в обох випадках
            mock_delete.assert_not_called()

            # foreign item залишається в DB
            db_foreign = await get_list_item(foreign_item.id, 200)
            self.assertIsNotNone(db_foreign)
            self.assertEqual(db_foreign.text, "Секретний чужий товар")

            # current target list не змінюється
            current_items_after = await list_list_items(ul_current.id, 100)
            self.assertEqual(len(current_items_before), len(current_items_after))
            self.assertEqual(current_items_before[0].id, current_items_after[0].id)

            # у response/log немає назви чи тексту foreign item або foreign list
            rendered_msg = str(f_edit_args[0])
            self.assertNotIn("Секретний чужий товар", rendered_msg)
            self.assertNotIn("Чужий секретний список", rendered_msg)
            joined_logs = "\n".join(captured_logs)
            self.assertNotIn("Секретний чужий товар", joined_logs)
            self.assertNotIn("Чужий секретний список", joined_logs)
        finally:
            cb_logger.removeHandler(handler)

    # 53. Cross-list regression: item другого списку разом з ID першого списку в тому самому чаті
    async def test_53_cross_list_delete_safe_and_indistinguishable(self):
        ul1, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 1")
        ul2, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Список 2")
        items2 = await add_list_items(ul2.id, 100, 1, ["Секретний товар 2"])
        item2 = items2[0]

        cb_logger = logging.getLogger("bot.handlers.callbacks")
        captured_logs = []
        class LogCapturer(logging.Handler):
            def emit(self, record):
                captured_logs.append(self.format(record))
        handler = LogCapturer()
        cb_logger.addHandler(handler)

        try:
            with patch("bot.handlers.callbacks.delete_list_item", AsyncMock(wraps=delete_list_item)) as mock_delete:
                update, query = make_mock_update(f"list:del:{ul1.id}:{item2.id}", user_id=1, chat_id=100)
                await handle_callback(update, MagicMock())

            query.answer.assert_awaited_with("ℹ️ Пункт уже видалено або недоступний.", show_alert=True)
            mock_delete.assert_not_called()

            # другий item залишається в DB
            db_item2 = await get_list_item(item2.id, 100)
            self.assertIsNotNone(db_item2)
            self.assertEqual(db_item2.list_id, ul2.id)

            # refresh behavior збігається: ul1 оновлюється
            query.message.edit_text.assert_awaited_once()
            args, _ = query.message.edit_text.call_args
            text = args[0]
            self.assertNotIn("Список 2", text)
            self.assertNotIn("Секретний товар 2", text)
            joined_logs = "\n".join(captured_logs)
            self.assertNotIn("Список 2", joined_logs)
            self.assertNotIn("Секретний товар 2", joined_logs)
        finally:
            cb_logger.removeHandler(handler)

    # 54. Done/undo indistinguishability: nonexistent, foreign-chat, cross-list items
    async def test_54_done_undo_indistinguishable(self):
        ul_current, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Поточний")
        ul_cross, _ = await create_or_get_user_list(1, 100, LIST_TYPE_SHOPPING, "Інший у чаті")
        ul_foreign, _ = await create_or_get_user_list(2, 200, LIST_TYPE_SHOPPING, "Чужий")

        cross_items = await add_list_items(ul_cross.id, 100, 1, ["Cross item"])
        cross_item = cross_items[0]
        foreign_items = await add_list_items(ul_foreign.id, 200, 2, ["Foreign item"])
        foreign_item = foreign_items[0]
        nonexistent_id = 888888

        for action in ("done", "undo"):
            scenarios = [
                ("nonexistent", nonexistent_id),
                ("foreign_chat", foreign_item.id),
                ("cross_list", cross_item.id),
            ]
            for name, target_item_id in scenarios:
                with self.subTest(action=action, scenario=name):
                    with patch("bot.handlers.callbacks.set_list_item_done", AsyncMock(wraps=set_list_item_done)) as mock_set_done:
                        update, query = make_mock_update(f"list:{action}:{ul_current.id}:{target_item_id}", user_id=1, chat_id=100)
                        await handle_callback(update, MagicMock())

                        query.answer.assert_awaited_with("❌ Список або пункт не знайдено в цьому чаті.", show_alert=True)
                        mock_set_done.assert_not_called()
                        query.message.edit_text.assert_not_awaited()
                        query.message.edit_reply_markup.assert_not_awaited()

        # Перевірка що cross-list та foreign items не змінились
        db_cross = await get_list_item(cross_item.id, 100)
        self.assertFalse(db_cross.is_done)
        db_foreign = await get_list_item(foreign_item.id, 200)
        self.assertFalse(db_foreign.is_done)


if __name__ == "__main__":
    unittest.main()
