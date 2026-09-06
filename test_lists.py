import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, text, inspect
from bot.database.models import Base, UserList, ListItem
from bot.utils.lists import (
    LIST_TYPE_SHOPPING,
    DEFAULT_SHOPPING_LIST_NAME,
    create_or_get_user_list,
    get_user_list,
    list_user_lists,
    resolve_user_list,
    list_list_items,
    add_list_items,
    set_list_item_done,
    delete_list_item,
    clear_done_list_items,
    find_existing_user_list,
    get_list_item,
    delete_user_list,
)


class TestListsDBLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.lists.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()

    # 1. Base.metadata.create_all створює user_lists і list_items
    async def test_1_create_all_creates_both_tables(self):
        async with self.engine.connect() as conn:
            def _check_tables(sync_conn):
                inspector = inspect(sync_conn)
                return inspector.get_table_names()

            tables = await conn.run_sync(_check_tables)
            self.assertIn("user_lists", tables)
            self.assertIn("list_items", tables)

    # 2. Поля UserList/ListItem, timestamps і author IDs реально зберігаються
    async def test_2_model_fields_timestamps_and_authors_persist(self):
        user_list, created = await create_or_get_user_list(
            user_id=101,
            chat_id=-202,
            list_type=LIST_TYPE_SHOPPING,
            name="Продукти на свято",
        )
        self.assertIsNotNone(user_list.id)
        self.assertEqual(user_list.chat_id, -202)
        self.assertEqual(user_list.list_type, LIST_TYPE_SHOPPING)
        self.assertEqual(user_list.name, "Продукти на свято")
        self.assertEqual(user_list.normalized_name, "продукти на свято")
        self.assertEqual(user_list.created_by_user_id, 101)
        self.assertIsNotNone(user_list.created_at)
        self.assertIsNotNone(user_list.updated_at)
        self.assertEqual(user_list.created_at.tzinfo, timezone.utc)
        self.assertEqual(user_list.updated_at.tzinfo, timezone.utc)

        items = await add_list_items(
            list_id=user_list.id,
            chat_id=-202,
            actor_user_id=303,
            items=["Морква", "Картопля"],
        )
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 2)
        item = items[0]
        self.assertIsNotNone(item.id)
        self.assertEqual(item.list_id, user_list.id)
        self.assertEqual(item.text, "Морква")
        self.assertFalse(item.is_done)
        self.assertEqual(item.created_by_user_id, 303)
        self.assertEqual(item.updated_by_user_id, 303)
        self.assertIsNotNone(item.created_at)
        self.assertIsNotNone(item.updated_at)
        self.assertEqual(item.created_at.tzinfo, timezone.utc)
        self.assertEqual(item.updated_at.tzinfo, timezone.utc)

    # 3. Нормалізація whitespace і Unicode casefold для назв
    async def test_3_normalization_whitespace_and_casefold(self):
        ul1, created1 = await create_or_get_user_list(
            user_id=1,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            name="   Молоко \t  і   ХЛІБ   \n",
        )
        self.assertEqual(ul1.name, "Молоко і ХЛІБ")
        self.assertEqual(ul1.normalized_name, "молоко і хліб")

        ul2, created2 = await create_or_get_user_list(
            user_id=2,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            name="молоко   І   хліб",
        )
        self.assertFalse(created2)
        self.assertEqual(ul2.id, ul1.id)

    # 4. Same chat/type/normalized name повторно використовує список
    async def test_4_same_chat_type_normalized_name_reuses_list(self):
        ul1, c1 = await create_or_get_user_list(user_id=1, chat_id=100, list_type=LIST_TYPE_SHOPPING, name="Ринок")
        self.assertTrue(c1)

        ul2, c2 = await create_or_get_user_list(user_id=2, chat_id=100, list_type=LIST_TYPE_SHOPPING, name="РИНОК")
        self.assertFalse(c2)
        self.assertEqual(ul1.id, ul2.id)

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(UserList.id)).where(UserList.chat_id == 100))
            self.assertEqual(count, 1)

    # 5. Різні назви, chat_id або list_type не змішуються
    async def test_5_different_names_chats_types_do_not_mix(self):
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=100, list_type=LIST_TYPE_SHOPPING, name="Список А")
        ul2, _ = await create_or_get_user_list(user_id=1, chat_id=100, list_type=LIST_TYPE_SHOPPING, name="Список Б")
        ul3, _ = await create_or_get_user_list(user_id=1, chat_id=200, list_type=LIST_TYPE_SHOPPING, name="Список А")

        self.assertNotEqual(ul1.id, ul2.id)
        self.assertNotEqual(ul1.id, ul3.id)
        self.assertNotEqual(ul2.id, ul3.id)

        with self.assertRaises(ValueError):
            await create_or_get_user_list(user_id=1, chat_id=100, list_type="todo", name="Непідтримуваний")

    # 6. Груповий chat: різні actor user IDs отримують той самий список
    async def test_6_group_chat_different_actors_same_list(self):
        group_chat_id = -100987654321
        ul_alice, c1 = await create_or_get_user_list(
            user_id=111, chat_id=group_chat_id, list_type=LIST_TYPE_SHOPPING, name="Офіс",
        )
        self.assertTrue(c1)
        self.assertEqual(ul_alice.created_by_user_id, 111)

        ul_bob, c2 = await create_or_get_user_list(
            user_id=222, chat_id=group_chat_id, list_type=LIST_TYPE_SHOPPING, name="Офіс",
        )
        self.assertFalse(c2)
        self.assertEqual(ul_bob.id, ul_alice.id)
        self.assertEqual(ul_bob.created_by_user_id, 111)

    # 7. Cross-chat isolation для get/list/items
    async def test_7_cross_chat_isolation(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Секретний")
        await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=["Секрет"])

        self.assertIsNone(await get_user_list(ul.id, chat_id=20))
        foreign_lists = await list_user_lists(chat_id=20, list_type=LIST_TYPE_SHOPPING)
        self.assertEqual(len(foreign_lists), 0)
        self.assertIsNone(await list_list_items(ul.id, chat_id=20))

    # 8. explicit_name має найвищий пріоритет і створює named list за відсутності
    async def test_8_resolve_explicit_name_priority(self):
        ul_default, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name=DEFAULT_SHOPPING_LIST_NAME)

        ul_resolved, created = await resolve_user_list(
            user_id=1,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            explicit_name="Вечірка",
            current_list_id=ul_default.id,
        )
        self.assertTrue(created)
        self.assertEqual(ul_resolved.name, "Вечірка")
        self.assertNotEqual(ul_resolved.id, ul_default.id)

        # Calling again with explicit_name reuses it
        ul_resolved_2, created_2 = await resolve_user_list(
            user_id=1,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            explicit_name="вечірка",
            current_list_id=ul_default.id,
        )
        self.assertFalse(created_2)
        self.assertEqual(ul_resolved_2.id, ul_resolved.id)

    # 9. Валідний current_list_id має другий пріоритет
    async def test_9_resolve_valid_current_list_id_priority(self):
        ul_a, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Список 1")
        ul_b, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Список 2")

        res, created = await resolve_user_list(
            user_id=1,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            explicit_name=None,
            current_list_id=ul_b.id,
        )
        self.assertFalse(created)
        self.assertEqual(res.id, ul_b.id)

        # Invalid or foreign current_list_id safely falls through
        foreign_list, _ = await create_or_get_user_list(user_id=1, chat_id=999, list_type=LIST_TYPE_SHOPPING, name="Чужий")
        res_fallback, _ = await resolve_user_list(
            user_id=1,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            explicit_name=None,
            current_list_id=foreign_list.id,
        )
        # In chat 10 there are 2 lists, neither is default "Покупки", so fallback creates default "Покупки"
        self.assertEqual(res_fallback.name, DEFAULT_SHOPPING_LIST_NAME)

    # 10. Єдиний список потрібного типу використовується як третій fallback
    async def test_10_resolve_single_list_fallback(self):
        single, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Мій Єдиний")

        res, created = await resolve_user_list(
            user_id=1,
            chat_id=10,
            list_type=LIST_TYPE_SHOPPING,
            explicit_name=None,
            current_list_id=None,
        )
        self.assertFalse(created)
        self.assertEqual(res.id, single.id)
        self.assertEqual(res.name, "Мій Єдиний")

    # 11. За відсутності списків створюється default "Покупки"
    async def test_11_resolve_empty_chat_creates_default(self):
        res, created = await resolve_user_list(
            user_id=1,
            chat_id=55,
            list_type=LIST_TYPE_SHOPPING,
            explicit_name=None,
            current_list_id=None,
        )
        self.assertTrue(created)
        self.assertEqual(res.name, DEFAULT_SHOPPING_LIST_NAME)
        self.assertEqual(res.normalized_name, DEFAULT_SHOPPING_LIST_NAME.casefold())

    # 12. За наявності кількох списків reuse/create default відбувається детерміновано
    async def test_12_resolve_multiple_lists_deterministic_default(self):
        await create_or_get_user_list(user_id=1, chat_id=77, list_type=LIST_TYPE_SHOPPING, name="Фрукти")
        await create_or_get_user_list(user_id=1, chat_id=77, list_type=LIST_TYPE_SHOPPING, name="Овочі")

        # Fallback creates default "Покупки" because none exists yet
        def_created, c1 = await resolve_user_list(user_id=1, chat_id=77, list_type=LIST_TYPE_SHOPPING)
        self.assertTrue(c1)
        self.assertEqual(def_created.name, DEFAULT_SHOPPING_LIST_NAME)

        # Now chat 77 has 3 lists including "Покупки". Calling again reuses "Покупки"
        def_reused, c2 = await resolve_user_list(user_id=1, chat_id=77, list_type=LIST_TYPE_SHOPPING)
        self.assertFalse(c2)
        self.assertEqual(def_reused.id, def_created.id)

        lists = await list_user_lists(chat_id=77, list_type=LIST_TYPE_SHOPPING)
        self.assertEqual(len(lists), 3)

    # 14. Batch add додає всі пункти однією транзакцією та зберігає input order
    async def test_14_batch_add_preserves_order_and_transaction(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Тест")
        items_input = ["Хліб", "Масло", "Кава", "Цукор"]
        created_items = await add_list_items(
            list_id=ul.id,
            chat_id=10,
            actor_user_id=101,
            items=items_input,
        )
        self.assertIsNotNone(created_items)
        self.assertEqual(len(created_items), 4)
        self.assertEqual([it.text for it in created_items], items_input)
        self.assertTrue(all(it.created_by_user_id == 101 for it in created_items))
        self.assertTrue(all(it.updated_by_user_id == 101 for it in created_items))

        # Query items to verify ordering (is_done ASC, id ASC)
        fetched = await list_list_items(ul.id, chat_id=10)
        self.assertEqual([it.text for it in fetched], items_input)

    # 15. Однакові item texts дозволені
    async def test_15_duplicate_item_texts_allowed(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Повтори")
        duplicates = ["Яблука", "Яблука", "Яблука"]
        created = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=duplicates)
        self.assertIsNotNone(created)
        self.assertEqual(len(created), 3)
        self.assertEqual(len(set(it.id for it in created)), 3)

    # 16. Невалідний один item блокує весь batch до DB write
    async def test_16_invalid_item_blocks_entire_batch(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Валідація")

        with self.assertRaises(ValueError):
            await add_list_items(
                list_id=ul.id,
                chat_id=10,
                actor_user_id=1,
                items=["Яблука", "   ", "Банани"],
            )

        with self.assertRaises(ValueError):
            await add_list_items(
                list_id=ul.id,
                chat_id=10,
                actor_user_id=1,
                items=["Яблука", 12345, "Банани"],  # type: ignore
            )

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(ListItem.id)).where(ListItem.list_id == ul.id))
            self.assertEqual(count, 0)

    # 17. Foreign chat не може додати або прочитати items
    async def test_17_foreign_chat_cannot_add_or_read_items(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Приватний")

        add_res = await add_list_items(list_id=ul.id, chat_id=999, actor_user_id=1, items=["Піца"])
        self.assertIsNone(add_res)

        items = await list_list_items(list_id=ul.id, chat_id=999)
        self.assertIsNone(items)

        # Valid chat can read
        valid_items = await list_list_items(list_id=ul.id, chat_id=10)
        self.assertEqual(valid_items, [])

    # 18. set done та set undone працюють і оновлюють updated_by_user_id
    async def test_18_set_done_and_undone_updates_author(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Справи")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=10, items=["Завдання 1"])
        item_id = items[0].id

        # Mark done by actor 20
        done_item, transitioned1 = await set_list_item_done(item_id, chat_id=10, actor_user_id=20, is_done=True)
        self.assertTrue(transitioned1)
        self.assertIsNotNone(done_item)
        self.assertTrue(done_item.is_done)
        self.assertEqual(done_item.updated_by_user_id, 20)

        # Mark undone by actor 30
        undone_item, transitioned2 = await set_list_item_done(item_id, chat_id=10, actor_user_id=30, is_done=False)
        self.assertTrue(transitioned2)
        self.assertIsNotNone(undone_item)
        self.assertFalse(undone_item.is_done)
        self.assertEqual(undone_item.updated_by_user_id, 30)

    # 19. Repeated set to same state ідемпотентний
    async def test_19_repeated_set_same_state_idempotent(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Ідемпотентність")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=10, items=["Пункт"])
        item_id = items[0].id

        # First transition
        _, tr1 = await set_list_item_done(item_id, chat_id=10, actor_user_id=20, is_done=True)
        self.assertTrue(tr1)

        # Repeat same transition
        item_repeat, tr2 = await set_list_item_done(item_id, chat_id=10, actor_user_id=30, is_done=True)
        self.assertFalse(tr2)
        self.assertIsNotNone(item_repeat)
        self.assertTrue(item_repeat.is_done)
        self.assertEqual(item_repeat.updated_by_user_id, 20)  # Preserves previous updater

    # 20. Foreign chat/nonexistent item не змінюються
    async def test_20_foreign_chat_nonexistent_item_not_modified(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Захист")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=10, items=["Пункт"])
        item_id = items[0].id

        res_foreign, tr_foreign = await set_list_item_done(item_id, chat_id=999, actor_user_id=20, is_done=True)
        self.assertIsNone(res_foreign)
        self.assertFalse(tr_foreign)

        res_nonexist, tr_nonexist = await set_list_item_done(99999, chat_id=10, actor_user_id=20, is_done=True)
        self.assertIsNone(res_nonexist)
        self.assertFalse(tr_nonexist)

        # Ensure ground-truth item in DB is untouched
        items_db = await list_list_items(ul.id, chat_id=10)
        self.assertFalse(items_db[0].is_done)

    # 23. delete item exact-scope та idempotent
    async def test_23_delete_item_exact_scope_and_idempotent(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Видалення")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=["Видали мене"])
        item_id = items[0].id

        # Foreign chat cannot delete
        del_foreign = await delete_list_item(item_id=item_id, chat_id=999, actor_user_id=2)
        self.assertFalse(del_foreign)

        # Exact chat deletes
        del_own = await delete_list_item(item_id=item_id, chat_id=10, actor_user_id=1)
        self.assertTrue(del_own)

        # Repeated delete returns False
        del_repeat = await delete_list_item(item_id=item_id, chat_id=10, actor_user_id=1)
        self.assertFalse(del_repeat)

    # 24. clear done видаляє тільки completed items одним bulk operation
    async def test_24_clear_done_bulk_operation(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Очистка")
        items = await add_list_items(
            list_id=ul.id, chat_id=10, actor_user_id=1, items=["Активний 1", "Готовий 1", "Активний 2", "Готовий 2"],
        )
        await set_list_item_done(items[1].id, chat_id=10, actor_user_id=1, is_done=True)
        await set_list_item_done(items[3].id, chat_id=10, actor_user_id=1, is_done=True)

        cleared_count = await clear_done_list_items(list_id=ul.id, chat_id=10, actor_user_id=1)
        self.assertEqual(cleared_count, 2)

        remaining = await list_list_items(ul.id, chat_id=10)
        self.assertEqual(len(remaining), 2)
        self.assertEqual([it.text for it in remaining], ["Активний 1", "Активний 2"])

        # Clearing again returns 0
        cleared_again = await clear_done_list_items(list_id=ul.id, chat_id=10, actor_user_id=1)
        self.assertEqual(cleared_again, 0)

    # 25. clear done для чужого list/chat повертає None і нічого не видаляє
    async def test_25_clear_done_foreign_list_or_chat(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Очистка чужого")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=["Готовий"])
        await set_list_item_done(items[0].id, chat_id=10, actor_user_id=1, is_done=True)

        # Foreign chat
        res_foreign = await clear_done_list_items(list_id=ul.id, chat_id=999, actor_user_id=1)
        self.assertIsNone(res_foreign)

        # Nonexistent list
        res_nonexist = await clear_done_list_items(list_id=99999, chat_id=10, actor_user_id=1)
        self.assertIsNone(res_nonexist)

        # Ensure items untouched
        items_db = await list_list_items(ul.id, chat_id=10)
        self.assertEqual(len(items_db), 1)
        self.assertTrue(items_db[0].is_done)

    # 26. Жодні AI, scheduler, Telegram або reminder side effects не викликаються
    async def test_26_no_side_effects_invoked(self):
        mock_exec = AsyncMock()
        mock_add_rem = AsyncMock()
        mock_del_rem = AsyncMock()

        with patch("bot.ai.tools.execute_tool", mock_exec), \
             patch("bot.utils.scheduler.scheduler_service.add_reminder", mock_add_rem), \
             patch("bot.utils.scheduler.scheduler_service.delete_reminder_by_id", mock_del_rem):

            ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Списочок")
            await resolve_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING)
            items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=["Тестовий пункт"])
            await set_list_item_done(items[0].id, chat_id=10, actor_user_id=1, is_done=True)
            await clear_done_list_items(list_id=ul.id, chat_id=10, actor_user_id=1)
            await delete_list_item(items[0].id, chat_id=10, actor_user_id=1)

            mock_exec.assert_not_called()
            mock_add_rem.assert_not_called()
            mock_del_rem.assert_not_called()

    # 27. Логи не містять sentinel list name/item text
    async def test_27_logs_do_not_contain_sentinel_names_or_texts(self):
        sentinel_name = "TOP_SECRET_LIST_NAME_12345"
        sentinel_text = "TOP_SECRET_ITEM_TEXT_67890"

        with self.assertLogs("bot.utils.lists", level="INFO") as log_capture:
            ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name=sentinel_name)
            items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=[sentinel_text])
            await set_list_item_done(items[0].id, chat_id=10, actor_user_id=1, is_done=True)
            await clear_done_list_items(list_id=ul.id, chat_id=10, actor_user_id=1)
            await delete_list_item(items[0].id, chat_id=10, actor_user_id=1)

        combined_logs = "\n".join(log_capture.output)
        self.assertNotIn(sentinel_name, combined_logs)
        self.assertNotIn(sentinel_text, combined_logs)

    # 28. find_existing_user_list lookup and fallbacks
    async def test_28_find_existing_user_list(self):
        # Empty chat returns None
        self.assertIsNone(await find_existing_user_list(chat_id=500))

        # 1 list in chat -> returned for no name
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=500, list_type=LIST_TYPE_SHOPPING, name="Мій Список")
        found = await find_existing_user_list(chat_id=500)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, ul1.id)

        # Explicit name match case-insensitive / trimmed
        found_explicit = await find_existing_user_list(chat_id=500, list_name="   мій список  ")
        self.assertIsNotNone(found_explicit)
        self.assertEqual(found_explicit.id, ul1.id)

        # Missing explicit name returns None
        self.assertIsNone(await find_existing_user_list(chat_id=500, list_name="Невідомий"))

        # Add second list without default name
        ul2, _ = await create_or_get_user_list(user_id=1, chat_id=500, list_type=LIST_TYPE_SHOPPING, name="Другий Список")
        # With 2 lists and no default "Покупки", fallback without name returns None
        self.assertIsNone(await find_existing_user_list(chat_id=500))

        # Add default list "Покупки"
        ul_default, _ = await create_or_get_user_list(user_id=1, chat_id=500, list_type=LIST_TYPE_SHOPPING, name=DEFAULT_SHOPPING_LIST_NAME)
        # Now fallback returns "Покупки"
        found_def = await find_existing_user_list(chat_id=500)
        self.assertIsNotNone(found_def)
        self.assertEqual(found_def.id, ul_default.id)

    # 29. get_list_item chat isolation and validation
    async def test_29_get_list_item(self):
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=601, list_type=LIST_TYPE_SHOPPING, name="Чат 1")
        items = await add_list_items(ul1.id, 601, 1, ["Хліб", "Масло"])
        self.assertIsNotNone(items)

        # Found in exact chat
        item = await get_list_item(items[0].id, 601)
        self.assertIsNotNone(item)
        self.assertEqual(item.id, items[0].id)
        self.assertEqual(item.text, "Хліб")

        # Foreign chat returns None
        self.assertIsNone(await get_list_item(items[0].id, 602))

        # Non-existent item ID returns None
        self.assertIsNone(await get_list_item(99999, 601))


class TestListsConcurrency(unittest.IsolatedAsyncioTestCase):
    """Concurrency tests using file-backed SQLite in WAL mode."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp_dir.name, "lists_concurrency.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
            await conn.execute(text("PRAGMA busy_timeout=5000;"))
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.lists.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    # 13. Concurrent default resolution, мінімум 5 одночасних викликів
    async def test_13_concurrent_default_resolution(self):
        chat_id = 9999
        concurrency_count = 8

        # Run 8 concurrent resolve calls on an empty chat
        tasks = [
            resolve_user_list(
                user_id=i + 1,
                chat_id=chat_id,
                list_type=LIST_TYPE_SHOPPING,
                explicit_name=None,
                current_list_id=None,
            )
            for i in range(concurrency_count)
        ]
        results = await asyncio.gather(*tasks)

        created_flags = [r[1] for r in results]
        list_ids = [r[0].id for r in results]

        # Exactly one call should have created=True
        self.assertEqual(created_flags.count(True), 1)
        self.assertEqual(created_flags.count(False), concurrency_count - 1)

        # All results must reference the exact same list ID
        self.assertEqual(len(set(list_ids)), 1)

        # Ground truth in SQLite: exactly one list exists for chat
        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(UserList.id)).where(UserList.chat_id == chat_id))
            self.assertEqual(count, 1)

    # 21. Concurrent done/done має рівно одного transitioned=True
    async def test_21_concurrent_done_done_single_winner(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Гонка Done")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=["Спільний пункт"])
        item_id = items[0].id

        concurrency_count = 6
        tasks = [
            set_list_item_done(
                item_id=item_id,
                chat_id=10,
                actor_user_id=100 + i,
                is_done=True,
            )
            for i in range(concurrency_count)
        ]
        results = await asyncio.gather(*tasks)

        transitioned_flags = [r[1] for r in results]
        self.assertEqual(transitioned_flags.count(True), 1)
        self.assertEqual(transitioned_flags.count(False), concurrency_count - 1)

        # Ground truth in SQLite
        async with self.SessionLocal() as session:
            db_item = await session.get(ListItem, item_id)
            self.assertTrue(db_item.is_done)

    # 22. Concurrent done/undone не створює нових рядків і лишає валідний final state
    async def test_22_concurrent_done_undone_valid_state_no_extra_rows(self):
        ul, _ = await create_or_get_user_list(user_id=1, chat_id=10, list_type=LIST_TYPE_SHOPPING, name="Гонка Перемикань")
        items = await add_list_items(list_id=ul.id, chat_id=10, actor_user_id=1, items=["Спірний пункт"])
        item_id = items[0].id

        tasks = [
            set_list_item_done(item_id, chat_id=10, actor_user_id=1, is_done=True),
            set_list_item_done(item_id, chat_id=10, actor_user_id=2, is_done=False),
            set_list_item_done(item_id, chat_id=10, actor_user_id=3, is_done=True),
            set_list_item_done(item_id, chat_id=10, actor_user_id=4, is_done=False),
        ]
        results = await asyncio.gather(*tasks)

        # None of the results should be (None, False)
        for item, _ in results:
            self.assertIsNotNone(item)

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(ListItem.id)).where(ListItem.list_id == ul.id))
            self.assertEqual(count, 1)

            final_item = await session.get(ListItem, item_id)
            self.assertIsInstance(final_item.is_done, bool)

    # Regression 1: Batch retry before commit does not duplicate items
    async def test_add_list_items_retry_before_commit_does_not_duplicate(self):
        """Verify OperationalError during pre-commit refresh rolls back and retry does not duplicate items."""
        from sqlalchemy.exc import OperationalError

        ul, _ = await create_or_get_user_list(
            user_id=1, chat_id=500, list_type=LIST_TYPE_SHOPPING, name="Пакетні покупки",
        )

        error_injected = False
        original_sessionmaker = self.SessionLocal

        def failing_refresh_sessionmaker(*args, **kwargs):
            session = original_sessionmaker(*args, **kwargs)
            real_refresh = session.refresh

            async def hooked_refresh(instance, *r_args, **r_kwargs):
                nonlocal error_injected
                if not error_injected:
                    error_injected = True
                    raise OperationalError("SELECT ...", {}, Exception("database is locked"))
                return await real_refresh(instance, *r_args, **r_kwargs)

            session.refresh = hooked_refresh
            return session

        with patch("bot.utils.lists.AsyncSessionLocal", failing_refresh_sessionmaker):
            input_items = ["Хліб", "Молоко", "Масло"]
            result = await add_list_items(
                list_id=ul.id,
                chat_id=500,
                actor_user_id=1,
                items=input_items,
            )

        self.assertTrue(error_injected)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), len(input_items))
        self.assertEqual([item.text for item in result], input_items)

        # Ground truth in SQLite must contain exactly one copy of the batch
        async with self.SessionLocal() as session:
            persisted = (
                await session.scalars(
                    select(ListItem)
                    .where(ListItem.list_id == ul.id)
                    .order_by(ListItem.id.asc())
                )
            ).all()
            self.assertEqual(len(persisted), 3)
            self.assertEqual([item.text for item in persisted], input_items)

    # Regression 2: Winning set_list_item_done still reports transitioned=True after a retryable pre-commit read failure
    async def test_set_list_item_done_retry_before_commit_preserves_transition_flag(self):
        """Verify OperationalError during pre-commit read rolls back and retry preserves transitioned=True."""
        from sqlalchemy.exc import OperationalError
        from sqlalchemy.sql.dml import Update
        from sqlalchemy.sql.selectable import Select

        ul, _ = await create_or_get_user_list(
            user_id=1, chat_id=600, list_type=LIST_TYPE_SHOPPING, name="Пункт для переходу",
        )
        items = await add_list_items(
            list_id=ul.id,
            chat_id=600,
            actor_user_id=10,
            items=["Активний пункт"],
        )
        self.assertIsNotNone(items)
        item_id = items[0].id

        error_injected = False
        original_sessionmaker = self.SessionLocal

        def failing_fetch_sessionmaker(*args, **kwargs):
            session = original_sessionmaker(*args, **kwargs)
            real_execute = session.execute
            saw_update_in_session = False

            async def hooked_execute(statement, *exec_args, **exec_kwargs):
                nonlocal error_injected, saw_update_in_session
                if isinstance(statement, Update):
                    saw_update_in_session = True
                    return await real_execute(statement, *exec_args, **exec_kwargs)

                if saw_update_in_session and isinstance(statement, Select) and not error_injected:
                    error_injected = True
                    raise OperationalError("SELECT ...", {}, Exception("database is locked"))

                return await real_execute(statement, *exec_args, **exec_kwargs)

            session.execute = hooked_execute
            return session

        with patch("bot.utils.lists.AsyncSessionLocal", failing_fetch_sessionmaker):
            transitioned_item, won = await set_list_item_done(
                item_id=item_id,
                chat_id=600,
                actor_user_id=25,
                is_done=True,
            )

        self.assertTrue(error_injected)
        self.assertTrue(won)
        self.assertIsNotNone(transitioned_item)
        self.assertTrue(transitioned_item.is_done)
        self.assertEqual(transitioned_item.updated_by_user_id, 25)

        # Ground truth in SQLite
        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(ListItem.id)).where(ListItem.list_id == ul.id))
            self.assertEqual(count, 1)

            final_row = await session.get(ListItem, item_id)
            self.assertIsNotNone(final_row)
            self.assertTrue(final_row.is_done)
            self.assertEqual(final_row.updated_by_user_id, 25)

    # Regression 3: clear_done_list_items scope regression
    async def test_clear_done_list_items_scope_hardening(self):
        """Verify clear_done_list_items enforces exact chat scope in DELETE and isolates chats."""
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=701, list_type=LIST_TYPE_SHOPPING, name="Чат 1")
        ul2, _ = await create_or_get_user_list(user_id=2, chat_id=702, list_type=LIST_TYPE_SHOPPING, name="Чат 2")

        # Chat 1: 1 active, 2 done
        c1_items = await add_list_items(ul1.id, 701, 1, ["Акт1", "Готово1_1", "Готово1_2"])
        self.assertIsNotNone(c1_items)
        await set_list_item_done(c1_items[1].id, 701, 1, is_done=True)
        await set_list_item_done(c1_items[2].id, 701, 1, is_done=True)

        # Chat 2: 1 active, 2 done
        c2_items = await add_list_items(ul2.id, 702, 2, ["Акт2", "Готово2_1", "Готово2_2"])
        self.assertIsNotNone(c2_items)
        await set_list_item_done(c2_items[1].id, 702, 2, is_done=True)
        await set_list_item_done(c2_items[2].id, 702, 2, is_done=True)

        # Foreign list/chat call returns None and deletes nothing
        foreign_clear = await clear_done_list_items(list_id=ul1.id, chat_id=702, actor_user_id=1)
        self.assertIsNone(foreign_clear)

        # Clear Chat 1
        cleared_count = await clear_done_list_items(list_id=ul1.id, chat_id=701, actor_user_id=1)
        self.assertEqual(cleared_count, 2)

        # Chat 1: only active item remains
        remaining_c1 = await list_list_items(ul1.id, 701)
        self.assertIsNotNone(remaining_c1)
        self.assertEqual(len(remaining_c1), 1)
        self.assertEqual(remaining_c1[0].text, "Акт1")
        self.assertFalse(remaining_c1[0].is_done)

        # Chat 2: all items remain intact (1 active, 2 done)
        remaining_c2 = await list_list_items(ul2.id, 702)
        self.assertIsNotNone(remaining_c2)
        self.assertEqual(len(remaining_c2), 3)
        self.assertEqual(sum(1 for it in remaining_c2 if it.is_done), 2)
        self.assertEqual(sum(1 for it in remaining_c2 if not it.is_done), 1)

        # Owned list with no completed items returns 0
        cleared_again = await clear_done_list_items(list_id=ul1.id, chat_id=701, actor_user_id=1)
        self.assertEqual(cleared_again, 0)

    # 26. delete_user_list atomic deletion, chat isolation, and no orphan items
    async def test_delete_user_list_atomic_and_chat_isolation(self):
        ul1, _ = await create_or_get_user_list(user_id=1, chat_id=801, list_type=LIST_TYPE_SHOPPING, name="Список 1")
        ul2, _ = await create_or_get_user_list(user_id=1, chat_id=801, list_type=LIST_TYPE_SHOPPING, name="Список 2")
        ul_other, _ = await create_or_get_user_list(user_id=2, chat_id=802, list_type=LIST_TYPE_SHOPPING, name="Чужий список")

        await add_list_items(ul1.id, 801, 1, ["Пункт 1.1", "Пункт 1.2"])
        await add_list_items(ul2.id, 801, 1, ["Пункт 2.1"])
        await add_list_items(ul_other.id, 802, 2, ["Чужий пункт"])

        # 1. Foreign chat deletion returns False and deletes nothing
        foreign_del = await delete_user_list(list_id=ul1.id, chat_id=802, actor_user_id=2)
        self.assertFalse(foreign_del)

        # 2. Non-existent list returns False
        non_existent_del = await delete_user_list(list_id=99999, chat_id=801, actor_user_id=1)
        self.assertFalse(non_existent_del)

        # 3. Successful deletion of ul1
        res = await delete_user_list(list_id=ul1.id, chat_id=801, actor_user_id=1)
        self.assertTrue(res)

        # 4. Repeated deletion returns False (idempotent)
        repeated_del = await delete_user_list(list_id=ul1.id, chat_id=801, actor_user_id=1)
        self.assertFalse(repeated_del)

        # Verify ul1 and its items are gone from DB, no orphan items
        async with self.SessionLocal() as session:
            ul1_db = await session.get(UserList, ul1.id)
            self.assertIsNone(ul1_db)
            ul1_items_count = await session.scalar(
                select(func.count(ListItem.id)).where(ListItem.list_id == ul1.id)
            )
            self.assertEqual(ul1_items_count, 0)

            # ul2 and its items remain intact
            ul2_db = await session.get(UserList, ul2.id)
            self.assertIsNotNone(ul2_db)
            ul2_items = (await session.scalars(select(ListItem).where(ListItem.list_id == ul2.id))).all()
            self.assertEqual(len(ul2_items), 1)

            # ul_other and its items in chat 802 remain intact
            ul_other_db = await session.get(UserList, ul_other.id)
            self.assertIsNotNone(ul_other_db)
            other_items = (await session.scalars(select(ListItem).where(ListItem.list_id == ul_other.id))).all()
            self.assertEqual(len(other_items), 1)

    # 27. delete_user_list argument validation
    async def test_delete_user_list_validation(self):
        invalid_cases = [
            (None, 801, 1),
            ("1", 801, 1),
            (True, 801, 1),
            (0, 801, 1),
            (-1, 801, 1),
            (1, None, 1),
            (1, "801", 1),
            (1, True, 1),
            (1, 0, 1),
            (1, 801, None),
            (1, 801, "1"),
            (1, 801, True),
            (1, 801, 0),
            (1, 801, -1),
        ]
        for lid, cid, aid in invalid_cases:
            with self.subTest(lid=lid, cid=cid, aid=aid):
                with self.assertRaises(ValueError):
                    await delete_user_list(lid, cid, aid)
