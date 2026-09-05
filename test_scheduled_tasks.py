import os
import sys
import asyncio
import unittest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import tempfile

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, text, inspect
from bot.database.models import Base, ScheduledTask, TaskOccurrence
from bot.utils.scheduled_tasks import (
    create_scheduled_task,
    get_scheduled_task,
    list_active_scheduled_tasks,
    deactivate_scheduled_task,
    get_or_create_task_occurrence,
    get_task_occurrence,
    claim_task_occurrence_for_delivery,
    complete_task_occurrence_delivery,
    transition_task_occurrence_terminal,
    snooze_task_occurrence,
    CONTEXT_TYPE_MEDICATION,
    CONTEXT_TYPE_GENERIC,
    OCCURRENCE_STATUS_SCHEDULED,
    OCCURRENCE_STATUS_DELIVERED,
    OCCURRENCE_STATUS_DONE,
    OCCURRENCE_STATUS_SKIPPED,
    OCCURRENCE_STATUS_SNOOZED,
)


class TestScheduledTasks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.scheduled_tasks.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()

    # 1. Base.metadata.create_all створює обидві нові таблиці
    async def test_1_create_all_creates_both_tables(self):
        """Verify Base.metadata.create_all creates scheduled_tasks and task_occurrences tables."""
        async with self.engine.connect() as conn:
            def _check_tables(sync_conn):
                inspector = inspect(sync_conn)
                tables = inspector.get_table_names()
                return tables

            tables = await conn.run_sync(_check_tables)
            self.assertIn("scheduled_tasks", tables)
            self.assertIn("task_occurrences", tables)

    # 2. ScheduledTask зберігає всі поля та timestamps
    async def test_2_scheduled_task_field_persistence_and_timestamps(self):
        """Verify ScheduledTask persists all columns, flags, and UTC timestamps."""
        task = await create_scheduled_task(
            user_id=123,
            chat_id=-100456,
            context_type="medication",
            name="Aspirin",
            local_time="08:30",
            timezone_name="Europe/Kyiv",
            days_of_week=[0, 2, 4],
            details="Take with meal",
            dosage="100mg",
        )

        self.assertIsNotNone(task.id)
        self.assertEqual(task.user_id, 123)
        self.assertEqual(task.chat_id, -100456)
        self.assertEqual(task.context_type, "medication")
        self.assertEqual(task.name, "Aspirin")
        self.assertEqual(task.local_time, "08:30")
        self.assertEqual(task.timezone, "Europe/Kyiv")
        self.assertEqual(task.days_of_week, [0, 2, 4])
        self.assertEqual(task.details, "Take with meal")
        self.assertEqual(task.dosage, "100mg")
        self.assertTrue(task.active)
        self.assertIsNotNone(task.created_at)
        self.assertIsNotNone(task.updated_at)
        self.assertIsNotNone(task.created_at.tzinfo)

    # 3. local_time канонізується до HH:MM
    async def test_3_local_time_canonicalization(self):
        """Verify local_time canonicalization to HH:MM format and rejection of invalid times."""
        t1 = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="Task 1", local_time="8:05", timezone_name="UTC", days_of_week=[0],
        )
        self.assertEqual(t1.local_time, "08:05")

        t2 = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="Task 2", local_time="0:00", timezone_name="UTC", days_of_week=[0],
        )
        self.assertEqual(t2.local_time, "00:00")

        t3 = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="Task 3", local_time="23:59", timezone_name="UTC", days_of_week=[0],
        )
        self.assertEqual(t3.local_time, "23:59")

        invalid_times = ["24:00", "12:60", "25:00", "invalid", "12", "12:34:56", 123, None]
        for inv in invalid_times:
            with self.subTest(local_time=inv):
                with self.assertRaises(ValueError):
                    await create_scheduled_task(
                        user_id=1, chat_id=1, context_type="generic",
                        name="Invalid", local_time=inv, timezone_name="UTC", days_of_week=[0],
                    )

    # 4. days_of_week дедуплікуються й сортуються
    async def test_4_days_of_week_deduplication_and_sorting(self):
        """Verify days_of_week are deduplicated, sorted, and validated (0..6)."""
        t = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="Task", local_time="10:00", timezone_name="UTC",
            days_of_week=[4, 1, 4, 0, 6, 1],
        )
        self.assertEqual(t.days_of_week, [0, 1, 4, 6])

        # Tuple input
        t_tuple = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="Task Tuple", local_time="10:00", timezone_name="UTC",
            days_of_week=(5, 2, 2),
        )
        self.assertEqual(t_tuple.days_of_week, [2, 5])

        # Invalid days_of_week
        invalid_days = [[], [-1], [7], [True], [0, 1, "2"], "0,1", None]
        for inv in invalid_days:
            with self.subTest(days=inv):
                with self.assertRaises(ValueError):
                    await create_scheduled_task(
                        user_id=1, chat_id=1, context_type="generic",
                        name="Invalid", local_time="10:00", timezone_name="UTC", days_of_week=inv,
                    )

    # 5. Валідна IANA timezone зберігається без підміни
    async def test_5_valid_iana_timezone_preserved(self):
        """Verify valid IANA timezones are preserved intact and invalid ones rejected."""
        t1 = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="Kyiv Task", local_time="10:00", timezone_name="Europe/Kyiv", days_of_week=[0],
        )
        self.assertEqual(t1.timezone, "Europe/Kyiv")

        t2 = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="generic",
            name="NY Task", local_time="10:00", timezone_name="America/New_York", days_of_week=[0],
        )
        self.assertEqual(t2.timezone, "America/New_York")

        for inv_tz in ["Invalid/Timezone", "", "  ", None, 123]:
            with self.subTest(tz=inv_tz):
                with self.assertRaises(ValueError):
                    await create_scheduled_task(
                        user_id=1, chat_id=1, context_type="generic",
                        name="Invalid", local_time="10:00", timezone_name=inv_tz, days_of_week=[0],
                    )

    # 6. Невалідні user/chat/context/name/time/timezone/days відхиляються до DB write
    async def test_6_invalid_inputs_rejected_before_db_write(self):
        """Verify invalid user/chat/context/name/time/timezone/days fail before DB write."""
        invalid_cases = [
            {"user_id": -1, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 0, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": True, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 0, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": False, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "invalid_type", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "   ", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": None, "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "25:00", "timezone_name": "UTC", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "Bad/TZ", "days_of_week": [0]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": []},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [7]},
            {"user_id": 1, "chat_id": 1, "context_type": "generic", "name": "N", "local_time": "10:00", "timezone_name": "UTC", "days_of_week": [0], "details": 123},
        ]

        for case in invalid_cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    await create_scheduled_task(**case)

        # Assert no rows written
        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(ScheduledTask.id)))
            self.assertEqual(count, 0)

    # 7. Medication без dosage або з порожнім dosage відхиляється
    async def test_7_medication_requires_non_empty_dosage(self):
        """Verify medication context strictly requires a non-empty dosage."""
        invalid_dosages = [None, "", "   ", 123]
        for d in invalid_dosages:
            with self.subTest(dosage=d):
                with self.assertRaises(ValueError):
                    await create_scheduled_task(
                        user_id=1,
                        chat_id=1,
                        context_type="medication",
                        name="Ibuprofen",
                        local_time="09:00",
                        timezone_name="UTC",
                        days_of_week=[0],
                        dosage=d,
                    )

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(ScheduledTask.id)))
            self.assertEqual(count, 0)

    # 8. Generic task може мати dosage=None
    async def test_8_generic_allows_none_dosage(self):
        """Verify generic context permits dosage=None or optional string dosage."""
        t1 = await create_scheduled_task(
            user_id=1,
            chat_id=1,
            context_type="generic",
            name="Standup meeting",
            local_time="09:30",
            timezone_name="UTC",
            days_of_week=[0, 1, 2, 3, 4],
            dosage=None,
        )
        self.assertIsNone(t1.dosage)

        t2 = await create_scheduled_task(
            user_id=1,
            chat_id=1,
            context_type="generic",
            name="Drink water",
            local_time="10:00",
            timezone_name="UTC",
            days_of_week=[0],
            dosage="500ml",
        )
        self.assertEqual(t2.dosage, "500ml")

    # 9. Exact user/chat ownership для get_scheduled_task
    async def test_9_exact_user_chat_ownership_get_scheduled_task(self):
        """Verify get_scheduled_task enforces exact task_id, user_id, and chat_id matching."""
        task = await create_scheduled_task(
            user_id=10,
            chat_id=20,
            context_type="generic",
            name="Owned Task",
            local_time="12:00",
            timezone_name="UTC",
            days_of_week=[0],
        )

        # Exact match
        found = await get_scheduled_task(task.id, user_id=10, chat_id=20)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, task.id)

        # Foreign user
        self.assertIsNone(await get_scheduled_task(task.id, user_id=999, chat_id=20))

        # Foreign chat
        self.assertIsNone(await get_scheduled_task(task.id, user_id=10, chat_id=999))

        # Both foreign
        self.assertIsNone(await get_scheduled_task(task.id, user_id=999, chat_id=999))

        # Non-existent task ID
        self.assertIsNone(await get_scheduled_task(9999, user_id=10, chat_id=20))

    # 10. list_active_scheduled_tasks не повертає inactive task
    async def test_10_list_active_scheduled_tasks(self):
        """Verify list_active_scheduled_tasks returns only active tasks ordered by ID."""
        t1 = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Task 1", local_time="08:00", timezone_name="UTC", days_of_week=[0],
        )
        t2 = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Task 2", local_time="09:00", timezone_name="UTC", days_of_week=[0],
        )
        t3 = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Task 3", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )

        # Deactivate t2
        await deactivate_scheduled_task(t2.id, user_id=10, chat_id=20)

        active = await list_active_scheduled_tasks()
        active_ids = [t.id for t in active]
        self.assertEqual(active_ids, [t1.id, t3.id])

    # 11. deactivate_scheduled_task є exact-owned та ідемпотентним
    async def test_11_deactivate_scheduled_task_ownership_and_idempotency(self):
        """Verify deactivate_scheduled_task isolates foreign ownership and is idempotent."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Deactivate me", local_time="12:00", timezone_name="UTC", days_of_week=[0],
        )

        # Foreign user cannot deactivate
        self.assertIsNone(await deactivate_scheduled_task(task.id, user_id=999, chat_id=20))
        reloaded = await get_scheduled_task(task.id, user_id=10, chat_id=20)
        self.assertTrue(reloaded.active)

        # Foreign chat cannot deactivate
        self.assertIsNone(await deactivate_scheduled_task(task.id, user_id=10, chat_id=999))
        reloaded = await get_scheduled_task(task.id, user_id=10, chat_id=20)
        self.assertTrue(reloaded.active)

        # Owner deactivates
        res1 = await deactivate_scheduled_task(task.id, user_id=10, chat_id=20)
        self.assertIsNotNone(res1)
        self.assertFalse(res1.active)

        # Idempotent repeat call
        res2 = await deactivate_scheduled_task(task.id, user_id=10, chat_id=20)
        self.assertIsNotNone(res2)
        self.assertFalse(res2.active)

        # Task row is not deleted
        async with self.SessionLocal() as session:
            t = await session.get(ScheduledTask, task.id)
            self.assertIsNotNone(t)
            self.assertFalse(t.active)

    # 12. Новий occurrence отримує planned_at == due_at, status scheduled, telegram_message_id=None
    async def test_12_new_occurrence_defaults(self):
        """Verify new TaskOccurrence default attributes and planned_at == due_at."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="medication",
            name="Vitamins", local_time="08:00", timezone_name="UTC",
            days_of_week=[0], dosage="1 pill",
        )
        planned = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)

        occ, created = await get_or_create_task_occurrence(
            task_id=task.id, user_id=10, chat_id=20, planned_at=planned,
        )
        self.assertTrue(created)
        self.assertIsNotNone(occ)
        self.assertEqual(occ.task_id, task.id)
        self.assertEqual(occ.planned_at, planned)
        self.assertEqual(occ.due_at, planned)
        self.assertEqual(occ.status, OCCURRENCE_STATUS_SCHEDULED)
        self.assertIsNone(occ.telegram_message_id)
        self.assertIsNotNone(occ.created_at)
        self.assertIsNotNone(occ.updated_at)

    # 13. Naive planned_at відхиляється
    async def test_13_naive_planned_at_rejected(self):
        """Verify naive datetime or non-datetime is rejected before any DB write."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Task", local_time="08:00", timezone_name="UTC", days_of_week=[0],
        )

        # Naive datetime
        with self.assertRaises(ValueError):
            await get_or_create_task_occurrence(
                task_id=task.id, user_id=10, chat_id=20,
                planned_at=datetime(2026, 9, 4, 8, 0),
            )

        # Invalid types
        for inv_dt in ["2026-09-04 08:00:00", None, 123, True]:
            with self.subTest(dt=inv_dt):
                with self.assertRaises(ValueError):
                    await get_or_create_task_occurrence(
                        task_id=task.id, user_id=10, chat_id=20,
                        planned_at=inv_dt,
                    )

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(TaskOccurrence.id)))
            self.assertEqual(count, 0)

    # 14. Однаковий UTC instant із різними offsets створює лише один occurrence
    async def test_14_same_utc_instant_different_offsets_returns_same_occurrence(self):
        """Verify the same UTC instant passed with different timezone offsets returns the same occurrence."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Task", local_time="12:00", timezone_name="UTC", days_of_week=[0],
        )

        t_utc = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        # Kyiv is UTC+3 in September: 15:00 Kyiv == 12:00 UTC
        t_kyiv = datetime(2026, 9, 4, 15, 0, tzinfo=ZoneInfo("Europe/Kyiv"))

        occ1, created1 = await get_or_create_task_occurrence(
            task_id=task.id, user_id=10, chat_id=20, planned_at=t_utc,
        )
        self.assertTrue(created1)

        occ2, created2 = await get_or_create_task_occurrence(
            task_id=task.id, user_id=10, chat_id=20, planned_at=t_kyiv,
        )
        self.assertFalse(created2)
        self.assertEqual(occ1.id, occ2.id)

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(TaskOccurrence.id)))
            self.assertEqual(count, 1)

    # 15. Повторний sequential get_or_create повертає той самий ID і created=False
    async def test_15_sequential_get_or_create_idempotent(self):
        """Verify repeated sequential calls return existing occurrence with created=False."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Sequential", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )
        planned = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)

        occ1, created1 = await get_or_create_task_occurrence(task.id, 10, 20, planned)
        self.assertTrue(created1)

        occ2, created2 = await get_or_create_task_occurrence(task.id, 10, 20, planned)
        self.assertFalse(created2)
        self.assertEqual(occ1.id, occ2.id)

    # 17. Різні task або різні planned time створюють різні occurrences
    async def test_17_different_tasks_or_times_create_different_occurrences(self):
        """Verify different tasks or different planned times yield distinct occurrences."""
        task1 = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="T1", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )
        task2 = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="T2", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )

        time1 = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        time2 = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)

        occ1, _ = await get_or_create_task_occurrence(task1.id, 10, 20, time1)
        occ2, _ = await get_or_create_task_occurrence(task1.id, 10, 20, time2)
        occ3, _ = await get_or_create_task_occurrence(task2.id, 10, 20, time1)

        self.assertNotEqual(occ1.id, occ2.id)
        self.assertNotEqual(occ1.id, occ3.id)
        self.assertNotEqual(occ2.id, occ3.id)

    # 18. get_task_occurrence ізолює foreign user і foreign chat
    async def test_18_get_task_occurrence_ownership_isolation(self):
        """Verify get_task_occurrence joins ScheduledTask and enforces ownership."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Iso Task", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )
        planned = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 10, 20, planned)

        # Owner lookup
        found = await get_task_occurrence(occ.id, user_id=10, chat_id=20)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, occ.id)

        # Foreign user
        self.assertIsNone(await get_task_occurrence(occ.id, user_id=999, chat_id=20))

        # Foreign chat
        self.assertIsNone(await get_task_occurrence(occ.id, user_id=10, chat_id=999))

        # Non-existent occurrence ID
        self.assertIsNone(await get_task_occurrence(9999, user_id=10, chat_id=20))

    # 19. Inactive task не дозволяє створити новий occurrence, але старий occurrence залишається доступним exact owner
    async def test_19_inactive_task_cannot_create_new_occurrence_but_old_remains_accessible(self):
        """Verify inactive task prevents new occurrence creation while preserving historical occurrence access."""
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Historical Task", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )
        time1 = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        occ1, created1 = await get_or_create_task_occurrence(task.id, 10, 20, time1)
        self.assertTrue(created1)

        # Deactivate task
        await deactivate_scheduled_task(task.id, user_id=10, chat_id=20)

        # Attempt to create new occurrence for inactive task
        time2 = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
        occ2, created2 = await get_or_create_task_occurrence(task.id, 10, 20, time2)
        self.assertIsNone(occ2)
        self.assertFalse(created2)

        # Old occurrence is still accessible by exact owner
        found = await get_task_occurrence(occ1.id, user_id=10, chat_id=20)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, occ1.id)

    # 20. Жодна DB-lifecycle функція не викликає scheduler, Telegram, AI tools або provider
    async def test_20_no_external_services_invoked(self):
        """Verify DB lifecycle operations do not invoke scheduler, Telegram, AI tools, or providers."""
        mock_tool = AsyncMock()
        mock_add = AsyncMock()
        mock_del = AsyncMock()

        with patch("bot.ai.tools.execute_tool", mock_tool), \
             patch("bot.utils.scheduler.scheduler_service.add_reminder", mock_add), \
             patch("bot.utils.scheduler.scheduler_service.delete_reminder_by_id", mock_del):

            task = await create_scheduled_task(
                user_id=10, chat_id=20, context_type="generic",
                name="Side effect check", local_time="10:00", timezone_name="UTC", days_of_week=[0],
            )
            await get_scheduled_task(task.id, 10, 20)
            await list_active_scheduled_tasks()

            planned = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
            occ, _ = await get_or_create_task_occurrence(task.id, 10, 20, planned)
            await get_task_occurrence(occ.id, 10, 20)
            await deactivate_scheduled_task(task.id, 10, 20)

            mock_tool.assert_not_called()
            mock_add.assert_not_called()
            mock_del.assert_not_called()

    # 21. transition_task_occurrence_terminal validates inputs, ownership, and atomic transition
    async def test_21_transition_task_occurrence_terminal_semantics(self):
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Term Task", local_time="10:00", timezone_name="UTC", days_of_week=[0],
        )
        planned = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 10, 20, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 500)

        # Invalid arguments validation
        self.assertEqual(await transition_task_occurrence_terminal("invalid", 10, 20, 500, OCCURRENCE_STATUS_DONE), (None, False))
        self.assertEqual(await transition_task_occurrence_terminal(occ.id, "bad", 20, 500, OCCURRENCE_STATUS_DONE), (None, False))
        self.assertEqual(await transition_task_occurrence_terminal(occ.id, 10, "bad", 500, OCCURRENCE_STATUS_DONE), (None, False))
        self.assertEqual(await transition_task_occurrence_terminal(occ.id, 10, 20, "bad", OCCURRENCE_STATUS_DONE), (None, False))
        self.assertEqual(await transition_task_occurrence_terminal(occ.id, 10, 20, 500, "bad_status"), (None, False))

        # Foreign user
        self.assertEqual(await transition_task_occurrence_terminal(occ.id, 999, 20, 500, OCCURRENCE_STATUS_DONE), (None, False))
        # Wrong message id
        occ_res, trans = await transition_task_occurrence_terminal(occ.id, 10, 20, 501, OCCURRENCE_STATUS_DONE)
        self.assertFalse(trans)
        self.assertIsNotNone(occ_res)
        self.assertEqual(occ_res.status, OCCURRENCE_STATUS_DELIVERED)

        # Successful transition
        occ_done, trans_done = await transition_task_occurrence_terminal(occ.id, 10, 20, 500, OCCURRENCE_STATUS_DONE)
        self.assertTrue(trans_done)
        self.assertEqual(occ_done.status, OCCURRENCE_STATUS_DONE)

        # Repeated transition fails
        occ_again, trans_again = await transition_task_occurrence_terminal(occ.id, 10, 20, 500, OCCURRENCE_STATUS_SKIPPED)
        self.assertFalse(trans_again)
        self.assertEqual(occ_again.status, OCCURRENCE_STATUS_DONE)

    # 22. snooze_task_occurrence validates inputs, calculates aware due_at, and preserves planned_at
    async def test_22_snooze_task_occurrence_semantics(self):
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="medication",
            name="Snooze Task", local_time="10:00", timezone_name="UTC", days_of_week=[0], dosage="1 tab"
        )
        planned = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 10, 20, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 500)

        now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)

        # Reject invalid minutes
        self.assertEqual(await snooze_task_occurrence(occ.id, 10, 20, 500, 20, now), (None, False))
        # Reject naive datetime
        with self.assertRaises(ValueError):
            await snooze_task_occurrence(occ.id, 10, 20, 500, 15, datetime(2026, 9, 5, 10, 5))

        # Successful snooze for 15 minutes
        occ_snoozed, trans = await snooze_task_occurrence(occ.id, 10, 20, 500, 15, now)
        self.assertTrue(trans)
        self.assertEqual(occ_snoozed.status, OCCURRENCE_STATUS_SNOOZED)
        self.assertEqual(occ_snoozed.planned_at, planned)
        self.assertEqual(occ_snoozed.due_at, now + timedelta(minutes=15))
        self.assertIsNone(occ_snoozed.telegram_message_id)

        # Repeated snooze fails to transition
        occ_repeat, trans_repeat = await snooze_task_occurrence(occ.id, 10, 20, 500, 30, now)
        self.assertFalse(trans_repeat)
        self.assertEqual(occ_repeat.due_at, now + timedelta(minutes=15))


class TestScheduledTasksConcurrencyRaces(unittest.IsolatedAsyncioTestCase):
    """Concurrency race suite using file-backed SQLite in WAL mode."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "scheduled_race_test.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
            await conn.execute(text("PRAGMA busy_timeout=5000;"))
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.scheduled_tasks.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()
        self.tmp_dir.cleanup()

    # 16. Кілька конкурентних get_or_create для одного task/time
    async def test_16_concurrency_race_get_or_create(self):
        """Verify concurrent get_or_create calls produce exactly 1 row, 1 winner created=True, and no IntegrityError."""
        task = await create_scheduled_task(
            user_id=100, chat_id=200, context_type="medication",
            name="Aspirin Race", local_time="08:00", timezone_name="UTC",
            days_of_week=[0, 1, 2], dosage="100mg",
        )
        planned = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)

        # 10 concurrent requests
        results = await asyncio.gather(*[
            get_or_create_task_occurrence(task.id, 100, 200, planned)
            for _ in range(10)
        ])

        created_flags = [created for _, created in results]
        occurrences = [occ for occ, _ in results]

        # Exactly one call had created=True
        self.assertEqual(sum(1 for c in created_flags if c), 1)
        self.assertEqual(sum(1 for c in created_flags if not c), 9)

        # All occurrences are non-None and have the exact same ID
        self.assertTrue(all(occ is not None for occ in occurrences))
        first_id = occurrences[0].id
        self.assertTrue(all(occ.id == first_id for occ in occurrences))

        # Exactly one DB row exists
        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(TaskOccurrence.id)))
            self.assertEqual(count, 1)
