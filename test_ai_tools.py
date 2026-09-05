import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta
import zoneinfo

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from bot.database.models import Base, Reminder, ActionDraft
from bot.ai.tools import (
    get_tool_definitions,
    get_openai_tools,
    execute_tool,
    ToolResult,
    get_active_reminders_summary,
    CALCULATE_DATE_SCHEMA,
    SCHEDULE_REMINDER_SCHEMA,
    DELETE_REMINDER_SCHEMA,
    WEB_SEARCH_SCHEMA
)
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.openrouter_provider import OpenRouterProvider
from bot.ai.google_provider import GoogleProvider
from bot.utils.scheduler import SchedulerService


class MockAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._iter = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class TestAITools(unittest.IsolatedAsyncioTestCase):
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

    # 1. Shared schema names and relaxed mutating schemas
    def test_shared_schema_names(self):
        """Verify shared schemas contain exact names and web_search only when allow_search=True."""
        # Raw tool definitions
        tools_no_search = get_tool_definitions(allow_search=False)
        names_no_search = [t["name"] for t in tools_no_search]
        expected_no_search = [
            "calculate_date",
            "schedule_reminder",
            "delete_reminder",
            "create_scheduled_tasks",
            "show_shopping_list",
            "add_shopping_items",
            "set_shopping_item_state",
            "delete_shopping_item",
            "clear_bought_items",
        ]
        self.assertEqual(names_no_search, expected_no_search)

        tools_search = get_tool_definitions(allow_search=True)
        names_search = [t["name"] for t in tools_search]
        self.assertEqual(names_search, expected_no_search + ["web_search"])

        # OpenAI wrapper
        openai_no_search = get_openai_tools(allow_search=False)
        self.assertEqual(len(openai_no_search), 9)
        for t in openai_no_search:
            self.assertEqual(t["type"], "function")
            self.assertIn(t["function"]["name"], expected_no_search)

        openai_search = get_openai_tools(allow_search=True)
        self.assertEqual(len(openai_search), 10)
        openai_names = [t["function"]["name"] for t in openai_search]
        self.assertEqual(openai_names, expected_no_search + ["web_search"])

        # Verify mutating schemas allow partial calls (required is empty)
        self.assertEqual(SCHEDULE_REMINDER_SCHEMA["parameters"].get("required", []), [])
        self.assertEqual(DELETE_REMINDER_SCHEMA["parameters"].get("required", []), [])
        # calculate_date and web_search retain required fields
        self.assertEqual(CALCULATE_DATE_SCHEMA["parameters"].get("required"), ["local_datetime"])
        self.assertEqual(WEB_SEARCH_SCHEMA["parameters"].get("required"), ["query"])

    # 2. calculate_date valid and invalid payload behavior
    async def test_calculate_date_behavior(self):
        """Verify calculate_date handles valid and invalid payloads properly."""
        # Valid input
        res = await execute_tool(
            "calculate_date",
            {"local_datetime": "2026-10-15 14:30:00"},
            timezone_name="UTC"
        )
        self.assertIsInstance(res, ToolResult)
        self.assertTrue(res.payload.get("success"))
        self.assertIn("2026-10-15T14:30:00", res.payload.get("iso_time_utc", ""))
        self.assertFalse(res.stop)

        # Invalid date format
        res_invalid = await execute_tool(
            "calculate_date",
            {"local_datetime": "not a real date"},
            timezone_name="UTC"
        )
        self.assertFalse(res_invalid.payload.get("success"))
        self.assertIn("error", res_invalid.payload)
        self.assertFalse(res_invalid.stop)

        # Empty string
        res_empty = await execute_tool("calculate_date", {"local_datetime": "   "})
        self.assertFalse(res_empty.payload.get("success"))
        self.assertIn("error", res_empty.payload)

        # Missing field
        res_missing = await execute_tool("calculate_date", {})
        self.assertFalse(res_missing.payload.get("success"))

    # 3. schedule_reminder direct mutation contract (execute_mutation=True)
    async def test_schedule_reminder_contract(self):
        """Verify direct schedule_reminder execution when execute_mutation=True."""
        future_dt = datetime.now(timezone.utc) + timedelta(hours=3)
        future_iso = future_dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # Trailing Z

        mock_add = AsyncMock(return_value=789)
        with patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            res = await execute_tool(
                "schedule_reminder",
                {
                    "iso_time_utc": future_iso,
                    "text": "Call Dr. <Smith> & check 100% status"
                },
                user_id=123,
                chat_id=456,
                timezone_name="Europe/Kyiv",
                execute_mutation=True,
            )

            # Verification of call and argument order: user_id, chat_id, text, aware UTC datetime
            mock_add.assert_awaited_once()
            called_user, called_chat, called_text, called_dt = mock_add.call_args[0]
            self.assertEqual(called_user, 123)
            self.assertEqual(called_chat, 456)
            self.assertEqual(called_text, "Call Dr. <Smith> & check 100% status")
            self.assertIsNotNone(called_dt.tzinfo)
            self.assertEqual(called_dt.utcoffset().total_seconds(), 0)

            # Verification of ToolResult shape
            self.assertTrue(res.payload.get("success"))
            self.assertEqual(res.payload.get("reminder_id"), 789)
            self.assertTrue(res.stop)

            # HTML escaping in display_text
            self.assertIsNotNone(res.display_text)
            self.assertIn("Dr. &lt;Smith&gt; &amp; check 100% status", res.display_text)
            self.assertNotIn("<Smith>", res.display_text)

    async def test_schedule_reminder_validation_rejects_invalid_inputs(self):
        """Verify invalid schedule_reminder inputs do not call scheduler_service under execute_mutation=True."""
        mock_add = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            # Past datetime
            past_iso = "2020-01-01T12:00:00+00:00"
            res_past = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": past_iso, "text": "past task"},
                user_id=1, chat_id=2,
                execute_mutation=True,
            )
            self.assertFalse(res_past.payload.get("success"))

            # Naive datetime (no timezone)
            naive_iso = "2027-01-01T12:00:00"
            res_naive = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": naive_iso, "text": "naive task"},
                user_id=1, chat_id=2,
                execute_mutation=True,
            )
            self.assertFalse(res_naive.payload.get("success"))

            # Malformed datetime
            res_malformed = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": "garbage", "text": "bad task"},
                user_id=1, chat_id=2,
                execute_mutation=True,
            )
            self.assertFalse(res_malformed.payload.get("success"))

            # Empty text
            future_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            res_no_text = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": future_iso, "text": "  "},
                user_id=1, chat_id=2,
                execute_mutation=True,
            )
            self.assertFalse(res_no_text.payload.get("success"))

            # Missing chat_id / user_id
            res_no_chat = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": future_iso, "text": "valid text"},
                user_id=None, chat_id=None,
                execute_mutation=True,
            )
            self.assertFalse(res_no_chat.payload.get("success"))

            mock_add.assert_not_called()

    # 4. delete_reminder direct mutation (execute_mutation=True)
    async def test_delete_reminder_scoped_to_chat(self):
        """Verify direct delete_reminder execution deletes from scheduler when execute_mutation=True."""
        scheduler = SchedulerService()
        scheduler.scheduler.remove_job = MagicMock()

        with patch("bot.utils.scheduler.AsyncSessionLocal", self.SessionLocal), \
             patch("bot.ai.tools.scheduler_service", scheduler):

            # Add two reminders in different chats
            async with self.SessionLocal() as session:
                rem_chat1 = Reminder(id=10, user_id=1, chat_id=100, text="Chat 1 task", trigger_time=datetime.now(timezone.utc))
                rem_chat2 = Reminder(id=20, user_id=2, chat_id=200, text="Chat 2 task", trigger_time=datetime.now(timezone.utc))
                session.add_all([rem_chat1, rem_chat2])
                await session.commit()

            # Attempt to delete Chat 2's reminder while in Chat 1 (cross-chat deletion)
            res_cross = await execute_tool("delete_reminder", {"reminder_id": 20}, chat_id=100, execute_mutation=True)
            self.assertFalse(res_cross.payload.get("success"))
            self.assertEqual(res_cross.payload.get("error"), "not_found")
            self.assertFalse(res_cross.stop)
            scheduler.scheduler.remove_job.assert_not_called()

            # Verify reminder 20 still exists in DB
            async with self.SessionLocal() as session:
                rem2 = await session.get(Reminder, 20)
                self.assertIsNotNone(rem2)

            # Authorized deletion in Chat 2
            res_auth = await execute_tool("delete_reminder", {"reminder_id": 20}, chat_id=200, execute_mutation=True)
            self.assertTrue(res_auth.payload.get("success"))
            self.assertFalse(res_auth.stop)
            scheduler.scheduler.remove_job.assert_called_once_with("20")

            # Verify reminder 20 was deleted from DB
            async with self.SessionLocal() as session:
                rem2_after = await session.get(Reminder, 20)
                self.assertIsNone(rem2_after)

    async def test_delete_reminder_validation(self):
        """Verify positive integer validation occurs before calling service under execute_mutation=True."""
        mock_delete = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.delete_reminder_by_id", mock_delete):
            for bad_id in [-5, 0, "not_int", True, False, None]:
                res = await execute_tool("delete_reminder", {"reminder_id": bad_id}, chat_id=123, execute_mutation=True)
                self.assertFalse(res.payload.get("success"))

            mock_delete.assert_not_called()

    # 5. web_search
    async def test_web_search_behavior(self):
        """Verify web_search validation, capping at 5 links, and ToolResult formatting."""
        mock_search = AsyncMock(return_value="LINK: https://example.com/1\nLINK: https://example.com/2\nLINK: https://example.com/3\nLINK: https://example.com/4\nLINK: https://example.com/5\nLINK: https://example.com/6")
        with patch("bot.ai.tools.perform_search", mock_search):
            res_empty = await execute_tool("web_search", {"query": "  "})
            self.assertFalse(res_empty.payload.get("success"))
            self.assertIn("error", res_empty.payload)

            res = await execute_tool("web_search", {"query": "python async"})
            self.assertIn("results", res.payload)
            self.assertEqual(res.display_text, "\n🔎 <i>Шукаю...</i>\n")
            self.assertFalse(res.stop)
            self.assertEqual(len(res.source_urls), 5)
            self.assertNotIn("https://example.com/6", res.source_urls)

    # 6. Unknown tool
    async def test_unknown_tool_name(self):
        """Verify unknown tool returns safe error without side effects."""
        res = await execute_tool("nonexistent_tool", {"foo": "bar"})
        self.assertFalse(res.payload.get("success"))
        self.assertIn("Unknown tool", res.payload.get("error", ""))
        self.assertFalse(res.stop)

    # 7. OpenAIProvider uses shared executor and propagates settings
    async def test_openai_provider_uses_shared_executor(self):
        """Verify OpenAIProvider routes tool calls through execute_tool and propagates source_message_id."""
        provider = OpenAIProvider(api_key="test-key")

        tc_mock = MagicMock()
        tc_mock.index = 0
        tc_mock.id = "call_abc"
        tc_mock.function.name = "calculate_date"
        tc_mock.function.arguments = '{"local_datetime": "2026-09-04 12:00:00"}'

        stream1 = MockAsyncStream([MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc_mock], content=None))])])
        stream2 = MockAsyncStream([MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=None, content="Calculated date response"))])])
        provider.client.chat.completions.create = AsyncMock(side_effect=[stream1, stream2])

        fake_res = ToolResult(payload={"success": True, "iso_time_utc": "2026-09-04T12:00:00+00:00"})
        with patch("bot.ai.openai_provider.execute_tool", AsyncMock(return_value=fake_res)) as mock_exec:
            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "schedule something"}],
                settings={"user_id": 11, "chat_id": 22, "timezone": "UTC", "source_message_id": 888}
            ):
                chunks.append(chunk)

            self.assertIn("Calculated date response", "".join(chunks))
            mock_exec.assert_awaited_once_with(
                "calculate_date",
                {"local_datetime": "2026-09-04 12:00:00"},
                user_id=11,
                chat_id=22,
                timezone_name="UTC",
                source_message_id=888,
            )

    # 8. OpenRouterProvider default draft mode for schedule_reminder
    async def test_openrouter_schedule_reminder_creates_draft_and_stops(self):
        """Verify OpenRouterProvider invokes schedule_reminder in default draft mode without adding reminder."""
        provider = OpenRouterProvider(api_key="test-key")

        tc_mock = MagicMock()
        tc_mock.index = 0
        tc_mock.id = "call_or_1"
        tc_mock.function.name = "schedule_reminder"
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        tc_mock.function.arguments = f'{{"iso_time_utc": "{future_iso}", "text": "OpenRouter test"}}'

        stream1 = MockAsyncStream([MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc_mock], content=None))])])
        provider.client.chat.completions.create = AsyncMock(return_value=stream1)

        mock_add = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            chunks = []
            settings = {"user_id": 555, "chat_id": 666, "timezone": "Europe/Kyiv", "source_message_id": 1234}
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "remind me"}],
                settings=settings
            ):
                chunks.append(chunk)

            # Scheduler must NOT be called in default mode
            mock_add.assert_not_called()

            # Draft ID must be recorded in settings
            self.assertIn("_action_draft_id", settings)
            self.assertIsInstance(settings["_action_draft_id"], int)

            # Preview displayed, no "Встановлено"
            full_out = "".join(chunks)
            self.assertIn("Підтвердження нагадування:", full_out)
            self.assertIn("OpenRouter test", full_out)
            self.assertNotIn("Встановлено:", full_out)

    # 9. OpenRouterProvider default draft mode for delete_reminder
    async def test_openrouter_delete_reminder_creates_draft_and_stops(self):
        """Verify OpenRouterProvider creates delete draft and does not call delete_reminder_by_id directly."""
        provider = OpenRouterProvider(api_key="test-key")

        tc_mock = MagicMock()
        tc_mock.index = 0
        tc_mock.id = "call_or_2"
        tc_mock.function.name = "delete_reminder"
        tc_mock.function.arguments = '{"reminder_id": 88}'

        stream1 = MockAsyncStream([MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc_mock], content=None))])])
        provider.client.chat.completions.create = AsyncMock(return_value=stream1)

        mock_del = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.delete_reminder_by_id", mock_del):
            chunks = []
            settings = {"user_id": 555, "chat_id": 777}
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "delete reminder 88"}],
                settings=settings
            ):
                chunks.append(chunk)

            # Scheduler must NOT be called
            mock_del.assert_not_called()

            # Draft ID recorded and preview yielded
            self.assertIn("_action_draft_id", settings)
            self.assertIn("Підтвердження видалення:", "".join(chunks))

    # 10. GoogleProvider tool declarations match get_tool_definitions
    def test_google_provider_tool_declarations(self):
        """Verify GoogleProvider tool declarations have exactly the same names as get_tool_definitions."""
        provider = GoogleProvider(api_key="test-key")

        expected_no_search = [
            "calculate_date",
            "schedule_reminder",
            "delete_reminder",
            "create_scheduled_tasks",
            "show_shopping_list",
            "add_shopping_items",
            "set_shopping_item_state",
            "delete_shopping_item",
            "clear_bought_items",
        ]
        proto_no_search = provider._get_tools_proto(allow_search=False)
        self.assertEqual(len(proto_no_search.function_declarations), 9)
        decl_names_no_search = [d.name for d in proto_no_search.function_declarations]
        self.assertEqual(decl_names_no_search, expected_no_search)

        proto_search = provider._get_tools_proto(allow_search=True)
        self.assertEqual(len(proto_search.function_declarations), 10)
        decl_names_search = [d.name for d in proto_search.function_declarations]
        self.assertEqual(decl_names_search, expected_no_search + ["web_search"])

    # 11. GoogleProvider routes calculate_date through execute_tool
    async def test_google_provider_routes_calculate_date(self):
        """Verify GoogleProvider routes calculate_date through execute_tool with correct arguments."""
        provider = GoogleProvider(api_key="test-key")

        fn_part = MagicMock()
        fn_part.name = "calculate_date"
        fn_part.args = {"local_datetime": "2026-09-04 15:00:00"}
        chunk1 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)
        chunk2 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=None)]))], text="Done calculating")

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(side_effect=[MockAsyncStream([chunk1]), MockAsyncStream([chunk2])])

        fake_res = ToolResult(payload={"success": True, "iso_time_utc": "2026-09-04T12:00:00+00:00"}, stop=False)
        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.google_provider.execute_tool", AsyncMock(return_value=fake_res)) as mock_exec:
            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "calc"}],
                settings={"user_id": 10, "chat_id": 20, "timezone": "Europe/Kyiv"}
            ):
                chunks.append(chunk)

            self.assertIn("Done calculating", "".join(chunks))
            mock_exec.assert_awaited_once_with(
                "calculate_date",
                {"local_datetime": "2026-09-04 15:00:00"},
                user_id=10,
                chat_id=20,
                timezone_name="Europe/Kyiv",
                source_message_id=None,
            )

    # 12. GoogleProvider non-stopping tool returns FunctionResponse
    async def test_google_provider_non_stopping_tool_result(self):
        """Verify non-stopping tool returns FunctionResponse, triggers second response, and yields final text."""
        provider = GoogleProvider(api_key="test-key")

        fn_part = MagicMock()
        fn_part.name = "calculate_date"
        fn_part.args = {"local_datetime": "tomorrow 10:00"}
        chunk1 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)
        chunk2 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=None)]))], text="Calculated successfully!")

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(side_effect=[MockAsyncStream([chunk1]), MockAsyncStream([chunk2])])

        expected_payload = {"success": True, "iso_time_utc": "2026-09-05T07:00:00Z"}
        fake_res = ToolResult(payload=expected_payload, stop=False)

        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.google_provider.execute_tool", AsyncMock(return_value=fake_res)):
            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "test"}],
                settings={"user_id": 1, "chat_id": 2}
            ):
                chunks.append(chunk)

            self.assertIn("Calculated successfully!", "".join(chunks))
            self.assertEqual(mock_chat.send_message_async.call_count, 2)

    # 13. Stopping schedule_reminder in GoogleProvider creates draft and stops
    async def test_google_provider_stopping_schedule_reminder(self):
        """Verify stopping schedule_reminder creates draft, sets _action_draft_id, and stops."""
        provider = GoogleProvider(api_key="test-key")

        fn_part = MagicMock()
        fn_part.name = "schedule_reminder"
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        fn_part.args = {"iso_time_utc": future_iso, "text": "Google reminder"}
        chunk1 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([chunk1]))

        mock_add = AsyncMock()
        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            chunks = []
            settings = {"user_id": 11, "chat_id": 22, "timezone": "Europe/Kyiv"}
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "remind me"}],
                settings=settings
            ):
                chunks.append(chunk)

            # Direct scheduler is NOT called
            mock_add.assert_not_called()
            # Draft ID recorded in settings
            self.assertIn("_action_draft_id", settings)

            full_reply = "".join(chunks)
            self.assertIn("Підтвердження нагадування:", full_reply)
            self.assertIn("Google reminder", full_reply)
            self.assertEqual(mock_chat.send_message_async.call_count, 1)

    # 14. Google web_search routes through execute_tool and appends sources
    async def test_google_provider_web_search(self):
        """Verify Google web_search routes through execute_tool, continues to final text, and appends sources."""
        provider = GoogleProvider(api_key="test-key")

        fn_part = MagicMock()
        fn_part.name = "web_search"
        fn_part.args = {"query": "latest news"}
        chunk1 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)
        chunk2 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=None)]))], text="Here is the news.")

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(side_effect=[MockAsyncStream([chunk1]), MockAsyncStream([chunk2])])

        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.tools.perform_search", AsyncMock(return_value="LINK: https://news.com/1")):
            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "news"}],
                settings={"user_id": 1, "chat_id": 2}
            ):
                chunks.append(chunk)

            full_out = "".join(chunks)
            self.assertIn("Шукаю...", full_out)
            self.assertIn("Here is the news.", full_out)
            self.assertIn("Джерела:", full_out)
            self.assertIn("https://news.com/1", full_out)

    # 15. Google delete_reminder routes through execute_tool in draft mode
    async def test_google_provider_delete_reminder_routes_through_execute_tool(self):
        """Verify Google delete_reminder creates draft and does not call SchedulerService directly."""
        provider = GoogleProvider(api_key="test-key")

        fn_part = MagicMock()
        fn_part.name = "delete_reminder"
        fn_part.args = {"reminder_id": 99}
        chunk1 = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([chunk1]))

        mock_del = AsyncMock()
        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.tools.scheduler_service.delete_reminder_by_id", mock_del):
            chunks = []
            settings = {"user_id": 1, "chat_id": 444}
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "delete 99"}],
                settings=settings
            ):
                chunks.append(chunk)

            mock_del.assert_not_called()
            self.assertIn("_action_draft_id", settings)
            self.assertIn("Підтвердження видалення:", "".join(chunks))
            self.assertEqual(mock_chat.send_message_async.call_count, 1)

    # 16. disable_tools=True creates no Tool declaration and cannot invoke execute_tool
    async def test_google_provider_disable_tools(self):
        """Verify disable_tools=True creates no Tool declaration and cannot invoke execute_tool."""
        provider = GoogleProvider(api_key="test-key")
        proto = provider._get_tools_proto(allow_search=True)
        self.assertIsNotNone(proto)

        with patch("google.generativeai.GenerativeModel") as mock_model_cls, \
             patch("bot.ai.google_provider.execute_tool") as mock_exec:
            mock_model = MagicMock()
            mock_chat = MagicMock()
            mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([MagicMock(candidates=[], text="Plain text")]))
            mock_model.start_chat.return_value = mock_chat
            mock_model_cls.return_value = mock_model

            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "hello"}],
                settings={"disable_tools": True}
            ):
                chunks.append(chunk)

            mock_model_cls.assert_called_once()
            call_kwargs = mock_model_cls.call_args[1]
            self.assertIsNone(call_kwargs["tools"])
            mock_exec.assert_not_called()

    # 17. ToolResult.source_urls is immutable tuple
    async def test_tool_result_source_urls_immutable_tuple(self):
        """Verify ToolResult source_urls default is immutable and search returns a tuple."""
        res_default = ToolResult(payload={"ok": True})
        self.assertIsInstance(res_default.source_urls, tuple)
        self.assertEqual(res_default.source_urls, ())

        with patch("bot.ai.tools.perform_search", AsyncMock(return_value="LINK: https://a.com\nLINK: https://b.com")):
            res_search = await execute_tool("web_search", {"query": "python"})
            self.assertIsInstance(res_search.source_urls, tuple)
            self.assertEqual(res_search.source_urls, ("https://a.com", "https://b.com"))

    # 18. Unexpected add_reminder exceptions are logged under execute_mutation=True
    async def test_schedule_reminder_unexpected_exception_logged_and_sanitized(self):
        """Verify unexpected add_reminder exceptions are logged, return reminder_create_failed, and hide raw error."""
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        secret_error = "FATAL: password 'super_secret' in /private/db_dump.sql"

        with patch("bot.ai.tools.scheduler_service.add_reminder", AsyncMock(side_effect=RuntimeError(secret_error))), \
             self.assertLogs("bot.ai.tools", level="ERROR") as cm:
            res = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": future_iso, "text": "test task"},
                user_id=1, chat_id=2,
                execute_mutation=True,
            )

            self.assertFalse(res.payload.get("success"))
            self.assertEqual(res.payload.get("error"), "reminder_create_failed")
            self.assertNotIn(secret_error, str(res.payload))
            self.assertTrue(any(secret_error in log_msg for log_msg in cm.output))

    # --- C2.1 Specific Tests ---

    # 19. calculate_date and web_search remain immediate and create no ActionDraft
    async def test_read_only_tools_create_no_draft(self):
        """Verify read-only tools calculate_date and web_search create no ActionDraft."""
        res_calc = await execute_tool("calculate_date", {"local_datetime": "2026-12-01 10:00:00"}, timezone_name="UTC")
        self.assertIsNone(res_calc.draft_id)

        with patch("bot.ai.tools.perform_search", AsyncMock(return_value="search info")):
            res_search = await execute_tool("web_search", {"query": "python"})
            self.assertIsNone(res_search.draft_id)

        async with self.SessionLocal() as session:
            count = (await session.execute(select(ActionDraft))).scalars().all()
            self.assertEqual(len(count), 0)

    # 20. schedule_reminder default complete creates pending_confirmation ActionDraft without scheduler
    async def test_schedule_reminder_default_complete_creates_pending_draft(self):
        """Verify default complete schedule_reminder creates pending_confirmation ActionDraft without calling scheduler."""
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        mock_add = AsyncMock()

        with patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            res = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": future_iso, "text": "Buy groceries"},
                user_id=42,
                chat_id=142,
                source_message_id=999,
                timezone_name="UTC",
            )

            mock_add.assert_not_called()
            self.assertTrue(res.stop)
            self.assertIsNotNone(res.draft_id)
            self.assertEqual(res.payload["status"], "pending_confirmation")
            self.assertEqual(res.payload["missing_fields"], [])

            self.assertIn("Підтвердження нагадування:", res.display_text)
            self.assertIn("Buy groceries", res.display_text)
            self.assertNotIn("Встановлено", res.display_text)

            async with self.SessionLocal() as session:
                draft = await session.get(ActionDraft, res.draft_id)
                self.assertIsNotNone(draft)
                self.assertEqual(draft.status, "pending_confirmation")
                self.assertEqual(draft.source_message_id, 999)
                self.assertEqual(draft.payload["text"], "Buy groceries")

    # 21. schedule_reminder default incomplete creates awaiting_info and asks only the first question
    async def test_schedule_reminder_default_incomplete_asks_one_question(self):
        """Verify incomplete schedule_reminder creates awaiting_info and asks exactly one question."""
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()

        # Case A: Missing text
        res_no_text = await execute_tool(
            "schedule_reminder",
            {"iso_time_utc": future_iso},
            user_id=1, chat_id=2
        )
        self.assertEqual(res_no_text.payload["status"], "awaiting_info")
        self.assertEqual(res_no_text.payload["missing_fields"], ["text"])
        self.assertEqual(res_no_text.display_text, "❓ Що саме вам нагадати?")

        # Case B: Missing/invalid time
        res_bad_time = await execute_tool(
            "schedule_reminder",
            {"text": "Gym", "iso_time_utc": "invalid_iso"},
            user_id=1, chat_id=2
        )
        self.assertEqual(res_bad_time.payload["status"], "awaiting_info")
        self.assertEqual(res_bad_time.payload["missing_fields"], ["iso_time_utc"])
        self.assertEqual(res_bad_time.display_text, "❓ На коли встановити нагадування?")

        # Case C: Both missing -> asks only the first (text)
        res_empty = await execute_tool(
            "schedule_reminder",
            {},
            user_id=1, chat_id=2
        )
        self.assertEqual(res_empty.payload["status"], "awaiting_info")
        self.assertEqual(res_empty.payload["missing_fields"], ["text", "iso_time_utc"])
        self.assertEqual(res_empty.display_text, "❓ Що саме вам нагадати?")

    # 22. schedule_reminder rejects past/naive datetime from payload
    async def test_schedule_reminder_invalid_time_not_persisted_in_payload(self):
        """Verify naive or past schedule time is not persisted as valid in payload and marks iso_time_utc missing."""
        past_iso = "2020-01-01T10:00:00Z"
        res = await execute_tool(
            "schedule_reminder",
            {"text": "Past task", "iso_time_utc": past_iso},
            user_id=5, chat_id=6
        )
        self.assertEqual(res.payload["status"], "awaiting_info")
        self.assertIn("iso_time_utc", res.payload["missing_fields"])

        async with self.SessionLocal() as session:
            draft = await session.get(ActionDraft, res.draft_id)
            self.assertNotIn("iso_time_utc", draft.payload)
            self.assertEqual(draft.payload.get("text"), "Past task")

    # 23. delete_reminder default valid creates pending draft without deleting
    async def test_delete_reminder_default_valid_creates_pending_draft(self):
        """Verify valid delete_reminder creates pending_confirmation ActionDraft without calling scheduler."""
        mock_del = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.delete_reminder_by_id", mock_del):
            res = await execute_tool("delete_reminder", {"reminder_id": 55}, user_id=10, chat_id=20)
            mock_del.assert_not_called()
            self.assertTrue(res.stop)
            self.assertIsNotNone(res.draft_id)
            self.assertEqual(res.payload["status"], "pending_confirmation")
            self.assertIn("Підтвердження видалення:", res.display_text)
            self.assertIn("#55", res.display_text)

    # 24. delete_reminder default missing/invalid creates awaiting_info draft
    async def test_delete_reminder_default_invalid_creates_awaiting_info(self):
        """Verify missing or invalid reminder_id creates awaiting_info ActionDraft asking which reminder to delete."""
        mock_del = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.delete_reminder_by_id", mock_del):
            res = await execute_tool("delete_reminder", {}, user_id=10, chat_id=20)
            mock_del.assert_not_called()
            self.assertEqual(res.payload["status"], "awaiting_info")
            self.assertEqual(res.payload["missing_fields"], ["reminder_id"])
            self.assertIn("Яке саме нагадування ви хочете видалити?", res.display_text)

    # 25. Model-supplied execute_mutation inside args cannot bypass draft creation
    async def test_model_supplied_execute_mutation_cannot_bypass_draft(self):
        """Verify model JSON containing execute_mutation=True is ignored and still creates ActionDraft."""
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        mock_add = AsyncMock()

        with patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            res = await execute_tool(
                "schedule_reminder",
                {"iso_time_utc": future_iso, "text": "Hacker attempt", "execute_mutation": True},
                user_id=1,
                chat_id=2,
            )
            mock_add.assert_not_called()
            self.assertIsNotNone(res.draft_id)
            self.assertEqual(res.payload["status"], "pending_confirmation")

    # 26. OpenAIProvider records draft_id and propagates source_message_id
    async def test_openai_provider_records_draft_id_in_settings(self):
        """Verify OpenAIProvider stores ToolResult.draft_id in settings['_action_draft_id']."""
        provider = OpenAIProvider(api_key="test-key")

        tc_mock = MagicMock()
        tc_mock.index = 0
        tc_mock.id = "call_draft"
        tc_mock.function.name = "schedule_reminder"
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        tc_mock.function.arguments = f'{{"iso_time_utc": "{future_iso}", "text": "OpenAI Draft"}}'

        stream1 = MockAsyncStream([MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc_mock], content=None))])])
        provider.client.chat.completions.create = AsyncMock(return_value=stream1)

        mock_add = AsyncMock()
        with patch("bot.ai.tools.scheduler_service.add_reminder", mock_add):
            chunks = []
            settings = {"user_id": 99, "chat_id": 199, "source_message_id": 54321}
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "schedule"}],
                settings=settings
            ):
                chunks.append(chunk)

            mock_add.assert_not_called()
            self.assertIn("_action_draft_id", settings)
            self.assertIn("Підтвердження нагадування:", "".join(chunks))


if __name__ == "__main__":
    unittest.main()
