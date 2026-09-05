import os
import sys
import unittest
import asyncio
import logging
import html
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select

from bot.database.models import Base, ActionDraft, ScheduledTask, TaskOccurrence, Reminder
from bot.ai.tools import (
    get_tool_definitions,
    get_openai_tools,
    execute_tool,
    apply_action_draft_reply,
    format_draft_preview_or_question,
    ToolResult,
    CREATE_SCHEDULED_TASKS_SCHEMA,
    SCHEDULE_REMINDER_SCHEMA,
    DELETE_REMINDER_SCHEMA,
)
from bot.ai.google_provider import GoogleProvider, _json_schema_to_google_schema
from google.ai.generativelanguage import Type
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.openrouter_provider import OpenRouterProvider
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    confirm_action_draft,
    cancel_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_CONFIRMED,
)
from bot.utils.scheduled_tasks import (
    create_scheduled_tasks_batch,
    create_scheduled_task,
    get_scheduled_task,
)
from bot.handlers.callbacks import handle_callback


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


def make_mock_callback_update(callback_data: str, user_id: int = 123, chat_id: int = 456):
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 999
    query.message.text = "Existing message"
    query.message.reply_text = AsyncMock()
    query.message.edit_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=user_id, first_name="TestUser")
    update.effective_chat = MagicMock(id=chat_id, type="private")
    return update, query


class TestScheduledAction(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.scheduled_tasks.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.scheduler.AsyncSessionLocal", self.SessionLocal),
            patch("bot.ai.tools.scheduler_service.add_reminder", new_callable=AsyncMock),
        ]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        await self.engine.dispose()

    # 1. Shared schemas include create_scheduled_tasks with and without web search
    def test_shared_schemas_include_create_scheduled_tasks(self):
        tools_no_search = get_tool_definitions(allow_search=False)
        names_no = [t["name"] for t in tools_no_search]
        self.assertIn("create_scheduled_tasks", names_no)

        tools_search = get_tool_definitions(allow_search=True)
        names_search = [t["name"] for t in tools_search]
        self.assertIn("create_scheduled_tasks", names_search)
        self.assertIn("web_search", names_search)

    # 2. OpenAI and OpenRouter receive the same array/nested-object schema
    def test_openai_openrouter_schema_parity(self):
        tools = get_openai_tools(allow_search=False)
        st_tools = [t for t in tools if t["function"]["name"] == "create_scheduled_tasks"]
        self.assertEqual(len(st_tools), 1)
        fn = st_tools[0]["function"]
        params = fn["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertIn("context_type", params["properties"])
        self.assertEqual(params["properties"]["context_type"].get("enum"), ["medication", "generic"])
        self.assertNotIn("context_type", params.get("required", []))
        self.assertIn("items", params["properties"])
        items_prop = params["properties"]["items"]
        self.assertEqual(items_prop["type"], "array")
        item_obj = items_prop["items"]
        self.assertEqual(item_obj["type"], "object")
        for f in ["name", "details", "dosage", "local_time", "days_of_week", "relative_to", "offset_minutes", "reference_time"]:
            self.assertIn(f, item_obj["properties"])
        self.assertEqual(item_obj["properties"]["days_of_week"]["type"], "array")
        self.assertEqual(item_obj["properties"]["days_of_week"]["items"]["type"], "integer")

    # 3. Google converter correctly maps items and days_of_week arrays and preserves enum
    def test_google_converter_array_mapping(self):
        schema = _json_schema_to_google_schema(CREATE_SCHEDULED_TASKS_SCHEMA["parameters"])
        self.assertEqual(schema.type_, Type.OBJECT)
        context_schema = schema.properties["context_type"]
        self.assertEqual(context_schema.type_, Type.STRING)
        self.assertEqual(list(context_schema.enum), ["medication", "generic"])
        items_schema = schema.properties["items"]
        self.assertEqual(items_schema.type_, Type.ARRAY)
        item_obj_schema = items_schema.items
        self.assertEqual(item_obj_schema.type_, Type.OBJECT)
        days_schema = item_obj_schema.properties["days_of_week"]
        self.assertEqual(days_schema.type_, Type.ARRAY)
        self.assertEqual(days_schema.items.type_, Type.INTEGER)

    # 4. disable_tools=True exposes no recurring action for all providers
    async def test_disable_tools_exposes_no_action(self):
        # Google
        gp = GoogleProvider(api_key="test")
        with patch("google.generativeai.GenerativeModel") as mock_gm:
            mock_chat = MagicMock()
            mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([]))
            mock_gm.return_value.start_chat.return_value = mock_chat
            async for _ in gp.generate_stream([{"role": "user", "content": "hi"}], {"disable_tools": True}):
                pass
            call_kwargs = mock_gm.call_args[1]
            self.assertIsNone(call_kwargs["tools"])

        # OpenAI
        op = OpenAIProvider(api_key="test")
        with patch.object(op.client.chat.completions, "create", AsyncMock(return_value=MockAsyncStream([]))) as mock_create:
            async for _ in op.generate_stream([{"role": "user", "content": "hi"}], {"disable_tools": True}):
                pass
            call_kwargs = mock_create.call_args[1]
            self.assertIsNone(call_kwargs.get("tools"))

        # OpenRouter
        orp = OpenRouterProvider(api_key="test")
        with patch.object(orp.client.chat.completions, "create", AsyncMock(return_value=MockAsyncStream([]))) as mock_or_create:
            async for _ in orp.generate_stream([{"role": "user", "content": "hi"}], {"disable_tools": True}):
                pass
            call_kwargs = mock_or_create.call_args[1]
            self.assertIsNone(call_kwargs.get("tools"))

    # 5. All three providers route the action through shared execute_tool
    async def test_all_providers_route_action_through_execute_tool(self):
        fake_res = ToolResult(payload={"success": True}, display_text="Draft created", stop=True, draft_id=101)

        # Google
        gp = GoogleProvider(api_key="test")
        fn_part = MagicMock()
        fn_part.name = "create_scheduled_tasks"
        fn_part.args = {"context_type": "generic", "items": [{"name": "Task A"}]}
        chunk = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)
        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([chunk]))
        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.google_provider.execute_tool", AsyncMock(return_value=fake_res)) as mock_g_exec:
            async for _ in gp.generate_stream([{"role": "user", "content": "schedule"}], {"user_id": 1, "chat_id": 2}):
                pass
            mock_g_exec.assert_awaited_once()
            self.assertEqual(mock_g_exec.call_args[0][0], "create_scheduled_tasks")

        # OpenAI
        op = OpenAIProvider(api_key="test")
        tc = MagicMock(index=0, id="call_1")
        tc.function.name = "create_scheduled_tasks"
        tc.function.arguments = '{"context_type": "generic", "items": [{"name": "Task A"}]}'
        o_chunk = MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc], content=None))])
        with patch.object(op.client.chat.completions, "create", AsyncMock(return_value=MockAsyncStream([o_chunk]))), \
             patch("bot.ai.openai_provider.execute_tool", AsyncMock(return_value=fake_res)) as mock_o_exec:
            async for _ in op.generate_stream([{"role": "user", "content": "schedule"}], {"user_id": 1, "chat_id": 2}):
                pass
            mock_o_exec.assert_awaited_once()
            self.assertEqual(mock_o_exec.call_args[0][0], "create_scheduled_tasks")

        # OpenRouter
        orp = OpenRouterProvider(api_key="test")
        tc_or = MagicMock(index=0, id="call_2")
        tc_or.function.name = "create_scheduled_tasks"
        tc_or.function.arguments = '{"context_type": "generic", "items": [{"name": "Task A"}]}'
        or_chunk = MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc_or], content=None))])
        with patch.object(orp.client.chat.completions, "create", AsyncMock(return_value=MockAsyncStream([or_chunk]))), \
             patch("bot.ai.openrouter_provider.execute_tool", AsyncMock(return_value=fake_res)) as mock_or_exec:
            async for _ in orp.generate_stream([{"role": "user", "content": "schedule"}], {"user_id": 1, "chat_id": 2}):
                pass
            mock_or_exec.assert_awaited_once()
            self.assertEqual(mock_or_exec.call_args[0][0], "create_scheduled_tasks")

    # 6. Provider logs contain tool name but not sentinel medication name, dosage, details, or full args
    async def test_provider_logs_contain_tool_name_only(self):
        sentinel_name = "SYNTHETIC_MED_X7"
        sentinel_dosage = "500_MG_DOSE"
        sentinel_details = "SECRET_DETAILS"

        fake_res = ToolResult(payload={"success": True}, stop=True, draft_id=1)

        # Test GoogleProvider
        gp = GoogleProvider(api_key="test")
        fn_part = MagicMock()
        fn_part.name = "create_scheduled_tasks"
        fn_part.args = {"context_type": "medication", "items": [{"name": sentinel_name, "dosage": sentinel_dosage, "details": sentinel_details}]}
        chunk = MagicMock(candidates=[MagicMock(content=MagicMock(parts=[MagicMock(function_call=fn_part)]))], text=None)
        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([chunk]))

        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.google_provider.execute_tool", AsyncMock(return_value=fake_res)), \
             self.assertLogs("bot.ai.google_provider", level="INFO") as cm_g:
            async for _ in gp.generate_stream([{"role": "user", "content": "test"}], {"user_id": 1, "chat_id": 2}):
                pass
            g_output = " ".join(cm_g.output)
            self.assertIn("create_scheduled_tasks", g_output)
            self.assertNotIn(sentinel_name, g_output)
            self.assertNotIn(sentinel_dosage, g_output)
            self.assertNotIn(sentinel_details, g_output)

        # Test OpenAIProvider
        op = OpenAIProvider(api_key="test")
        tc = MagicMock(index=0, id="tc_1")
        tc.function.name = "create_scheduled_tasks"
        tc.function.arguments = f'{{"items": [{{"name": "{sentinel_name}", "dosage": "{sentinel_dosage}"}}]}}'
        o_chunk = MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc], content=None))])

        with patch.object(op.client.chat.completions, "create", AsyncMock(return_value=MockAsyncStream([o_chunk]))), \
             patch("bot.ai.openai_provider.execute_tool", AsyncMock(return_value=fake_res)), \
             self.assertLogs("bot.ai.openai_provider", level="INFO") as cm_o:
            async for _ in op.generate_stream([{"role": "user", "content": "test"}], {"user_id": 1, "chat_id": 2}):
                pass
            o_output = " ".join(cm_o.output)
            self.assertIn("create_scheduled_tasks", o_output)
            self.assertNotIn(sentinel_name, o_output)
            self.assertNotIn(sentinel_dosage, o_output)

        # Test OpenRouterProvider
        orp = OpenRouterProvider(api_key="test")
        tc_or = MagicMock(index=0, id="tc_2")
        tc_or.function.name = "create_scheduled_tasks"
        tc_or.function.arguments = f'{{"items": [{{"name": "{sentinel_name}", "dosage": "{sentinel_dosage}"}}]}}'
        or_chunk = MagicMock(choices=[MagicMock(delta=MagicMock(tool_calls=[tc_or], content=None))])

        with patch.object(orp.client.chat.completions, "create", AsyncMock(return_value=MockAsyncStream([or_chunk]))), \
             patch("bot.ai.openrouter_provider.execute_tool", AsyncMock(return_value=fake_res)), \
             self.assertLogs("bot.ai.openrouter_provider", level="INFO") as cm_or:
            async for _ in orp.generate_stream([{"role": "user", "content": "test"}], {"user_id": 1, "chat_id": 2}):
                pass
            or_output = " ".join(cm_or.output)
            self.assertIn("create_scheduled_tasks", or_output)
            self.assertNotIn(sentinel_name, or_output)
            self.assertNotIn(sentinel_dosage, or_output)

    # 7. Complete generic batch creates one pending_confirmation ActionDraft and no ScheduledTask/jobs before confirmation
    async def test_complete_generic_batch_creates_pending_draft(self):
        args = {
            "context_type": "generic",
            "items": [
                {"name": "Exercise", "local_time": "07:30", "days_of_week": [0, 2, 4]},
                {"name": "Read book", "local_time": "21:00", "days_of_week": [0, 1, 2, 3, 4, 5, 6]},
            ]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2, timezone_name="UTC")
        self.assertTrue(res.stop)
        self.assertIsNotNone(res.draft_id)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(res.payload["missing_fields"], [])

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 0)
            drafts = (await session.execute(select(ActionDraft))).scalars().all()
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].status, DRAFT_STATUS_PENDING_CONFIRMATION)

    # 8. Complete medication batch with multiple items creates one—not several—pending_confirmation ActionDraft
    async def test_complete_medication_batch_creates_single_draft(self):
        args = {
            "context_type": "medication",
            "items": [
                {"name": "Med A", "dosage": "1 tab", "local_time": "08:00", "days_of_week": [0, 1, 2, 3, 4]},
                {"name": "Med B", "dosage": "2 drops", "local_time": "20:00", "days_of_week": [0, 1, 2, 3, 4]},
            ]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=10, chat_id=20, timezone_name="UTC")
        self.assertTrue(res.stop)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)

        async with self.SessionLocal() as session:
            drafts = (await session.execute(select(ActionDraft))).scalars().all()
            self.assertEqual(len(drafts), 1)

    # 9. Missing context creates awaiting_info and asks only context question
    async def test_missing_context_asks_context_question(self):
        args = {
            "items": [{"name": "Task 1", "local_time": "08:00", "days_of_week": [0]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(res.payload["missing_fields"][0], "context_type")
        self.assertEqual(res.display_text, "❓ Це розклад ліків чи інше повторюване завдання?")

    # 10. Missing medication dosage creates awaiting_info and asks only for first missing dosage
    async def test_missing_medication_dosage_asks_first_missing_dosage(self):
        args = {
            "context_type": "medication",
            "items": [
                {"name": "Med A", "local_time": "08:00", "days_of_week": [0]},
                {"name": "Med B", "local_time": "09:00", "days_of_week": [1]},
            ]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(res.payload["missing_fields"][0], "item_dosage:0")
        self.assertIn("Med A", res.display_text)
        self.assertIn("бот не обирає та не призначає дозування самостійно", res.display_text)

    # 11. Missing weekdays creates correct first missing token and question
    async def test_missing_weekdays_asks_days(self):
        args = {
            "context_type": "generic",
            "items": [{"name": "Task A", "local_time": "08:00"}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertEqual(res.payload["missing_fields"][0], "item_days:0")
        self.assertIn("У які дні повторювати", res.display_text)
        self.assertIn("Task A", res.display_text)

    # 12. Missing concrete time creates correct first missing token and question
    async def test_missing_concrete_time_asks_time(self):
        args = {
            "context_type": "generic",
            "items": [{"name": "Task A", "days_of_week": [0, 1]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertEqual(res.payload["missing_fields"][0], "item_time:0")
        self.assertIn("О котрій годині", res.display_text)
        self.assertIn("08:30", res.display_text)

    # 13. Relative item missing reference time asks for named event's time
    async def test_relative_missing_reference_time_asks_event_time(self):
        args = {
            "context_type": "generic",
            "items": [{
                "name": "Task A",
                "days_of_week": [0],
                "relative_to": "сніданок",
                "offset_minutes": 120,
            }]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertEqual(res.payload["missing_fields"][0], "reference_time:0")
        self.assertEqual(res.display_text, "❓ О котрій у вас сніданок?")

    # 14. Several relative items sharing one event produce one reference-time question
    async def test_several_relative_items_sharing_event_produce_one_question(self):
        args = {
            "context_type": "generic",
            "items": [
                {"name": "Item 1", "days_of_week": [0], "relative_to": "сніданок", "offset_minutes": 60},
                {"name": "Item 2", "days_of_week": [0], "relative_to": "  Сніданок ", "offset_minutes": 120},
            ]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        ref_fields = [f for f in res.payload["missing_fields"] if f.startswith("reference_time:")]
        self.assertEqual(len(ref_fields), 1)
        self.assertEqual(ref_fields[0], "reference_time:0")

    # 15. Invalid model-provided time/day/offset is not silently accepted
    async def test_invalid_model_values_treated_as_missing(self):
        args = {
            "context_type": "generic",
            "items": [{
                "name": "Task A",
                "local_time": "25:00",  # invalid
                "days_of_week": [7],    # invalid day 7
                "offset_minutes": 5000, # out of range
            }]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertIn("item_days:0", res.payload["missing_fields"])
        self.assertIn("item_time:0", res.payload["missing_fields"])

    # 16. Context clarification recomputes missing fields, including medication dosage
    async def test_context_clarification_recomputes_medication_dosage(self):
        args = {
            "items": [{"name": "Aspirin", "local_time": "08:00", "days_of_week": [0]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertEqual(res.payload["missing_fields"], ["context_type"])

        # Clarify context as medication
        res_reply = await apply_action_draft_reply(res.draft_id, user_id=1, chat_id=2, reply_text="ліки")
        self.assertTrue(res_reply.payload["success"])
        self.assertEqual(res_reply.payload["missing_fields"], ["item_dosage:0"])
        self.assertIn("дозування для <b>Aspirin</b>", res_reply.display_text)

    # 17. Name and dosage clarification preserve trimmed text verbatim on the same draft ID
    async def test_name_and_dosage_clarification_preserves_trimmed_verbatim(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="create_scheduled_tasks",
            payload={"context_type": "medication", "timezone": "UTC", "items": [{"name": None, "dosage": None, "local_time": "08:00", "days_of_week": [0]}]},
            missing_fields=["item_name:0", "item_dosage:0"]
        )

        res1 = await apply_action_draft_reply(draft.id, user_id=1, chat_id=2, reply_text="  Парацетамол-М 500  ")
        self.assertEqual(res1.draft_id, draft.id)
        self.assertEqual(res1.payload["missing_fields"], ["item_dosage:0"])

        res2 = await apply_action_draft_reply(draft.id, user_id=1, chat_id=2, reply_text="  1/2 таблетки  ")
        self.assertEqual(res2.draft_id, draft.id)
        self.assertEqual(res2.payload["missing_fields"], [])
        self.assertEqual(res2.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)

        d = await get_action_draft(draft.id, 1, 2)
        self.assertEqual(d.payload["items"][0]["name"], "Парацетамол-М 500")
        self.assertEqual(d.payload["items"][0]["dosage"], "1/2 таблетки")

    # 18. Time clarification canonicalizes 8:05 to 08:05
    async def test_time_clarification_canonicalization(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="create_scheduled_tasks",
            payload={"context_type": "generic", "timezone": "UTC", "items": [{"name": "Task", "local_time": None, "days_of_week": [0]}]},
            missing_fields=["item_time:0"]
        )
        res = await apply_action_draft_reply(draft.id, user_id=1, chat_id=2, reply_text=" 8:05 ")
        self.assertTrue(res.payload["success"])
        d = await get_action_draft(draft.id, 1, 2)
        self.assertEqual(d.payload["items"][0]["local_time"], "08:05")
        self.assertEqual(d.payload["items"][0]["resolved_local_time"], "08:05")

    # 19. Day clarification handles daily, weekdays, weekends, Ukrainian, English, and numeric values
    async def test_day_clarification_variants(self):
        cases = [
            ("щодня", [0, 1, 2, 3, 4, 5, 6]),
            ("будні", [0, 1, 2, 3, 4]),
            ("вихідні", [5, 6]),
            ("Пн, Ср, Пт", [0, 2, 4]),
            ("п’ятниця", [4]),
            ("пятниця", [4]),
            ("Mon, Tue", [0, 1]),
            ("0, 6", [0, 6]),
        ]
        for reply, expected in cases:
            draft = await create_action_draft(
                user_id=1, chat_id=2, action_type="create_scheduled_tasks",
                payload={"context_type": "generic", "timezone": "UTC", "items": [{"name": "T", "local_time": "08:00", "days_of_week": None}]},
                missing_fields=["item_days:0"]
            )
            res = await apply_action_draft_reply(draft.id, 1, 2, reply)
            self.assertTrue(res.payload["success"], f"Failed on {reply}")
            d = await get_action_draft(draft.id, 1, 2)
            self.assertEqual(d.payload["items"][0]["days_of_week"], expected, f"Mismatch for {reply}")

    # 20. Invalid clarification leaves same draft active and same first missing field
    async def test_invalid_clarification_rejection(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="create_scheduled_tasks",
            payload={"context_type": None, "timezone": "UTC", "items": [{"name": "Task"}]},
            missing_fields=["context_type"]
        )
        res = await apply_action_draft_reply(draft.id, 1, 2, "invalid context")
        self.assertFalse(res.payload["success"])
        self.assertEqual(res.payload["missing_fields"], ["context_type"])
        d = await get_action_draft(draft.id, 1, 2)
        self.assertEqual(d.status, DRAFT_STATUS_AWAITING_INFO)

    # 21. One reference-time reply resolves every item sharing that reference event
    async def test_one_reference_time_reply_resolves_all_matching_items(self):
        payload = {
            "context_type": "generic",
            "timezone": "UTC",
            "items": [
                {"name": "A", "relative_to": "сніданок", "offset_minutes": 30, "days_of_week": [0], "reference_time": None},
                {"name": "B", "relative_to": "Сніданок", "offset_minutes": 60, "days_of_week": [0], "reference_time": None},
            ]
        }
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="create_scheduled_tasks",
            payload=payload,
            missing_fields=["reference_time:0"]
        )
        res = await apply_action_draft_reply(draft.id, 1, 2, "08:00")
        self.assertTrue(res.payload["success"])
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)
        d = await get_action_draft(draft.id, 1, 2)
        self.assertEqual(d.payload["items"][0]["resolved_local_time"], "08:30")
        self.assertEqual(d.payload["items"][1]["resolved_local_time"], "09:00")

    # 22. Positive relative offset calculates exact local time
    async def test_positive_relative_offset(self):
        args = {
            "context_type": "generic",
            "items": [{"name": "After", "relative_to": "обід", "offset_minutes": 90, "reference_time": "13:00", "days_of_week": [0]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        d = await get_action_draft(res.draft_id, 1, 2)
        self.assertEqual(d.payload["items"][0]["resolved_local_time"], "14:30")
        self.assertEqual(d.payload["items"][0]["resolved_days_of_week"], [0])

    # 23. Negative relative offset calculates exact local time
    async def test_negative_relative_offset(self):
        args = {
            "context_type": "generic",
            "items": [{"name": "Before", "relative_to": "сніданок", "offset_minutes": -30, "reference_time": "08:00", "days_of_week": [0]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        d = await get_action_draft(res.draft_id, 1, 2)
        self.assertEqual(d.payload["items"][0]["resolved_local_time"], "07:30")
        self.assertEqual(d.payload["items"][0]["resolved_days_of_week"], [0])

    # 24. Crossing midnight forward shifts weekdays forward correctly
    async def test_crossing_midnight_forward_shifts_weekdays(self):
        # Monday (0) 23:30 + 120 min -> Tuesday (1) 01:30
        args = {
            "context_type": "generic",
            "items": [{"name": "Night", "relative_to": "вечеря", "offset_minutes": 120, "reference_time": "23:30", "days_of_week": [0]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        d = await get_action_draft(res.draft_id, 1, 2)
        self.assertEqual(d.payload["items"][0]["resolved_local_time"], "01:30")
        self.assertEqual(d.payload["items"][0]["resolved_days_of_week"], [1])

    # 25. Crossing midnight backward shifts weekdays backward correctly
    async def test_crossing_midnight_backward_shifts_weekdays(self):
        # Monday (0) 00:15 - 30 min -> Sunday (6) 23:45
        args = {
            "context_type": "generic",
            "items": [{"name": "Early", "relative_to": "північ", "offset_minutes": -30, "reference_time": "00:15", "days_of_week": [0]}]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        d = await get_action_draft(res.draft_id, 1, 2)
        self.assertEqual(d.payload["items"][0]["resolved_local_time"], "23:45")
        self.assertEqual(d.payload["items"][0]["resolved_days_of_week"], [6])

    # 26. Preview contains all items, resolved times, weekdays, timezone, and original relative rules
    async def test_preview_contents(self):
        args = {
            "context_type": "medication",
            "timezone": "Europe/Kyiv",
            "items": [
                {"name": "Med 1", "dosage": "1 таб", "details": "після їжі", "local_time": "09:00", "days_of_week": [0, 2, 4]},
                {"name": "Med 2", "dosage": "5 крапель", "relative_to": "вечеря", "offset_minutes": 60, "reference_time": "19:00", "days_of_week": [5, 6]},
            ]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2, timezone_name="Europe/Kyiv")
        preview = res.display_text
        self.assertIn("Підтвердження розкладу:", preview)
        self.assertIn("Ліки", preview)
        self.assertIn("Europe/Kyiv", preview)
        self.assertIn("Med 1", preview)
        self.assertIn("1 таб", preview)
        self.assertIn("після їжі", preview)
        self.assertIn("09:00", preview)
        self.assertIn("Пн, Ср, Пт", preview)
        self.assertIn("Med 2", preview)
        self.assertIn("20:00", preview)
        self.assertIn("+60 хв від «вечеря» о 19:00", preview)
        self.assertIn("Потрібне підтвердження", preview)

    # 27. Preview HTML-escapes names, dosage, details, and reference-event names
    async def test_preview_html_escapes_sensitive_fields(self):
        args = {
            "context_type": "medication",
            "items": [{
                "name": "Med <danger>",
                "dosage": "100 & 200 mg",
                "details": "take with <water>",
                "relative_to": "meal <lunch>",
                "offset_minutes": 30,
                "reference_time": "12:00",
                "days_of_week": [0],
            }]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        preview = res.display_text
        self.assertIn("Med &lt;danger&gt;", preview)
        self.assertIn("100 &amp; 200 mg", preview)
        self.assertIn("take with &lt;water&gt;", preview)
        self.assertIn("meal &lt;lunch&gt;", preview)
        self.assertNotIn("<danger>", preview)

    # 28. Default/draft execution creates no ScheduledTask, TaskOccurrence, scheduler job, or Telegram send
    async def test_default_execution_has_no_side_effects(self):
        args = {
            "context_type": "generic",
            "items": [{"name": "Task A", "local_time": "08:00", "days_of_week": [0]}]
        }
        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task") as mock_sched:
            res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
            mock_sched.assert_not_called()

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 0)
            occs = (await session.execute(select(TaskOccurrence))).scalars().all()
            self.assertEqual(len(occs), 0)

    # 29. create_scheduled_tasks_batch creates multiple ordered rows in one transaction
    async def test_create_scheduled_tasks_batch_creates_ordered_rows(self):
        items = [
            {"name": "First", "local_time": "08:00", "days_of_week": [0], "details": "d1", "dosage": None},
            {"name": "Second", "local_time": "12:00", "days_of_week": [1], "details": None, "dosage": None},
            {"name": "Third", "local_time": "18:00", "days_of_week": [2], "details": "d3", "dosage": None},
        ]
        tasks = await create_scheduled_tasks_batch(
            user_id=10, chat_id=20, context_type="generic", timezone_name="UTC", items=items
        )
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].name, "First")
        self.assertEqual(tasks[1].name, "Second")
        self.assertEqual(tasks[2].name, "Third")

        async with self.SessionLocal() as session:
            db_tasks = (await session.execute(select(ScheduledTask).order_by(ScheduledTask.id.asc()))).scalars().all()
            self.assertEqual(len(db_tasks), 3)
            self.assertEqual([t.name for t in db_tasks], ["First", "Second", "Third"])

    # 30. An invalid item causes zero rows from the whole batch
    async def test_create_scheduled_tasks_batch_invalid_item_rolls_back(self):
        items = [
            {"name": "Valid 1", "local_time": "08:00", "days_of_week": [0]},
            {"name": "Invalid", "local_time": "bad_time", "days_of_week": [0]},
            {"name": "Valid 2", "local_time": "10:00", "days_of_week": [0]},
        ]
        with self.assertRaises(ValueError):
            await create_scheduled_tasks_batch(10, 20, "generic", "UTC", items)

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 0)

    # 31. Confirmed execute_mutation=True creates all rows once and registers one cron job per task
    async def test_confirmed_mutation_creates_tasks_and_registers_jobs(self):
        payload = {
            "context_type": "generic",
            "timezone": "UTC",
            "items": [
                {"name": "T1", "local_time": "08:00", "days_of_week": [0, 1]},
                {"name": "T2", "local_time": "18:00", "days_of_week": [2, 3]},
            ]
        }
        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task") as mock_sched:
            res = await execute_tool(
                "create_scheduled_tasks", payload, user_id=10, chat_id=20, timezone_name="UTC", execute_mutation=True
            )
            self.assertTrue(res.payload["success"])
            self.assertEqual(res.payload["count"], 2)
            self.assertEqual(len(res.payload["task_ids"]), 2)
            self.assertEqual(mock_sched.call_count, 2)

    # 32. Confirmed execution creates no immediate TaskOccurrence
    async def test_confirmed_execution_creates_no_immediate_occurrence(self):
        payload = {
            "context_type": "generic",
            "timezone": "UTC",
            "items": [{"name": "Task", "local_time": "08:00", "days_of_week": [0]}]
        }
        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task"):
            await execute_tool("create_scheduled_tasks", payload, user_id=1, chat_id=2, execute_mutation=True)

        async with self.SessionLocal() as session:
            occs = (await session.execute(select(TaskOccurrence))).scalars().all()
            self.assertEqual(len(occs), 0)

    # 33. Scheduler registration failure after DB commit does not duplicate/delete rows and returns sanitized status
    async def test_scheduler_failure_retains_db_rows_and_returns_safe_status(self):
        payload = {
            "context_type": "generic",
            "timezone": "UTC",
            "items": [
                {"name": "T1", "local_time": "08:00", "days_of_week": [0]},
                {"name": "T2", "local_time": "09:00", "days_of_week": [1]},
            ]
        }
        call_count = 0
        def fail_first(task):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("APScheduler crashed")

        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task", side_effect=fail_first):
            res = await execute_tool(
                "create_scheduled_tasks", payload, user_id=1, chat_id=2, execute_mutation=True
            )
            self.assertTrue(res.payload["success"])
            self.assertEqual(res.payload["scheduler_errors"], 1)
            self.assertIn("перезапуску", res.display_text)

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 2)

    # 34. DB failure returns sanitized error with no sensitive log or response leak
    async def test_db_failure_returns_sanitized_error(self):
        payload = {
            "context_type": "generic",
            "timezone": "UTC",
            "items": [{"name": "SecretMed", "local_time": "08:00", "days_of_week": [0]}]
        }
        with patch("bot.ai.tools.create_scheduled_tasks_batch", side_effect=RuntimeError("SQL secret error")), \
             self.assertLogs("bot.ai.tools", level="ERROR") as cm:
            res = await execute_tool("create_scheduled_tasks", payload, user_id=1, chat_id=2, execute_mutation=True)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            err_logs = " ".join(cm.output)
            self.assertNotIn("SQL secret error", err_logs)
            self.assertNotIn("SecretMed", err_logs)

    # 35. Existing confirmation callback executes new action once for atomic winner
    async def test_confirmation_callback_executes_action(self):
        draft = await create_action_draft(
            user_id=123, chat_id=456, action_type="create_scheduled_tasks",
            payload={
                "context_type": "generic",
                "timezone": "UTC",
                "items": [{"name": "Exercise", "local_time": "08:00", "days_of_week": [0]}]
            },
            missing_fields=[]
        )
        update, query = make_mock_callback_update(f"draft:ok:{draft.id}", user_id=123, chat_id=456)
        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task"):
            await handle_callback(update, MagicMock())

        d = await get_action_draft(draft.id, 123, 456)
        self.assertEqual(d.status, DRAFT_STATUS_CONFIRMED)

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 1)

    # 36. Repeated confirmation creates no additional rows or jobs
    async def test_repeated_confirmation_creates_no_additional_rows(self):
        draft = await create_action_draft(
            user_id=123, chat_id=456, action_type="create_scheduled_tasks",
            payload={
                "context_type": "generic",
                "timezone": "UTC",
                "items": [{"name": "Exercise", "local_time": "08:00", "days_of_week": [0]}]
            },
            missing_fields=[]
        )
        update, query = make_mock_callback_update(f"draft:ok:{draft.id}", user_id=123, chat_id=456)
        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task") as mock_sched:
            await handle_callback(update, MagicMock())
            self.assertEqual(mock_sched.call_count, 1)

            # Second confirm
            await handle_callback(update, MagicMock())
            self.assertEqual(mock_sched.call_count, 1)

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 1)

    # 37. Exact user/chat ownership prevents foreign user from confirming or filling recurring draft
    async def test_ownership_enforcement(self):
        draft = await create_action_draft(
            user_id=10, chat_id=20, action_type="create_scheduled_tasks",
            payload={"context_type": None, "timezone": "UTC", "items": [{"name": "T"}]},
            missing_fields=["context_type"]
        )
        # Foreign user tries to clarify
        res = await apply_action_draft_reply(draft.id, user_id=999, chat_id=20, reply_text="ліки")
        self.assertFalse(res.payload["success"])
        self.assertEqual(res.payload["error"], "draft_not_found")

        # Foreign user tries to confirm via callback
        update, query = make_mock_callback_update(f"draft:ok:{draft.id}", user_id=999, chat_id=20)
        await handle_callback(update, MagicMock())
        query.answer.assert_called_with("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)

    # 38. Voice transcription does not execute AI action before explicit button callback
    async def test_voice_safety_unchanged(self):
        from bot.handlers.media import handle_voice_video
        with patch("bot.handlers.media.context_manager.save_message", new_callable=AsyncMock, return_value=777), \
             patch("bot.handlers.media.send_long_message", new_callable=AsyncMock) as mock_send, \
             patch("bot.handlers.media.get_ai_provider", new_callable=AsyncMock) as mock_get_provider, \
             patch("bot.handlers.media.check_transcription_limit", new_callable=AsyncMock, return_value=(True, "")), \
             patch("bot.handlers.media.record_transcription_usage", new_callable=AsyncMock), \
             patch("bot.handlers.media.download_file", new_callable=AsyncMock, return_value="/tmp/test.ogg"), \
             patch("bot.handlers.media.validate_audio_size"), \
             patch("bot.handlers.media.beautify_text", new_callable=AsyncMock, return_value=("Cleaned text", "gpt-4o-mini")), \
             patch("bot.handlers.media.get_user_model_settings", new_callable=AsyncMock, return_value={}), \
             patch("bot.handlers.media.cleanup_files"), \
             patch("bot.ai.tools.execute_tool", new_callable=AsyncMock) as mock_exec:

            mock_provider = MagicMock()
            mock_provider.transcribe = AsyncMock(return_value="Schedule medication every morning at 08:00")
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

            mock_exec.assert_not_called()
            mock_send.assert_awaited_once()
            kb = mock_send.call_args.kwargs.get("reply_markup")
            self.assertIsNotNone(kb)
            button_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
            button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
            self.assertIn("run_gpt:777", button_data)
            self.assertIn("▶️ Обробити як інструкцію", button_texts)

    # 39. Existing one-time reminder and delete-reminder actions still behave unchanged
    async def test_existing_reminder_actions_intact(self):
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        res = await execute_tool("schedule_reminder", {"text": "One-time", "iso_time_utc": future_iso}, user_id=1, chat_id=2)
        self.assertEqual(res.payload["action_type"], "schedule_reminder")
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)

        res_del = await execute_tool("delete_reminder", {"reminder_id": 5}, user_id=1, chat_id=2)
        self.assertEqual(res_del.payload["action_type"], "delete_reminder")
        self.assertEqual(res_del.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)

    # 40. Existing D1/D2 scheduled-task creation still behaves unchanged
    async def test_existing_d1_d2_intact(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="medication", name="Direct Task",
            local_time="08:00", timezone_name="UTC", days_of_week=[0], dosage="1 tab"
        )
        self.assertIsNotNone(task.id)
        fetched = await get_scheduled_task(task.id, 1, 2)
        self.assertEqual(fetched.name, "Direct Task")

    # 41. Malformed second item preserves index 1 and produces missing fields
    async def test_malformed_second_item_preserves_index_and_clarification(self):
        args = {
            "context_type": "generic",
            "items": [
                {"name": "Valid First", "local_time": "08:00", "days_of_week": [0]},
                "invalid second item string",
            ]
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2)
        self.assertTrue(res.stop)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        d = await get_action_draft(res.draft_id, 1, 2)
        self.assertEqual(len(d.payload["items"]), 2)
        self.assertEqual(d.payload["items"][0]["name"], "Valid First")
        self.assertIsNone(d.payload["items"][1]["name"])
        missing = res.payload["missing_fields"]
        self.assertIn("item_name:1", missing)
        self.assertIn("item_days:1", missing)
        self.assertIn("item_time:1", missing)
        self.assertEqual(missing[0], "item_name:1")

    # 42. Trusted timezone boundary: draft creation derives timezone from trusted argument, not tool args
    async def test_timezone_boundary_draft_creation_ignores_untrusted_args(self):
        args = {
            "context_type": "generic",
            "timezone": "America/New_York",
            "items": [{"name": "Task A", "local_time": "08:00", "days_of_week": [0]}],
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2, timezone_name="Europe/Kyiv")
        self.assertTrue(res.stop)
        d = await get_action_draft(res.draft_id, 1, 2)
        self.assertEqual(d.payload["timezone"], "Europe/Kyiv")
        self.assertNotIn("America/New_York", d.payload["timezone"])

    # 43. Trusted timezone boundary: clarification rewrites/keeps draft on currently supplied trusted timezone
    async def test_timezone_boundary_clarification_rewrites_stale_injected_timezone(self):
        draft = await create_action_draft(
            user_id=1, chat_id=2, action_type="create_scheduled_tasks",
            payload={
                "context_type": None,
                "timezone": "America/New_York",
                "items": [{"name": "Task A", "local_time": "08:00", "days_of_week": [0]}],
            },
            missing_fields=["context_type"]
        )
        res = await apply_action_draft_reply(
            draft.id, user_id=1, chat_id=2, reply_text="завдання", timezone_name="Europe/Kyiv"
        )
        self.assertTrue(res.payload["success"])
        d = await get_action_draft(draft.id, 1, 2)
        self.assertEqual(d.payload["timezone"], "Europe/Kyiv")
        self.assertIn("Europe/Kyiv", res.display_text)
        self.assertNotIn("America/New_York", res.display_text)

    # 44. Trusted timezone boundary: confirmed execution creates ScheduledTask rows with trusted timezone
    async def test_timezone_boundary_confirmed_mutation_uses_trusted_timezone(self):
        payload = {
            "context_type": "generic",
            "timezone": "America/New_York",
            "items": [
                {"name": "T1", "local_time": "08:00", "days_of_week": [0]},
                {"name": "T2", "local_time": "09:00", "days_of_week": [1]},
            ],
        }
        with patch("bot.ai.tools.scheduler_service.schedule_recurring_task"):
            res = await execute_tool(
                "create_scheduled_tasks", payload, user_id=1, chat_id=2, timezone_name="Europe/Kyiv", execute_mutation=True
            )
            self.assertTrue(res.payload["success"])

        async with self.SessionLocal() as session:
            tasks = (await session.execute(select(ScheduledTask))).scalars().all()
            self.assertEqual(len(tasks), 2)
            for t in tasks:
                self.assertEqual(t.timezone, "Europe/Kyiv")
                self.assertNotEqual(t.timezone, "America/New_York")

    # 45. Trusted timezone boundary: invalid trusted timezone uses safe fallback, never untrusted payload
    async def test_timezone_boundary_invalid_trusted_uses_safe_fallback_not_untrusted(self):
        args = {
            "context_type": "generic",
            "timezone": "America/New_York",
            "items": [{"name": "Task A", "local_time": "08:00", "days_of_week": [0]}],
        }
        res = await execute_tool("create_scheduled_tasks", args, user_id=1, chat_id=2, timezone_name="Invalid/Trash_TZ")
        self.assertTrue(res.stop)
        d = await get_action_draft(res.draft_id, 1, 2)
        from config import BOT_TIMEZONE
        expected_fallback = d.payload["timezone"]
        self.assertIn(expected_fallback, (BOT_TIMEZONE, "UTC"))
        self.assertNotEqual(expected_fallback, "America/New_York")
        self.assertIn(expected_fallback, res.display_text)
        self.assertNotIn("Invalid/Trash_TZ", res.display_text)
        self.assertNotIn("America/New_York", res.display_text)


if __name__ == "__main__":
    unittest.main()
