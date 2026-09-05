import os
import sys
import unittest
import html
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func

from bot.database.models import Base, UserList, ListItem, ActionDraft
from bot.ai.tools import (
    get_tool_definitions,
    get_openai_tools,
    execute_tool,
    apply_action_draft_reply,
    format_draft_preview_or_question,
    ToolResult,
    SHOW_SHOPPING_LIST_SCHEMA,
    ADD_SHOPPING_ITEMS_SCHEMA,
    SET_SHOPPING_ITEM_STATE_SCHEMA,
    DELETE_SHOPPING_ITEM_SCHEMA,
    CLEAR_BOUGHT_ITEMS_SCHEMA,
    SHOPPING_DISPLAY_LIMIT,
)
from bot.ai.google_provider import GoogleProvider
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.openrouter_provider import OpenRouterProvider
from bot.utils.lists import (
    LIST_TYPE_SHOPPING,
    DEFAULT_SHOPPING_LIST_NAME,
    create_or_get_user_list,
    add_list_items,
    set_list_item_done,
    get_list_item,
    list_list_items,
)
from bot.utils.action_drafts import (
    get_action_draft,
    get_active_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
)


class MockAsyncStream:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class TestShoppingActionsBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.lists.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()


class TestShoppingSchemaParity(unittest.IsolatedAsyncioTestCase):
    """Scenarios 1-6: Schema & Provider Parity."""

    # 1. Усі п’ять shopping actions присутні у get_tool_definitions(False).
    def test_01_all_five_actions_in_get_tool_definitions_no_search(self):
        defs = get_tool_definitions(allow_search=False)
        names = [d["name"] for d in defs]
        expected_shopping = [
            "show_shopping_list",
            "add_shopping_items",
            "set_shopping_item_state",
            "delete_shopping_item",
            "clear_bought_items",
        ]
        for name in expected_shopping:
            self.assertIn(name, names)

    # 2. allow_search=False прибирає тільки web_search.
    def test_02_allow_search_false_removes_only_web_search(self):
        no_search = [d["name"] for d in get_tool_definitions(allow_search=False)]
        with_search = [d["name"] for d in get_tool_definitions(allow_search=True)]
        self.assertNotIn("web_search", no_search)
        self.assertIn("web_search", with_search)
        diff = set(with_search) - set(no_search)
        self.assertEqual(diff, {"web_search"})

    # 3. OpenAI/OpenRouter wrapper отримує ті самі назви.
    def test_03_openai_openrouter_wrapper_has_same_names(self):
        wrapper = get_openai_tools(allow_search=False)
        wrapper_names = [t["function"]["name"] for t in wrapper]
        defs_names = [d["name"] for d in get_tool_definitions(allow_search=False)]
        self.assertEqual(wrapper_names, defs_names)

    # 4. Google declaration converter отримує ті самі назви.
    def test_04_google_declaration_converter_has_same_names(self):
        provider = GoogleProvider(api_key="test-key")
        proto = provider._get_tools_proto(allow_search=False)
        google_names = [d.name for d in proto.function_declarations]
        defs_names = [d["name"] for d in get_tool_definitions(allow_search=False)]
        self.assertEqual(google_names, defs_names)

    # 5. У provider-файлах немає локальних hardcoded shopping schemas.
    def test_05_no_hardcoded_shopping_schemas_in_provider_files(self):
        shopping_names = [
            "show_shopping_list",
            "add_shopping_items",
            "set_shopping_item_state",
            "delete_shopping_item",
            "clear_bought_items",
        ]
        provider_files = [
            "bot/ai/openai_provider.py",
            "bot/ai/openrouter_provider.py",
            "bot/ai/google_provider.py",
        ]
        repo_root = os.path.dirname(os.path.abspath(__file__))
        for rel_path in provider_files:
            abs_path = os.path.join(repo_root, rel_path)
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            for action in shopping_names:
                self.assertNotIn(f'"{action}"', content, f"Hardcoded action {action} found in {rel_path}")

    # 6. disable_tools=True не передає tools моделі.
    async def test_06_disable_tools_does_not_pass_tools_to_models(self):
        # Test GoogleProvider
        google_provider = GoogleProvider(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_cls:
            mock_model = MagicMock()
            mock_chat = MagicMock()
            mock_chat.send_message_async = AsyncMock(return_value=MockAsyncStream([MagicMock(candidates=[], text="Hello")]))
            mock_model.start_chat.return_value = mock_chat
            mock_model_cls.return_value = mock_model

            chunks = []
            async for chunk in google_provider.generate_stream(
                messages=[{"role": "user", "content": "hi"}],
                settings={"disable_tools": True, "chat_id": 100, "user_id": 10}
            ):
                chunks.append(chunk)

            mock_model_cls.assert_called_once()
            call_kwargs = mock_model_cls.call_args[1]
            self.assertIsNone(call_kwargs.get("tools"))


class TestShoppingReadOnlyShow(TestShoppingActionsBase):
    """Scenarios 7-16: Read-only Show."""

    # 7. Порожня БД: empty-list display, не створюється UserList, не створюється ActionDraft.
    async def test_07_empty_db_show_shopping_list_creates_no_list_or_draft(self):
        res = await execute_tool("show_shopping_list", {}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertTrue(res.stop)
        self.assertIn("Покупки", res.display_text)
        self.assertIn("Список порожній", res.display_text)

        async with self.SessionLocal() as session:
            lists_cnt = await session.scalar(select(func.count(UserList.id)))
            drafts_cnt = await session.scalar(select(func.count(ActionDraft.id)))
            self.assertEqual(lists_cnt, 0)
            self.assertEqual(drafts_cnt, 0)

    # 8. Explicit existing list знаходиться case-insensitive/whitespace-normalized.
    async def test_08_explicit_existing_list_found_case_insensitive_normalized(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Мої Покупки")
        await add_list_items(ul.id, 101, 1, ["Яблука"])

        res = await execute_tool("show_shopping_list", {"list_name": "  мої   покупки  "}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertIn("Мої Покупки", res.display_text)
        self.assertIn("Яблука", res.display_text)

    # 9. Explicit missing list не створюється.
    async def test_09_explicit_missing_list_not_created(self):
        res = await execute_tool("show_shopping_list", {"list_name": "Невідомий Список"}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertIn("Невідомий Список", res.display_text)
        self.assertIn("Список порожній", res.display_text)

        async with self.SessionLocal() as session:
            lists_cnt = await session.scalar(select(func.count(UserList.id)))
            self.assertEqual(lists_cnt, 0)

    # 10. За відсутності explicit name reuse єдиного existing shopping list.
    async def test_10_fallback_to_single_existing_shopping_list(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Особистий")
        await add_list_items(ul.id, 101, 1, ["Сир"])

        res = await execute_tool("show_shopping_list", {}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertIn("Особистий", res.display_text)
        self.assertIn("Сир", res.display_text)

    # 11. За кількох списків reuse existing default Покупки.
    async def test_11_fallback_to_existing_default_pokupky_when_multiple(self):
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Будівництво")
        await add_list_items(ul1.id, 101, 1, ["Цвяхи"])
        ul2, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name=DEFAULT_SHOPPING_LIST_NAME)
        await add_list_items(ul2.id, 101, 1, ["Хліб"])

        res = await execute_tool("show_shopping_list", {}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertIn(DEFAULT_SHOPPING_LIST_NAME, res.display_text)
        self.assertIn("Хліб", res.display_text)
        self.assertNotIn("Цвяхи", res.display_text)

    # 12. Інший chat не бачить назву або items.
    async def test_12_other_chat_cannot_see_name_or_items(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Секретний")
        await add_list_items(ul.id, 101, 1, ["Секретний Товар"])

        res = await execute_tool("show_shopping_list", {"list_name": "Секретний"}, chat_id=999, user_id=2)
        self.assertTrue(res.payload["success"])
        self.assertNotIn("Секретний Товар", res.display_text)
        self.assertIn("Список порожній", res.display_text)

    # 13. User list name та item text з <, >, &, лапками не ламають HTML.
    async def test_13_html_escaping_in_list_name_and_item_texts(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="<Тест & Спецсимволи>")
        await add_list_items(ul.id, 101, 1, ["Яблука <червоні> & 'солодкі'"])

        res = await execute_tool("show_shopping_list", {"list_name": "<Тест & Спецсимволи>"}, chat_id=101, user_id=1)
        self.assertIn("&lt;Тест &amp; Спецсимволи&gt;", res.display_text)
        self.assertIn("Яблука &lt;червоні&gt; &amp; &#x27;солодкі&#x27;", res.display_text)
        self.assertNotIn("<червоні>", res.display_text)

    # 14. Active items виводяться перед done items.
    async def test_14_active_items_displayed_before_done_items(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Перший", "Другий", "Третій"])
        await set_list_item_done(items[0].id, 101, 1, is_done=True)  # First is done

        res = await execute_tool("show_shopping_list", {}, chat_id=101, user_id=1)
        active_pos_2 = res.display_text.find("Другий")
        active_pos_3 = res.display_text.find("Третій")
        done_pos_1 = res.display_text.find("Перший")
        self.assertTrue(active_pos_2 < done_pos_1)
        self.assertTrue(active_pos_3 < done_pos_1)

    # 15. Виводяться numeric item IDs.
    async def test_15_numeric_item_ids_present_in_output(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Банани"])

        res = await execute_tool("show_shopping_list", {}, chat_id=101, user_id=1)
        self.assertIn(f"#{items[0].id} Банани", res.display_text)

    # 16. Великий список не перевищує safe output bound і містить truncated count.
    async def test_16_large_list_bounded_with_truncated_count(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Великий")
        # 100 long items
        batch = [f"Довгий пункт списку номер {i:03d} з додатковим описом для тестування довжини повідомлення" for i in range(100)]
        await add_list_items(ul.id, 101, 1, batch)

        res = await execute_tool("show_shopping_list", {"list_name": "Великий"}, chat_id=101, user_id=1)
        self.assertLessEqual(len(res.display_text), 3600)
        self.assertIn("… ще", res.display_text)
        self.assertIn("пунктів", res.display_text)


class TestShoppingDraftInterception(TestShoppingActionsBase):
    """Scenarios 17-24: Draft Interception."""

    # 17. add_shopping_items у default mode: створює ActionDraft, не створює UserList, не створює ListItem.
    async def test_17_add_shopping_items_default_creates_draft_no_list_no_items(self):
        res = await execute_tool("add_shopping_items", {"items": ["Молоко", "Хліб"]}, user_id=1, chat_id=101)
        self.assertTrue(res.payload["success"])
        self.assertTrue(res.stop)
        self.assertIsNotNone(res.draft_id)

        async with self.SessionLocal() as session:
            lists_cnt = await session.scalar(select(func.count(UserList.id)))
            items_cnt = await session.scalar(select(func.count(ListItem.id)))
            drafts_cnt = await session.scalar(select(func.count(ActionDraft.id)))
            self.assertEqual(lists_cnt, 0)
            self.assertEqual(items_cnt, 0)
            self.assertEqual(drafts_cnt, 1)

    # 18. Повний add payload створює pending_confirmation.
    async def test_18_complete_add_payload_creates_pending_confirmation(self):
        res = await execute_tool("add_shopping_items", {"items": ["Кава"]}, user_id=1, chat_id=101)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(res.payload["missing_fields"], [])
        self.assertIn("Підтвердження додавання до списку", res.display_text)
        self.assertIn("Кава", res.display_text)

    # 19. Add без items створює awaiting_info з missing_fields=["items"].
    async def test_19_add_without_items_creates_awaiting_info_items(self):
        res = await execute_tool("add_shopping_items", {"list_name": "Покупки"}, user_id=1, chat_id=101)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(res.payload["missing_fields"], ["items"])
        self.assertEqual(res.display_text, "❓ Що додати до списку покупок?")

    # 20. set_shopping_item_state, delete_shopping_item, clear_bought_items у default mode не змінюють list tables.
    async def test_20_mutating_actions_in_default_mode_do_not_modify_list_tables(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Хліб", "Масло"])

        # Call all three in default mode
        await execute_tool("set_shopping_item_state", {"item_id": items[0].id, "state": "done"}, user_id=1, chat_id=101)
        await execute_tool("delete_shopping_item", {"item_id": items[1].id}, user_id=1, chat_id=101)
        await execute_tool("clear_bought_items", {}, user_id=1, chat_id=101)

        # Verify DB untouched
        current_items = await list_list_items(ul.id, 101)
        self.assertEqual(len(current_items), 2)
        self.assertFalse(current_items[0].is_done)
        self.assertFalse(current_items[1].is_done)

    # 21. Неповний set-state запитує поля по одному у стабільному порядку.
    async def test_21_incomplete_set_state_asks_fields_in_stable_order(self):
        res = await execute_tool("set_shopping_item_state", {}, user_id=1, chat_id=101)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(res.payload["missing_fields"], ["item_id", "state"])
        self.assertEqual(res.display_text, "❓ Вкажіть номер пункту, стан якого потрібно змінити.")

    # 22. Foreign/nonexistent item ID не створює executable pending draft і не розкриває інший чат.
    async def test_22_foreign_or_nonexistent_item_id_not_executable_no_leak(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=202, list_type=LIST_TYPE_SHOPPING, name="Чужий")
        foreign_items = await add_list_items(ul.id, 202, 1, ["Чужий Хліб"])

        # Attempt in chat 101 with foreign item ID
        res = await execute_tool("set_shopping_item_state", {"item_id": foreign_items[0].id, "state": "done"}, user_id=1, chat_id=101)
        self.assertEqual(res.payload["status"], DRAFT_STATUS_AWAITING_INFO)
        self.assertIn("item_id", res.payload["missing_fields"])
        self.assertEqual(res.display_text, "❓ Вкажіть номер пункту, стан якого потрібно змінити.")

    # 23. Preview HTML-escape-ить list name та items.
    async def test_23_preview_escapes_html_in_list_name_and_items(self):
        res = await execute_tool("add_shopping_items", {"list_name": "<Шопінг & Більше>", "items": ["<Хліб> & 'Булка'"]}, user_id=1, chat_id=101)
        self.assertIn("&lt;Шопінг &amp; Більше&gt;", res.display_text)
        self.assertIn("&lt;Хліб&gt; &amp; &#x27;Булка&#x27;", res.display_text)
        self.assertNotIn("<Хліб>", res.display_text)

    # 24. Новий draft замінює попередній active draft за поточним ActionDraft lifecycle.
    async def test_24_new_draft_replaces_previous_active_draft(self):
        res1 = await execute_tool("add_shopping_items", {"items": ["Перший"]}, user_id=1, chat_id=101)
        draft1_id = res1.draft_id
        res2 = await execute_tool("add_shopping_items", {"items": ["Другий"]}, user_id=1, chat_id=101)
        draft2_id = res2.draft_id

        self.assertNotEqual(draft1_id, draft2_id)
        d1 = await get_action_draft(draft1_id, 1, 101)
        d2 = await get_action_draft(draft2_id, 1, 101)
        self.assertEqual(d1.status, "cancelled")
        self.assertEqual(d2.status, DRAFT_STATUS_PENDING_CONFIRMATION)


class TestShoppingClarification(TestShoppingActionsBase):
    """Scenarios 25-32: Clarification Replies."""

    # 25. Reply молоко, хліб; кава\nяблука дає чотири items у правильному порядку.
    async def test_25_reply_delimiters_split_items_in_correct_order(self):
        res = await execute_tool("add_shopping_items", {}, user_id=1, chat_id=101)
        draft_id = res.draft_id

        reply_res = await apply_action_draft_reply(
            draft_id=draft_id,
            user_id=1,
            chat_id=101,
            reply_text="молоко, хліб; кава\nяблука",
        )
        self.assertTrue(reply_res.payload["success"])
        d = await get_action_draft(draft_id, 1, 101)
        self.assertEqual(d.payload["items"], ["молоко", "хліб", "кава", "яблука"])
        self.assertEqual(d.status, DRAFT_STATUS_PENDING_CONFIRMATION)

    # 26. Reply без delimiter дає один item.
    async def test_26_reply_without_delimiters_yields_single_item(self):
        res = await execute_tool("add_shopping_items", {}, user_id=1, chat_id=101)
        reply_res = await apply_action_draft_reply(
            draft_id=res.draft_id,
            user_id=1,
            chat_id=101,
            reply_text="хліб і масло",
        )
        self.assertTrue(reply_res.payload["success"])
        d = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(d.payload["items"], ["хліб і масло"])

    # 27. Порожній/некоректний reply залишає draft у awaiting_info.
    async def test_27_empty_or_invalid_reply_leaves_draft_awaiting_info(self):
        res = await execute_tool("add_shopping_items", {}, user_id=1, chat_id=101)
        reply_res = await apply_action_draft_reply(
            draft_id=res.draft_id,
            user_id=1,
            chat_id=101,
            reply_text="   ,,, ;;; \n\n   ",
        )
        self.assertFalse(reply_res.payload["success"])
        d = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(d.status, DRAFT_STATUS_AWAITING_INFO)
        self.assertIn("items", d.missing_fields)

    # 28. 12 і #12 коректно заповнюють item_id.
    async def test_28_numeric_and_hash_syntax_populates_item_id(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Пункт А", "Пункт Б"])

        # Test "12"
        res1 = await execute_tool("delete_shopping_item", {}, user_id=1, chat_id=101)
        reply1 = await apply_action_draft_reply(res1.draft_id, 1, 101, str(items[0].id))
        self.assertTrue(reply1.payload["success"])
        d1 = await get_action_draft(res1.draft_id, 1, 101)
        self.assertEqual(d1.payload["item_id"], items[0].id)
        self.assertEqual(d1.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        # Test "#12"
        res2 = await execute_tool("delete_shopping_item", {}, user_id=1, chat_id=101)
        reply2 = await apply_action_draft_reply(res2.draft_id, 1, 101, f"#{items[1].id}")
        self.assertTrue(reply2.payload["success"])
        d2 = await get_action_draft(res2.draft_id, 1, 101)
        self.assertEqual(d2.payload["item_id"], items[1].id)
        self.assertEqual(d2.status, DRAFT_STATUS_PENDING_CONFIRMATION)

    # 29. Invalid/zero/negative/foreign item ID не переводить draft у confirmation.
    async def test_29_invalid_or_foreign_item_id_keeps_awaiting_info(self):
        ul_other, _ = await create_or_get_user_list(user_id=1, chat_id=999, list_type=LIST_TYPE_SHOPPING, name="Чужий")
        other_items = await add_list_items(ul_other.id, 999, 1, ["Чужий"])

        res = await execute_tool("delete_shopping_item", {}, user_id=1, chat_id=101)

        # Foreign ID
        rep_foreign = await apply_action_draft_reply(res.draft_id, 1, 101, str(other_items[0].id))
        self.assertFalse(rep_foreign.payload["success"])

        # Nonexistent ID
        rep_nonexist = await apply_action_draft_reply(res.draft_id, 1, 101, "88888")
        self.assertFalse(rep_nonexist.payload["success"])

        # Zero
        rep_zero = await apply_action_draft_reply(res.draft_id, 1, 101, "0")
        self.assertFalse(rep_zero.payload["success"])

        # Negative
        rep_neg = await apply_action_draft_reply(res.draft_id, 1, 101, "-5")
        self.assertFalse(rep_neg.payload["success"])

        # Text
        rep_txt = await apply_action_draft_reply(res.draft_id, 1, 101, "не число")
        self.assertFalse(rep_txt.payload["success"])

        d = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(d.status, DRAFT_STATUS_AWAITING_INFO)

    # 30. State synonyms переходять у done або active.
    async def test_30_state_synonyms_map_to_done_or_active(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Пункт"])

        for word in ["done", "куплено", "куплений", "куплена", "куплене", "готово"]:
            res = await execute_tool("set_shopping_item_state", {"item_id": items[0].id}, user_id=1, chat_id=101)
            reply = await apply_action_draft_reply(res.draft_id, 1, 101, word)
            self.assertTrue(reply.payload["success"])
            d = await get_action_draft(res.draft_id, 1, 101)
            self.assertEqual(d.payload["state"], "done")

        for word in ["active", "повернути", "не куплено", "активний", "активне", "активна"]:
            res = await execute_tool("set_shopping_item_state", {"item_id": items[0].id}, user_id=1, chat_id=101)
            reply = await apply_action_draft_reply(res.draft_id, 1, 101, word)
            self.assertTrue(reply.payload["success"])
            d = await get_action_draft(res.draft_id, 1, 101)
            self.assertEqual(d.payload["state"], "active")

    # 31. Оновлюється та сама draft ID; нова draft не створюється.
    async def test_31_same_draft_id_updated_no_new_draft_created(self):
        res = await execute_tool("add_shopping_items", {}, user_id=1, chat_id=101)
        original_draft_id = res.draft_id

        reply_res = await apply_action_draft_reply(original_draft_id, 1, 101, "Морква")
        self.assertEqual(reply_res.draft_id, original_draft_id)

        async with self.SessionLocal() as session:
            drafts_cnt = await session.scalar(select(func.count(ActionDraft.id)))
            self.assertEqual(drafts_cnt, 1)

    # 32. Після заповнення останнього поля status стає pending_confirmation.
    async def test_32_filling_final_missing_field_transitions_to_pending_confirmation(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Пункт"])

        res = await execute_tool("set_shopping_item_state", {}, user_id=1, chat_id=101)
        d_init = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(d_init.missing_fields, ["item_id", "state"])

        # Fill 1st field
        rep1 = await apply_action_draft_reply(res.draft_id, 1, 101, str(items[0].id))
        d_mid = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(d_mid.status, DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(d_mid.missing_fields, ["state"])

        # Fill 2nd field
        rep2 = await apply_action_draft_reply(res.draft_id, 1, 101, "куплено")
        d_final = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(d_final.status, DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(d_final.missing_fields, [])


class TestShoppingConfirmedExecution(TestShoppingActionsBase):
    """Scenarios 33-44: Confirmed Execution."""

    # 33. Confirmed add: створює default list лише на execution path; додає всі items одним batch; повертає точні IDs і count.
    async def test_33_confirmed_add_creates_default_list_only_on_execution_path_batch_insert(self):
        # 1. Draft phase creates no list
        draft_res = await execute_tool("add_shopping_items", {"items": ["Хліб", "Масло"]}, user_id=1, chat_id=101)
        async with self.SessionLocal() as session:
            self.assertEqual(await session.scalar(select(func.count(UserList.id))), 0)

        # 2. Confirmed execution phase
        draft = await get_action_draft(draft_res.draft_id, 1, 101)
        exec_res = await execute_tool(
            "add_shopping_items",
            dict(draft.payload),
            user_id=1,
            chat_id=101,
            execute_mutation=True,
        )
        self.assertTrue(exec_res.payload["success"])
        self.assertEqual(exec_res.payload["count"], 2)
        self.assertEqual(len(exec_res.payload["item_ids"]), 2)
        self.assertTrue(exec_res.stop)
        self.assertIn("Додано 2 пункт(ів)", exec_res.display_text)

        async with self.SessionLocal() as session:
            self.assertEqual(await session.scalar(select(func.count(UserList.id))), 1)
            self.assertEqual(await session.scalar(select(func.count(ListItem.id))), 2)

    # 34. Explicit named list створюється/reuse лише після confirmation.
    async def test_34_explicit_named_list_created_or_reused_only_after_confirmation(self):
        draft_res = await execute_tool(
            "add_shopping_items",
            {"list_name": "Ремонт", "items": ["Фарба"]},
            user_id=1,
            chat_id=101,
        )
        async with self.SessionLocal() as session:
            self.assertEqual(await session.scalar(select(func.count(UserList.id))), 0)

        exec_res = await execute_tool(
            "add_shopping_items",
            {"list_name": "Ремонт", "items": ["Фарба"]},
            user_id=1,
            chat_id=101,
            execute_mutation=True,
        )
        self.assertTrue(exec_res.payload["success"])
        self.assertIn("Ремонт", exec_res.display_text)

    # 35. Existing deterministic target не підміняється іншим списком між preview і confirmation.
    async def test_35_existing_target_not_swapped_between_preview_and_confirmation(self):
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Єдиний")
        draft_res = await execute_tool("add_shopping_items", {"items": ["Яблуко"]}, user_id=1, chat_id=101)
        d = await get_action_draft(draft_res.draft_id, 1, 101)
        self.assertEqual(d.payload.get("list_id"), ul1.id)

        # Concurrently create another list before confirmation
        ul2, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Другий")

        exec_res = await execute_tool("add_shopping_items", dict(d.payload), user_id=1, chat_id=101, execute_mutation=True)
        self.assertEqual(exec_res.payload["list_id"], ul1.id)

    # 36. Mark done змінює exact item у correct chat.
    async def test_36_mark_done_updates_exact_item_in_correct_chat(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Хліб"])

        exec_res = await execute_tool(
            "set_shopping_item_state",
            {"item_id": items[0].id, "state": "done"},
            user_id=1,
            chat_id=101,
            execute_mutation=True,
        )
        self.assertTrue(exec_res.payload["success"])
        self.assertTrue(exec_res.payload["changed"])
        self.assertTrue(exec_res.payload["is_done"])

        it = await get_list_item(items[0].id, 101)
        self.assertTrue(it.is_done)

    # 37. Repeated desired state є idempotent success з changed=False.
    async def test_37_repeated_same_state_is_idempotent_with_changed_false(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Хліб"])

        # Mark done 1st time
        await execute_tool("set_shopping_item_state", {"item_id": items[0].id, "state": "done"}, user_id=1, chat_id=101, execute_mutation=True)

        # Mark done 2nd time
        exec_res = await execute_tool("set_shopping_item_state", {"item_id": items[0].id, "state": "done"}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertTrue(exec_res.payload["success"])
        self.assertFalse(exec_res.payload["changed"])
        self.assertIn("вже був позначений", exec_res.display_text)

    # 38. Restore переводить exact item назад в active.
    async def test_38_restore_marks_item_active(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Хліб"])
        await set_list_item_done(items[0].id, 101, 1, is_done=True)

        exec_res = await execute_tool(
            "set_shopping_item_state",
            {"item_id": items[0].id, "state": "active"},
            user_id=1,
            chat_id=101,
            execute_mutation=True,
        )
        self.assertTrue(exec_res.payload["success"])
        self.assertTrue(exec_res.payload["changed"])
        self.assertFalse(exec_res.payload["is_done"])

        it = await get_list_item(items[0].id, 101)
        self.assertFalse(it.is_done)

    # 39. Delete видаляє лише exact item у correct chat.
    async def test_39_delete_removes_exact_item_in_correct_chat(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Хліб", "Масло"])

        exec_res = await execute_tool("delete_shopping_item", {"item_id": items[0].id}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertTrue(exec_res.payload["success"])
        self.assertIn("успішно видалено", exec_res.display_text)

        remaining = await list_list_items(ul.id, 101)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, items[1].id)

    # 40. Clear bought видаляє лише done items target list; active items залишаються.
    async def test_40_clear_bought_deletes_only_done_items_preserves_active(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Активний 1", "Куплений 1", "Куплений 2"])
        await set_list_item_done(items[1].id, 101, 1, is_done=True)
        await set_list_item_done(items[2].id, 101, 1, is_done=True)

        exec_res = await execute_tool("clear_bought_items", {"list_id": ul.id}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertTrue(exec_res.payload["success"])
        self.assertEqual(exec_res.payload["deleted_count"], 2)

        remaining = await list_list_items(ul.id, 101)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, items[0].id)

        # Clear again when 0 bought items
        exec_res_zero = await execute_tool("clear_bought_items", {"list_id": ul.id}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertTrue(exec_res_zero.payload["success"])
        self.assertEqual(exec_res_zero.payload["deleted_count"], 0)
        self.assertIn("немає куплених пунктів", exec_res_zero.display_text)

    # 41. Foreign chat не може set/restore/delete/clear чужі items.
    async def test_41_foreign_chat_cannot_modify_or_clear_items(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        items = await add_list_items(ul.id, 101, 1, ["Хліб"])

        # Foreign set
        res_set = await execute_tool("set_shopping_item_state", {"item_id": items[0].id, "state": "done"}, user_id=2, chat_id=202, execute_mutation=True)
        self.assertFalse(res_set.payload["success"])
        self.assertEqual(res_set.payload["error"], "item_not_found")

        # Foreign delete
        res_del = await execute_tool("delete_shopping_item", {"item_id": items[0].id}, user_id=2, chat_id=202, execute_mutation=True)
        self.assertFalse(res_del.payload["success"])
        self.assertEqual(res_del.payload["error"], "item_not_found")

        # Item intact
        it = await get_list_item(items[0].id, 101)
        self.assertIsNotNone(it)
        self.assertFalse(it.is_done)

    # 42. DB failure не показує raw exception і не містить item/list content у логах.
    async def test_42_db_failure_safe_error_no_raw_exception_or_item_text_in_logs(self):
        sentinel_name = "SECRET_SUPER_LIST_NAME"
        sentinel_item = "SECRET_SUPER_ITEM_TEXT"

        with patch("bot.ai.tools.add_list_items", side_effect=Exception("RAW_INTERNAL_DB_CRASH_SECRET")), \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool(
                "add_shopping_items",
                {"list_name": sentinel_name, "items": [sentinel_item]},
                user_id=1,
                chat_id=101,
                execute_mutation=True,
            )
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertNotIn("RAW_INTERNAL_DB_CRASH_SECRET", res.display_text)

            combined_logs = "\n".join(log_capture.output)
            self.assertNotIn("RAW_INTERNAL_DB_CRASH_SECRET", combined_logs)
            self.assertNotIn(sentinel_name, combined_logs)
            self.assertNotIn(sentinel_item, combined_logs)

    # 43. execute_mutation=False ніколи не викликає E1 mutation functions.
    async def test_43_execute_mutation_false_never_calls_e1_mutations(self):
        with patch("bot.ai.tools.add_list_items") as m_add, \
             patch("bot.ai.tools.set_list_item_done") as m_set, \
             patch("bot.ai.tools.delete_list_item") as m_del, \
             patch("bot.ai.tools.clear_done_list_items") as m_clr:

            await execute_tool("add_shopping_items", {"items": ["Хліб"]}, user_id=1, chat_id=101, execute_mutation=False)
            await execute_tool("set_shopping_item_state", {"item_id": 1, "state": "done"}, user_id=1, chat_id=101, execute_mutation=False)
            await execute_tool("delete_shopping_item", {"item_id": 1}, user_id=1, chat_id=101, execute_mutation=False)
            await execute_tool("clear_bought_items", {}, user_id=1, chat_id=101, execute_mutation=False)

            m_add.assert_not_called()
            m_set.assert_not_called()
            m_del.assert_not_called()
            m_clr.assert_not_called()

    # 44. Unknown/invalid payload не змінює DB.
    async def test_44_unknown_or_invalid_payload_does_not_mutate_db(self):
        # Invalid add with empty items
        res1 = await execute_tool("add_shopping_items", {"items": []}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertFalse(res1.payload["success"])

        # Invalid set state with invalid state
        res2 = await execute_tool("set_shopping_item_state", {"item_id": 1, "state": "invalid"}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertFalse(res2.payload["success"])

        # Invalid delete with invalid ID
        res3 = await execute_tool("delete_shopping_item", {"item_id": "not_an_int"}, user_id=1, chat_id=101, execute_mutation=True)
        self.assertFalse(res3.payload["success"])

        async with self.SessionLocal() as session:
            self.assertEqual(await session.scalar(select(func.count(UserList.id))), 0)
            self.assertEqual(await session.scalar(select(func.count(ListItem.id))), 0)


class TestShoppingSeniorReviewFollowup(TestShoppingActionsBase):
    """Scenarios 1-14: Senior Review Follow-up (Exception Containment & Output Bounds)."""

    # 1. show_shopping_list: find_existing_user_list throws Exception("RAW_DB_SECRET")
    async def test_01_show_shopping_list_find_list_exception_contained(self):
        sentinel = "RAW_DB_SECRET_FIND_LIST"
        with patch("bot.ai.tools.find_existing_user_list", side_effect=Exception(sentinel)), \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("show_shopping_list", {}, chat_id=101, user_id=1)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

    # 2. show_shopping_list: list_list_items throws exception
    async def test_02_show_shopping_list_list_items_exception_contained(self):
        sentinel = "RAW_DB_SECRET_LIST_ITEMS"
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Покупки")
        with patch("bot.ai.tools.list_list_items", side_effect=Exception(sentinel)), \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("show_shopping_list", {"list_name": "Покупки"}, chat_id=101, user_id=1)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

    # 3. Default add_shopping_items: find_existing_user_list throws exception
    async def test_03_default_add_find_list_exception_contained(self):
        sentinel = "RAW_DB_SECRET_ADD_FIND"
        with patch("bot.ai.tools.find_existing_user_list", side_effect=Exception(sentinel)), \
             patch("bot.ai.tools.add_list_items") as m_add, \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("add_shopping_items", {"items": ["Хліб"]}, chat_id=101, user_id=1, execute_mutation=False)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            self.assertIsNone(res.draft_id)
            m_add.assert_not_called()
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

        async with self.SessionLocal() as session:
            drafts_cnt = await session.scalar(select(func.count(ActionDraft.id)))
            self.assertEqual(drafts_cnt, 0)

    # 4. Default mutating action: create_action_draft throws exception
    async def test_04_default_create_draft_exception_contained(self):
        sentinel = "RAW_DB_SECRET_CREATE_DRAFT"
        with patch("bot.ai.tools.create_action_draft", side_effect=Exception(sentinel)), \
             patch("bot.ai.tools.add_list_items") as m_add, \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("add_shopping_items", {"items": ["Хліб"]}, chat_id=101, user_id=1, execute_mutation=False)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            self.assertIsNone(res.draft_id)
            m_add.assert_not_called()
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

    # 5. Confirmed add_shopping_items: resolve_user_list throws exception
    async def test_05_confirmed_add_resolve_list_exception_contained(self):
        sentinel = "RAW_DB_SECRET_RESOLVE"
        with patch("bot.ai.tools.resolve_user_list", side_effect=Exception(sentinel)), \
             patch("bot.ai.tools.add_list_items") as m_add, \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("add_shopping_items", {"items": ["Хліб"]}, chat_id=101, user_id=1, execute_mutation=True)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            m_add.assert_not_called()
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

    # 6. Default set/delete: get_list_item throws exception
    async def test_06_default_set_delete_get_item_exception_contained(self):
        sentinel = "RAW_DB_SECRET_GET_ITEM"
        for tool_name, args in [
            ("set_shopping_item_state", {"item_id": 42, "state": "done"}),
            ("delete_shopping_item", {"item_id": 42}),
        ]:
            with self.subTest(tool=tool_name):
                with patch("bot.ai.tools.get_list_item", side_effect=Exception(sentinel)), \
                     self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
                    res = await execute_tool(tool_name, args, chat_id=101, user_id=1, execute_mutation=False)
                    self.assertFalse(res.payload["success"])
                    self.assertEqual(res.payload["error"], "database_error")
                    self.assertTrue(res.stop)
                    self.assertIsNone(res.draft_id)
                    self.assertNotIn(sentinel, res.display_text)
                    self.assertNotIn(sentinel, "\n".join(log_capture.output))

                async with self.SessionLocal() as session:
                    drafts_cnt = await session.scalar(select(func.count(ActionDraft.id)))
                    self.assertEqual(drafts_cnt, 0)

    # 7. Confirmed clear_bought_items: target lookup or resolve throws exception
    async def test_07_confirmed_clear_lookup_or_resolve_exception_contained(self):
        sentinel = "RAW_DB_SECRET_CLEAR_LOOKUP"
        # Subcase A: find_existing_user_list fails
        with patch("bot.ai.tools.find_existing_user_list", side_effect=Exception(sentinel)), \
             patch("bot.ai.tools.clear_done_list_items") as m_clear, \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("clear_bought_items", {}, chat_id=101, user_id=1, execute_mutation=True)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            m_clear.assert_not_called()
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

        # Subcase B: resolve_user_list fails
        with patch("bot.ai.tools.find_existing_user_list", return_value=None), \
             patch("bot.ai.tools.resolve_user_list", side_effect=Exception(sentinel)), \
             patch("bot.ai.tools.clear_done_list_items") as m_clear, \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            res = await execute_tool("clear_bought_items", {}, chat_id=101, user_id=1, execute_mutation=True)
            self.assertFalse(res.payload["success"])
            self.assertEqual(res.payload["error"], "database_error")
            self.assertTrue(res.stop)
            m_clear.assert_not_called()
            self.assertNotIn(sentinel, res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

    # 8. Shopping clarification: get_list_item throws exception
    async def test_08_shopping_clarification_get_item_exception_contained(self):
        sentinel = "RAW_DB_SECRET_CLARIFY"
        res_init = await execute_tool("delete_shopping_item", {}, chat_id=101, user_id=1)
        draft_id = res_init.draft_id
        self.assertIsNotNone(draft_id)

        with patch("bot.ai.tools.get_list_item", side_effect=Exception(sentinel)), \
             patch("bot.ai.tools.update_action_draft_information") as m_update, \
             self.assertLogs("bot.ai.tools", level="INFO") as log_capture:
            reply_res = await apply_action_draft_reply(draft_id, 1, 101, "12")
            self.assertFalse(reply_res.payload["success"])
            self.assertEqual(reply_res.payload["error"], "database_error")
            self.assertEqual(reply_res.draft_id, draft_id)
            self.assertTrue(reply_res.stop)
            m_update.assert_not_called()
            self.assertNotIn(sentinel, reply_res.display_text)
            self.assertNotIn(sentinel, "\n".join(log_capture.output))

        d = await get_action_draft(draft_id, 1, 101)
        self.assertEqual(d.status, DRAFT_STATUS_AWAITING_INFO)
        self.assertIn("item_id", d.missing_fields)

    # 9. Empty show with title length 10,000 characters
    async def test_09_empty_show_title_10000_chars_bounded_and_html_safe(self):
        huge_title = "<script>alert('xss')</script>" * 350
        self.assertGreater(len(huge_title), 10000)

        res = await execute_tool("show_shopping_list", {"list_name": huge_title}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertLessEqual(len(res.display_text), SHOPPING_DISPLAY_LIMIT)
        self.assertNotIn("<script>", res.display_text)
        self.assertIn("&lt;script&gt;", res.display_text)
        self.assertIn("<b>", res.display_text)
        self.assertIn("</b>", res.display_text)
        self.assertIn("Список порожній", res.display_text)

    # 10. Show with single item text length 10,000 characters
    async def test_10_show_single_item_10000_chars_bounded_and_has_ellipsis(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="Тест")
        huge_item = "<item>data & more</item>" * 450
        self.assertGreater(len(huge_item), 10000)
        await add_list_items(ul.id, 101, 1, [huge_item])

        res = await execute_tool("show_shopping_list", {"list_name": "Тест"}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertLessEqual(len(res.display_text), SHOPPING_DISPLAY_LIMIT)
        self.assertIn("…", res.display_text)
        self.assertNotIn("<item>", res.display_text)
        self.assertIn("&lt;item&gt;", res.display_text)

    # 11. Show with large number of ampersands (&)
    async def test_11_show_many_ampersands_no_broken_entities(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=101, list_type=LIST_TYPE_SHOPPING, name="&" * 10000)
        await add_list_items(ul.id, 101, 1, ["&" * 10000])

        res = await execute_tool("show_shopping_list", {"list_name": "&" * 10000}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertLessEqual(len(res.display_text), SHOPPING_DISPLAY_LIMIT)
        import re
        broken_entities = re.findall(r"&(?!amp;|lt;|gt;|quot;|#x27;|#39;|[a-zA-Z0-9]+;)", res.display_text)
        self.assertEqual(broken_entities, [], f"Found broken entity in: {res.display_text}")

    # 12. Add preview with 100+ long items
    async def test_12_add_preview_100_long_items_bounded_and_payload_intact(self):
        items = [f"Пункт списку покупок #{i:03d} з довгим описом для перевірки обмежень" for i in range(120)]
        res = await execute_tool("add_shopping_items", {"items": items}, chat_id=101, user_id=1)
        self.assertTrue(res.payload["success"])
        self.assertLessEqual(len(res.display_text), SHOPPING_DISPLAY_LIMIT)
        self.assertIn("… ще", res.display_text)
        self.assertIn("пунктів", res.display_text)

        import re
        match = re.search(r"… ще (\d+) пунктів", res.display_text)
        self.assertIsNotNone(match)
        omitted_n = int(match.group(1))
        shown_bullets = len(re.findall(r"• Пункт списку", res.display_text))
        self.assertEqual(shown_bullets + omitted_n, 120)

        d = await get_action_draft(res.draft_id, 1, 101)
        self.assertEqual(len(d.payload["items"]), 120)

    # 13. Add clarification with large reply
    async def test_13_add_clarification_large_reply_bounded_and_payload_intact(self):
        res_init = await execute_tool("add_shopping_items", {}, chat_id=101, user_id=1)
        draft_id = res_init.draft_id
        self.assertIsNotNone(draft_id)

        large_reply = ", ".join(f"Товар #{i:03d} особливий сорт" for i in range(120))
        reply_res = await apply_action_draft_reply(draft_id, 1, 101, large_reply)
        self.assertTrue(reply_res.payload["success"])
        self.assertEqual(reply_res.draft_id, draft_id)
        self.assertEqual(reply_res.payload["status"], DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertLessEqual(len(reply_res.display_text), SHOPPING_DISPLAY_LIMIT)
        self.assertIn("… ще", reply_res.display_text)

        d = await get_action_draft(draft_id, 1, 101)
        self.assertEqual(len(d.payload["items"]), 120)

    # 14. Confirmed add/clear success with very long list name
    async def test_14_confirmed_add_and_clear_long_list_name_bounded_and_db_full_name(self):
        long_name = "НадзвичайноДовгаНазваСпискуПокупок" * 300
        self.assertGreater(len(long_name), 10000)

        # Confirmed add
        res_add = await execute_tool(
            "add_shopping_items",
            {"list_name": long_name, "items": ["Хліб", "Масло"]},
            chat_id=101,
            user_id=1,
            execute_mutation=True,
        )
        self.assertTrue(res_add.payload["success"])
        self.assertLessEqual(len(res_add.display_text), SHOPPING_DISPLAY_LIMIT)
        self.assertIn("…", res_add.display_text)

        async with self.SessionLocal() as session:
            ul = await session.scalar(select(UserList).where(UserList.id == res_add.payload["list_id"]))
            self.assertIsNotNone(ul)
            self.assertEqual(ul.name, long_name)

        # Confirmed clear
        res_clear = await execute_tool(
            "clear_bought_items",
            {"list_id": res_add.payload["list_id"]},
            chat_id=101,
            user_id=1,
            execute_mutation=True,
        )
        self.assertTrue(res_clear.payload["success"])
        self.assertLessEqual(len(res_clear.display_text), SHOPPING_DISPLAY_LIMIT)
        self.assertIn("…", res_clear.display_text)


if __name__ == "__main__":
    unittest.main()
