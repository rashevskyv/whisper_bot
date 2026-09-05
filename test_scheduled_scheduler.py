import os
import sys
import html
import asyncio
import logging
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, text
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from bot.database.models import Base, Reminder, ScheduledTask, TaskOccurrence
from bot.utils.scheduled_tasks import (
    create_scheduled_task,
    get_scheduled_task,
    list_active_scheduled_tasks,
    deactivate_scheduled_task,
    get_or_create_task_occurrence,
    get_task_occurrence,
    claim_task_occurrence_for_delivery,
    complete_task_occurrence_delivery,
    revert_task_occurrence_delivery,
    mark_task_occurrence_missed,
    get_latest_task_occurrence_planned_at,
    list_pending_task_occurrences,
    CONTEXT_TYPE_MEDICATION,
    CONTEXT_TYPE_GENERIC,
    OCCURRENCE_STATUS_SCHEDULED,
    OCCURRENCE_STATUS_DELIVERED,
    OCCURRENCE_STATUS_DONE,
    OCCURRENCE_STATUS_SNOOZED,
    OCCURRENCE_STATUS_SKIPPED,
    OCCURRENCE_STATUS_MISSED,
)
from bot.utils.scheduler import SchedulerService, scheduler_service, build_occurrence_inline_keyboard
import bot_runner


class TestScheduledScheduler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.utils.scheduled_tasks.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.scheduler.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

        # Dedicated test service to prevent interference
        self.service = SchedulerService()
        self.mock_bot = AsyncMock()
        self.mock_app = MagicMock()
        self.mock_app.bot = self.mock_bot
        self.service.start(self.mock_app)

    async def asyncTearDown(self):
        if self.service.scheduler.running:
            self.service.scheduler.shutdown(wait=False)
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()

    # 1. Active task produces a CronTrigger with exact hour, minute, weekday mapping, and IANA timezone.
    async def test_1_active_task_cron_trigger_attributes(self):
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="medication",
            name="Aspirin", local_time="08:30", timezone_name="Europe/Kyiv",
            days_of_week=[0, 2, 4], dosage="100mg"
        )
        self.service.schedule_recurring_task(task)
        job = self.service.scheduler.get_job(f"scheduled_task:{task.id}")
        self.assertIsNotNone(job)
        self.assertIsInstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        self.assertEqual(fields["hour"], "8")
        self.assertEqual(fields["minute"], "30")
        self.assertEqual(fields["day_of_week"], "0,2,4")
        self.assertEqual(job.trigger.timezone, ZoneInfo("Europe/Kyiv"))

    # 2. Inactive task is not scheduled.
    async def test_2_inactive_task_not_scheduled(self):
        task = await create_scheduled_task(
            user_id=11, chat_id=21, context_type="generic",
            name="Check mail", local_time="09:00", timezone_name="UTC",
            days_of_week=[1]
        )
        await deactivate_scheduled_task(task.id, 11, 21)
        task.active = False

        self.service.schedule_recurring_task(task)
        job = self.service.scheduler.get_job(f"scheduled_task:{task.id}")
        self.assertIsNone(job)

        # Also verify deactivating an already scheduled task unschedules it
        task.active = True
        self.service.schedule_recurring_task(task)
        self.assertIsNotNone(self.service.scheduler.get_job(f"scheduled_task:{task.id}"))
        task.active = False
        self.service.schedule_recurring_task(task)
        self.assertIsNone(self.service.scheduler.get_job(f"scheduled_task:{task.id}"))

    # 3. Recurring job ID is scheduled_task:<id> and does not collide with numeric reminder ID.
    async def test_3_recurring_job_id_no_collision_with_numeric_reminder(self):
        self.service._schedule_job(reminder_id=100, chat_id=20, text="One-time", run_date=datetime.now(timezone.utc) + timedelta(hours=1))
        task = await create_scheduled_task(
            user_id=10, chat_id=20, context_type="generic",
            name="Recurring", local_time="10:00", timezone_name="UTC",
            days_of_week=[0]
        )
        task.id = 100 # force same numeric ID
        self.service.schedule_recurring_task(task)

        reminder_job = self.service.scheduler.get_job("100")
        recurring_job = self.service.scheduler.get_job("scheduled_task:100")

        self.assertIsNotNone(reminder_job)
        self.assertIsNotNone(recurring_job)
        self.assertEqual(reminder_job.id, "100")
        self.assertEqual(recurring_job.id, "scheduled_task:100")

    # 4. Repeated recurring registration replaces the existing cron job instead of duplicating it.
    async def test_4_repeated_registration_replaces_not_duplicates(self):
        task = await create_scheduled_task(
            user_id=12, chat_id=22, context_type="generic",
            name="Backup", local_time="03:00", timezone_name="UTC",
            days_of_week=[6]
        )
        self.service.schedule_recurring_task(task)
        self.service.schedule_recurring_task(task)
        self.service.schedule_recurring_task(task)

        jobs = [j for j in self.service.scheduler.get_jobs() if j.id == f"scheduled_task:{task.id}"]
        self.assertEqual(len(jobs), 1)

    # 5. A cron fire creates one occurrence with a UTC minute-normalized planned_at.
    async def test_5_cron_fire_creates_occurrence_minute_normalized(self):
        task = await create_scheduled_task(
            user_id=13, chat_id=23, context_type="medication",
            name="Vitamin D", local_time="09:15", timezone_name="UTC",
            days_of_week=[1], dosage="2000 IU"
        )
        # Tuesday 09:15:42.987654
        fire_dt = datetime(2026, 9, 8, 9, 15, 42, 987654, tzinfo=timezone.utc)
        await self.service._fire_scheduled_task(task.id, 13, 23, fire_time=fire_dt)

        occurrences = await list_pending_task_occurrences(task.id)
        self.assertEqual(len(occurrences), 1)
        occ = occurrences[0]
        self.assertEqual(occ.planned_at, datetime(2026, 9, 8, 9, 15, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(occ.due_at, occ.planned_at)
        self.assertEqual(occ.status, OCCURRENCE_STATUS_SCHEDULED)

    # 6. Repeated fire in the same task/minute reuses the same occurrence.
    async def test_6_repeated_fire_same_minute_reuses_occurrence(self):
        task = await create_scheduled_task(
            user_id=14, chat_id=24, context_type="generic",
            name="Walk", local_time="18:30", timezone_name="UTC",
            days_of_week=[0]
        )
        # Monday 18:30:05 and 18:30:50
        fire1 = datetime(2026, 9, 7, 18, 30, 5, tzinfo=timezone.utc)
        fire2 = datetime(2026, 9, 7, 18, 30, 50, tzinfo=timezone.utc)

        await self.service._fire_scheduled_task(task.id, 14, 24, fire_time=fire1)
        await self.service._fire_scheduled_task(task.id, 14, 24, fire_time=fire2)

        occurrences = await list_pending_task_occurrences(task.id)
        self.assertEqual(len(occurrences), 1)

    # 7. A concrete occurrence uses DateTrigger, due_at, and ID task_occurrence:<id>.
    async def test_7_occurrence_uses_date_trigger_and_prefixed_id(self):
        task = await create_scheduled_task(
            user_id=15, chat_id=25, context_type="generic",
            name="Plan day", local_time="07:00", timezone_name="UTC",
            days_of_week=[0]
        )
        due = datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 15, 25, due)

        self.service.schedule_task_occurrence(occ)
        job = self.service.scheduler.get_job(f"task_occurrence:{occ.id}")
        self.assertIsNotNone(job)
        self.assertIsInstance(job.trigger, DateTrigger)
        self.assertEqual(job.trigger.run_date, due)

    # 8. Medication delivery includes the exact escaped persisted name and dosage and the correct local HH:MM.
    async def test_8_medication_delivery_formatting_and_local_time(self):
        task = await create_scheduled_task(
            user_id=16, chat_id=26, context_type="medication",
            name="Amoxicillin & Clav <500>", local_time="08:30",
            timezone_name="Europe/Kyiv", days_of_week=[2],
            dosage="500mg & 125mg",
        )
        # Kyiv is UTC+3 in September, so 08:30 Kyiv is 05:30 UTC
        planned_utc = datetime(2026, 9, 9, 5, 30, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 16, 26, planned_utc)

        self.mock_bot.send_message.return_value = MagicMock(message_id=555)
        await self.service._deliver_task_occurrence(occ.id)

        self.mock_bot.send_message.assert_called_once()
        _, kwargs = self.mock_bot.send_message.call_args
        self.assertEqual(kwargs["chat_id"], 26)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        text = kwargs["text"]

        self.assertIn("💊 <b>Amoxicillin &amp; Clav &lt;500&gt;</b>", text)
        self.assertIn("Дозування: 500mg &amp; 125mg", text)
        self.assertIn("Час: 08:30", text)

    # 9. Generic delivery includes escaped name/details and omits a dosage line.
    async def test_9_generic_delivery_formatting_omits_dosage(self):
        task = await create_scheduled_task(
            user_id=17, chat_id=27, context_type="generic",
            name="Drink water <cool>", local_time="10:00",
            timezone_name="UTC", days_of_week=[0],
            details="2 glasses & a lemon slice",
        )
        planned_utc = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 17, 27, planned_utc)

        self.mock_bot.send_message.return_value = MagicMock(message_id=777)
        await self.service._deliver_task_occurrence(occ.id)

        self.mock_bot.send_message.assert_called_once()
        _, kwargs = self.mock_bot.send_message.call_args
        text = kwargs["text"]

        self.assertIn("⏰ <b>Drink water &lt;cool&gt;</b>", text)
        self.assertIn("2 glasses &amp; a lemon slice", text)
        self.assertIn("Час: 10:00", text)
        self.assertNotIn("Дозування", text)

        # Test generic without details
        task2 = await create_scheduled_task(
            user_id=17, chat_id=27, context_type="generic",
            name="Simple generic", local_time="11:00",
            timezone_name="UTC", days_of_week=[0],
        )
        occ2, _ = await get_or_create_task_occurrence(task2.id, 17, 27, planned_utc)
        self.mock_bot.send_message.reset_mock()
        self.mock_bot.send_message.return_value = MagicMock(message_id=778)
        await self.service._deliver_task_occurrence(occ2.id)
        _, kwargs2 = self.mock_bot.send_message.call_args
        self.assertIn("Час: 10:00", kwargs2["text"])
        self.assertNotIn("Дозування", kwargs2["text"])

    # 10. Successful delivery sets status="delivered" and stores returned Telegram message ID.
    async def test_10_successful_delivery_persists_status_and_message_id(self):
        task = await create_scheduled_task(
            user_id=18, chat_id=28, context_type="medication",
            name="Pill", local_time="12:00", timezone_name="UTC",
            days_of_week=[0], dosage="1 tab"
        )
        planned_utc = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 18, 28, planned_utc)

        self.mock_bot.send_message.return_value = MagicMock(message_id=12345)
        await self.service._deliver_task_occurrence(occ.id)

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_DELIVERED)
            self.assertEqual(refreshed.telegram_message_id, 12345)

    # 12. Already delivered/done/skipped/missed occurrences do not send again.
    async def test_12_terminal_statuses_do_not_send(self):
        task = await create_scheduled_task(
            user_id=19, chat_id=29, context_type="generic",
            name="Terminal check", local_time="13:00", timezone_name="UTC",
            days_of_week=[0]
        )
        for terminal_st in [OCCURRENCE_STATUS_DELIVERED, OCCURRENCE_STATUS_DONE, OCCURRENCE_STATUS_SKIPPED, OCCURRENCE_STATUS_MISSED]:
            planned = datetime(2026, 9, 7, 13, terminal_st.__hash__() % 60, tzinfo=timezone.utc)
            occ, _ = await get_or_create_task_occurrence(task.id, 19, 29, planned)
            async with self.SessionLocal() as session:
                occ_db = await session.get(TaskOccurrence, occ.id)
                occ_db.status = terminal_st
                await session.commit()

            self.mock_bot.send_message.reset_mock()
            await self.service._deliver_task_occurrence(occ.id)
            self.mock_bot.send_message.assert_not_called()

    # 13. An inactive parent task prevents delivery.
    async def test_13_inactive_parent_task_prevents_delivery(self):
        task = await create_scheduled_task(
            user_id=20, chat_id=30, context_type="medication",
            name="Inactive check", local_time="14:00", timezone_name="UTC",
            days_of_week=[0], dosage="10mg"
        )
        planned = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 20, 30, planned)

        await deactivate_scheduled_task(task.id, 20, 30)

        self.mock_bot.send_message.reset_mock()
        await self.service._deliver_task_occurrence(occ.id)
        self.mock_bot.send_message.assert_not_called()

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_SCHEDULED)

    # 14. Telegram exception restores exact previous status, leaves no message ID, and hides secret from logs.
    async def test_14_telegram_exception_reverts_status_no_log_leak(self):
        task = await create_scheduled_task(
            user_id=21, chat_id=31, context_type="medication",
            name="Secret Med", local_time="15:00", timezone_name="UTC",
            days_of_week=[0], dosage="Secret Dosage 999"
        )
        planned = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 21, 31, planned)

        # Test reversion for 'scheduled'
        self.mock_bot.send_message.side_effect = Exception("SUPER_SECRET_NETWORK_CRASH_XYZ123")

        with self.assertLogs("bot.utils.scheduler", level="ERROR") as cm:
            await self.service._deliver_task_occurrence(occ.id)

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_SCHEDULED)
            self.assertIsNone(refreshed.telegram_message_id)

        # Check secret was NOT leaked
        log_output = "\n".join(cm.output)
        self.assertNotIn("SUPER_SECRET_NETWORK_CRASH_XYZ123", log_output)
        self.assertNotIn("Secret Dosage 999", log_output)
        self.assertNotIn("Secret Med", log_output)

        # Test reversion for 'snoozed'
        async with self.SessionLocal() as session:
            occ_db = await session.get(TaskOccurrence, occ.id)
            occ_db.status = OCCURRENCE_STATUS_SNOOZED
            await session.commit()

        await self.service._deliver_task_occurrence(occ.id)

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_SNOOZED)
            self.assertIsNone(refreshed.telegram_message_id)

    # 15. Invalid/missing Telegram message ID follows the same retryable failure path.
    async def test_15_invalid_message_id_reverts_status(self):
        task = await create_scheduled_task(
            user_id=22, chat_id=32, context_type="generic",
            name="Invalid msg ID", local_time="16:00", timezone_name="UTC",
            days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 22, 32, planned)

        for bad_id in [None, 0, -5, True, False, "bad"]:
            self.mock_bot.send_message.side_effect = None
            self.mock_bot.send_message.return_value = MagicMock(message_id=bad_id)

            await self.service._deliver_task_occurrence(occ.id)

            async with self.SessionLocal() as session:
                refreshed = await session.get(TaskOccurrence, occ.id)
                self.assertEqual(refreshed.status, OCCURRENCE_STATUS_SCHEDULED)
                self.assertIsNone(refreshed.telegram_message_id)

    # 16. Restore registers CronTrigger jobs for all and only active definitions.
    async def test_16_restore_registers_cron_only_active(self):
        t1 = await create_scheduled_task(user_id=1, chat_id=1, context_type="generic", name="T1", local_time="08:00", timezone_name="UTC", days_of_week=[0])
        t2 = await create_scheduled_task(user_id=1, chat_id=1, context_type="generic", name="T2", local_time="09:00", timezone_name="UTC", days_of_week=[1])
        t3 = await create_scheduled_task(user_id=1, chat_id=1, context_type="generic", name="T3", local_time="10:00", timezone_name="UTC", days_of_week=[2])
        await deactivate_scheduled_task(t3.id, 1, 1)

        await self.service.restore_scheduled_tasks()

        self.assertIsNotNone(self.service.scheduler.get_job(f"scheduled_task:{t1.id}"))
        self.assertIsNotNone(self.service.scheduler.get_job(f"scheduled_task:{t2.id}"))
        self.assertIsNone(self.service.scheduler.get_job(f"scheduled_task:{t3.id}"))

    # 17. Restore registers DateTrigger jobs for future pending occurrences.
    async def test_17_restore_registers_date_trigger_for_future_pending(self):
        task = await create_scheduled_task(user_id=2, chat_id=2, context_type="generic", name="Fut", local_time="10:00", timezone_name="UTC", days_of_week=[0])
        now = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        fut_dt1 = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        fut_dt2 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

        occ1, _ = await get_or_create_task_occurrence(task.id, 2, 2, fut_dt1)
        occ2, _ = await get_or_create_task_occurrence(task.id, 2, 2, fut_dt2)
        async with self.SessionLocal() as session:
            o2 = await session.get(TaskOccurrence, occ2.id)
            o2.status = OCCURRENCE_STATUS_SNOOZED
            await session.commit()

        await self.service.restore_scheduled_tasks(now=now)

        j1 = self.service.scheduler.get_job(f"task_occurrence:{occ1.id}")
        j2 = self.service.scheduler.get_job(f"task_occurrence:{occ2.id}")
        self.assertIsNotNone(j1)
        self.assertIsNotNone(j2)
        self.assertIsInstance(j1.trigger, DateTrigger)
        self.assertIsInstance(j2.trigger, DateTrigger)

    # 18. Restore marks overdue pending occurrences missed.
    async def test_18_restore_marks_overdue_pending_missed(self):
        task = await create_scheduled_task(user_id=3, chat_id=3, context_type="generic", name="Past", local_time="10:00", timezone_name="UTC", days_of_week=[0])
        now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        past_dt = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)

        occ, _ = await get_or_create_task_occurrence(task.id, 3, 3, past_dt)

        await self.service.restore_scheduled_tasks(now=now)

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_MISSED)

    # 19. Offline recurrence enumeration creates and marks all missing scheduled instants without duplicates.
    async def test_19_offline_recurrence_enumeration_creates_and_marks_missed(self):
        # Created on Monday 2026-09-07 07:00 UTC, scheduled daily at 08:00 UTC
        created_dt = datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)
        task = await create_scheduled_task(
            user_id=4, chat_id=4, context_type="generic", name="Daily",
            local_time="08:00", timezone_name="UTC", days_of_week=[0, 1, 2, 3, 4, 5, 6]
        )
        async with self.SessionLocal() as session:
            t = await session.get(ScheduledTask, task.id)
            t.created_at = created_dt
            await session.commit()

        # Bot is offline until Thursday 2026-09-10 09:00 UTC
        # Missed instants: 09-07 08:00, 09-08 08:00, 09-09 08:00, 09-10 08:00 (4 occurrences)
        now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        await self.service.restore_scheduled_tasks(now=now)

        async with self.SessionLocal() as session:
            res = await session.execute(
                select(TaskOccurrence).where(TaskOccurrence.task_id == task.id).order_by(TaskOccurrence.planned_at.asc())
            )
            occs = list(res.scalars().all())
            self.assertEqual(len(occs), 4)
            for o in occs:
                self.assertEqual(o.status, OCCURRENCE_STATUS_MISSED)

    # 20. Multiple missed occurrences for one task produce one summary with the correct count.
    async def test_20_multiple_missed_occurrences_produce_single_summary(self):
        created_dt = datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)
        task = await create_scheduled_task(
            user_id=5, chat_id=55, context_type="medication", name="Aspirin <Med>",
            local_time="08:00", timezone_name="UTC", days_of_week=[0, 1, 2, 3, 4, 5, 6],
            dosage="100mg"
        )
        async with self.SessionLocal() as session:
            t = await session.get(ScheduledTask, task.id)
            t.created_at = created_dt
            await session.commit()

        now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        self.mock_bot.send_message.reset_mock()
        await self.service.restore_scheduled_tasks(now=now)

        self.mock_bot.send_message.assert_called_once()
        _, kwargs = self.mock_bot.send_message.call_args
        self.assertEqual(kwargs["chat_id"], 55)
        text = kwargs["text"]
        self.assertIn("Aspirin &lt;Med&gt;", text)
        self.assertIn("4", text)
        self.assertNotIn("100mg", text) # no medical details/dosage in summary

    # 21. Repeated restore does not resend the same missed summary.
    async def test_21_repeated_restore_does_not_resend_summary(self):
        created_dt = datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)
        task = await create_scheduled_task(
            user_id=6, chat_id=66, context_type="generic", name="Idempotent",
            local_time="08:00", timezone_name="UTC", days_of_week=[0, 1]
        )
        async with self.SessionLocal() as session:
            t = await session.get(ScheduledTask, task.id)
            t.created_at = created_dt
            await session.commit()

        now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
        await self.service.restore_scheduled_tasks(now=now)
        self.assertEqual(self.mock_bot.send_message.call_count, 1)

        # Call restore a second time
        self.mock_bot.send_message.reset_mock()
        await self.service.restore_scheduled_tasks(now=now)
        self.mock_bot.send_message.assert_not_called()

    # 22. Different affected tasks receive at most one summary each.
    async def test_22_different_tasks_receive_at_most_one_summary_each(self):
        created_dt = datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)
        t1 = await create_scheduled_task(user_id=7, chat_id=701, context_type="generic", name="TaskA", local_time="08:00", timezone_name="UTC", days_of_week=[0])
        t2 = await create_scheduled_task(user_id=7, chat_id=702, context_type="generic", name="TaskB", local_time="08:00", timezone_name="UTC", days_of_week=[0])
        async with self.SessionLocal() as session:
            task1 = await session.get(ScheduledTask, t1.id)
            task2 = await session.get(ScheduledTask, t2.id)
            task1.created_at = created_dt
            task2.created_at = created_dt
            await session.commit()

        now = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
        self.mock_bot.send_message.reset_mock()
        await self.service.restore_scheduled_tasks(now=now)

        self.assertEqual(self.mock_bot.send_message.call_count, 2)
        chats_called = {call.kwargs["chat_id"] for call in self.mock_bot.send_message.call_args_list}
        self.assertEqual(chats_called, {701, 702})

    # 23. Timezone/DST behavior comes from CronTrigger and stores unique UTC instants.
    async def test_23_timezone_dst_stored_as_unique_utc_instants(self):
        # Europe/Kyiv transitions from UTC+3 to UTC+2 on the last Sunday of October (2026-10-25)
        # 08:30 Kyiv before DST change (Oct 24, Sat) -> 05:30 UTC
        # 08:30 Kyiv after DST change (Oct 26, Mon) -> 06:30 UTC
        task = await create_scheduled_task(
            user_id=8, chat_id=88, context_type="generic", name="DST",
            local_time="08:30", timezone_name="Europe/Kyiv", days_of_week=[0, 5]
        )
        async with self.SessionLocal() as session:
            t = await session.get(ScheduledTask, task.id)
            t.created_at = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
            await session.commit()

        now = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
        await self.service.restore_scheduled_tasks(now=now)

        async with self.SessionLocal() as session:
            res = await session.execute(
                select(TaskOccurrence.planned_at).where(TaskOccurrence.task_id == task.id).order_by(TaskOccurrence.planned_at.asc())
            )
            times = list(res.scalars().all())
            self.assertEqual(len(times), 2)
            # Oct 24 (Saturday): 05:30 UTC
            self.assertEqual(times[0].replace(tzinfo=timezone.utc), datetime(2026, 10, 24, 5, 30, tzinfo=timezone.utc))
            # Oct 26 (Monday): 06:30 UTC
            self.assertEqual(times[1].replace(tzinfo=timezone.utc), datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc))

    # 23b. Ambiguous local time during Europe/Kyiv fall-back DST preserves fold and persists 2 distinct occurrences.
    async def test_23b_dst_ambiguous_local_time_preserves_fold_and_persists_both_occurrences(self):
        """Verify ambiguous local time during Europe/Kyiv fall-back DST preserves fold and persists 2 distinct occurrences."""
        kyiv_tz = ZoneInfo("Europe/Kyiv")
        # On 2026-10-25 in Europe/Kyiv, 03:30 occurs twice (fall-back from UTC+3 to UTC+2)
        local_fold0 = datetime(2026, 10, 25, 3, 30, tzinfo=kyiv_tz, fold=0)
        local_fold1 = datetime(2026, 10, 25, 3, 30, tzinfo=kyiv_tz, fold=1)

        utc_fold0 = local_fold0.astimezone(timezone.utc)
        utc_fold1 = local_fold1.astimezone(timezone.utc)

        # Assert their UTC offsets and UTC instants are different to genuinely represent an ambiguous time
        self.assertNotEqual(local_fold0.utcoffset(), local_fold1.utcoffset())
        self.assertNotEqual(utc_fold0, utc_fold1)
        self.assertEqual(utc_fold0, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(utc_fold1, datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))

        # 2026-10-25 is Sunday (day 6)
        task = await create_scheduled_task(
            user_id=88, chat_id=888, context_type="medication",
            name="AmbiguousDSTMed", local_time="03:30",
            timezone_name="Europe/Kyiv", days_of_week=[6], dosage="10mg"
        )

        # Invoke _fire_scheduled_task using the two corresponding aware UTC fire times
        await self.service._fire_scheduled_task(task.id, 88, 888, fire_time=utc_fold0)
        await self.service._fire_scheduled_task(task.id, 88, 888, fire_time=utc_fold1)

        async with self.SessionLocal() as session:
            stmt = (
                select(TaskOccurrence)
                .where(TaskOccurrence.task_id == task.id)
                .order_by(TaskOccurrence.planned_at.asc())
            )
            res = await session.execute(stmt)
            occs = list(res.scalars().all())

            # Two different TaskOccurrence rows are persisted
            self.assertEqual(len(occs), 2)
            self.assertNotEqual(occs[0].id, occs[1].id)

            # planned_at values equal the two distinct expected UTC instants
            occ0_planned = occs[0].planned_at.replace(tzinfo=timezone.utc)
            occ1_planned = occs[1].planned_at.replace(tzinfo=timezone.utc)
            self.assertEqual(occ0_planned, utc_fold0)
            self.assertEqual(occ1_planned, utc_fold1)

            # Seconds and microseconds are zero
            self.assertEqual(occs[0].planned_at.second, 0)
            self.assertEqual(occs[0].planned_at.microsecond, 0)
            self.assertEqual(occs[1].planned_at.second, 0)
            self.assertEqual(occs[1].planned_at.microsecond, 0)

    # 29. Future occurrence guard for mark_task_occurrence_missed.
    async def test_29_mark_task_occurrence_missed_future_occurrence_guard(self):
        """Verify mark_task_occurrence_missed returns False and preserves scheduled status for future occurrence."""
        task = await create_scheduled_task(
            user_id=30, chat_id=300, context_type="generic",
            name="FutureGuard", local_time="10:00", timezone_name="UTC", days_of_week=[0]
        )
        now = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        future_due = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc) # 2 hours after now

        occ, _ = await get_or_create_task_occurrence(task.id, 30, 300, future_due)

        # Call with now < due_at
        result = await mark_task_occurrence_missed(occ.id, now)
        self.assertFalse(result)

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_SCHEDULED)

    # 30. Stale-read / snooze race simulation for mark_task_occurrence_missed.
    async def test_30_mark_task_occurrence_missed_stale_read_snooze_race_simulation(self):
        """Verify that a concurrent snooze moving due_at to future prevents stale restore from marking missed."""
        task = await create_scheduled_task(
            user_id=31, chat_id=310, context_type="generic",
            name="SnoozeRace", local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        restore_now = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        initial_due = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc) # Overdue relative to restore_now

        # 1. Create an occurrence that was initially overdue
        occ, _ = await get_or_create_task_occurrence(task.id, 31, 310, initial_due)

        # 2. Load or list it as pending, representing the stale restore read
        pending = await list_pending_task_occurrences(task.id)
        self.assertEqual(len(pending), 1)
        stale_read_occ = pending[0]

        # 3. In another DB session, update its current state to snoozed with due_at in the future
        future_snoozed_due = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc) # > restore_now
        async with self.SessionLocal() as session:
            occ_db = await session.get(TaskOccurrence, stale_read_occ.id)
            occ_db.status = OCCURRENCE_STATUS_SNOOZED
            occ_db.due_at = future_snoozed_due
            await session.commit()

        # 4. Call mark_task_occurrence_missed using the stale restore boundary
        transitioned = await mark_task_occurrence_missed(stale_read_occ.id, restore_now)

        # 5. Assert it returns False
        self.assertFalse(transitioned)

        # 6. Assert the durable row remains snoozed
        # 7. Assert its future due_at is unchanged
        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, stale_read_occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_SNOOZED)
            self.assertEqual(refreshed.due_at.replace(tzinfo=timezone.utc), future_snoozed_due)

    # 31. Overdue positive test and idempotent repeat for mark_task_occurrence_missed.
    async def test_31_mark_task_occurrence_missed_overdue_positive_and_idempotent(self):
        """Verify an overdue occurrence transitions to missed once and an idempotent repeat returns False."""
        task = await create_scheduled_task(
            user_id=32, chat_id=320, context_type="generic",
            name="OverduePos", local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        now = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        past_due = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)

        occ, _ = await get_or_create_task_occurrence(task.id, 32, 320, past_due)

        # First call transitions successfully
        first_call = await mark_task_occurrence_missed(occ.id, now)
        self.assertTrue(first_call)

        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_MISSED)

        # Second call returns False (idempotent repeat)
        second_call = await mark_task_occurrence_missed(occ.id, now)
        self.assertFalse(second_call)

    # 32. Validation for mark_task_occurrence_missed.
    async def test_32_mark_task_occurrence_missed_validates_now(self):
        """Verify mark_task_occurrence_missed rejects naive or non-datetime now."""
        # Naive datetime
        with self.assertRaises(ValueError):
            await mark_task_occurrence_missed(1, datetime(2026, 9, 7, 10, 0))

        # Non-datetime
        with self.assertRaises(ValueError):
            await mark_task_occurrence_missed(1, "2026-09-07T10:00:00Z")

        # Invalid occurrence_id
        self.assertFalse(await mark_task_occurrence_missed(-1, datetime.now(timezone.utc)))
        self.assertFalse(await mark_task_occurrence_missed("bad", datetime.now(timezone.utc)))

    # 24. Job prefixes allow the same numeric ID in Reminder, ScheduledTask, and TaskOccurrence.
    async def test_24_same_numeric_id_coexists_across_namespaces(self):
        target_id = 999
        self.service._schedule_job(reminder_id=target_id, chat_id=1, text="R", run_date=datetime.now(timezone.utc) + timedelta(hours=1))

        task = await create_scheduled_task(user_id=1, chat_id=1, context_type="generic", name="S", local_time="10:00", timezone_name="UTC", days_of_week=[0])
        task.id = target_id
        self.service.schedule_recurring_task(task)

        occ = TaskOccurrence(id=target_id, task_id=task.id, planned_at=datetime.now(timezone.utc) + timedelta(hours=2), due_at=datetime.now(timezone.utc) + timedelta(hours=2), status="scheduled")
        self.service.schedule_task_occurrence(occ)

        jobs = {j.id for j in self.service.scheduler.get_jobs()}
        self.assertIn("999", jobs)
        self.assertIn("scheduled_task:999", jobs)
        self.assertIn("task_occurrence:999", jobs)

    # 25. Existing one-time reminder scheduling and deletion behavior remains unchanged.
    async def test_25_existing_one_time_reminder_regression(self):
        rem_id = await self.service.add_reminder(
            user_id=9, chat_id=99, text="Old reminder",
            trigger_time=datetime.now(timezone.utc) + timedelta(minutes=30)
        )
        self.assertIsNotNone(self.service.scheduler.get_job(str(rem_id)))

        deleted = await self.service.delete_reminder_by_id(rem_id, chat_id=99)
        self.assertTrue(deleted)
        self.assertIsNone(self.service.scheduler.get_job(str(rem_id)))

    # 26. bot_runner.post_init restores one-time reminders first and recurring scheduled tasks second.
    async def test_26_bot_runner_post_init_order(self):
        call_order = []

        async def mock_init_db():
            call_order.append("init_db")

        def mock_start(app):
            call_order.append("start")

        async def mock_restore_reminders():
            call_order.append("restore_reminders")

        async def mock_restore_scheduled_tasks():
            call_order.append("restore_scheduled_tasks")

        with patch("bot_runner.init_db", side_effect=mock_init_db), \
             patch.object(scheduler_service, "start", side_effect=mock_start), \
             patch.object(scheduler_service, "restore_reminders", side_effect=mock_restore_reminders), \
             patch.object(scheduler_service, "restore_scheduled_tasks", side_effect=mock_restore_scheduled_tasks):

            app = MagicMock()
            await bot_runner.post_init(app)

        self.assertEqual(call_order, ["init_db", "start", "restore_reminders", "restore_scheduled_tasks"])

    # 27. New D2 code does not call AI providers, execute_tool, ActionDraft handlers, or Telegram callback handlers.
    async def test_27_new_d2_code_does_not_invoke_external_handlers(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=1, context_type="medication",
            name="NoSideEffects", local_time="08:00", timezone_name="UTC",
            days_of_week=[0], dosage="10mg"
        )
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 1, datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc))

        mock_tool = AsyncMock()
        mock_draft = AsyncMock()

        with patch("bot.ai.tools.execute_tool", mock_tool), \
             patch("bot.utils.action_drafts.create_action_draft", mock_draft):

            self.service.schedule_recurring_task(task)
            self.service.schedule_task_occurrence(occ)
            await self.service._fire_scheduled_task(task.id, 1, 1, fire_time=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc))
            self.mock_bot.send_message.return_value = MagicMock(message_id=101)
            await self.service._deliver_task_occurrence(occ.id)
            await self.service.restore_scheduled_tasks(now=datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc))

            mock_tool.assert_not_called()
            mock_draft.assert_not_called()

    # 28. New D2 logs and Telegram errors never expose persisted dosage/details or a sentinel exception secret.
    async def test_28_logs_never_leak_sensitive_payload_or_sentinel_secret(self):
        task = await create_scheduled_task(
            user_id=99, chat_id=999, context_type="medication",
            name="ConfidentialDrug", local_time="10:00", timezone_name="UTC",
            days_of_week=[0], dosage="SECRET_SENSITIVE_DOSAGE_X99"
        )
        occ, _ = await get_or_create_task_occurrence(task.id, 99, 999, datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))

        self.mock_bot.send_message.side_effect = Exception("SENTINEL_CRASH_SECRET_987654321")

        with self.assertLogs("bot.utils.scheduler", level="INFO") as cm:
            await self.service._deliver_task_occurrence(occ.id)

        all_logs = "\n".join(cm.output)
        self.assertNotIn("SECRET_SENSITIVE_DOSAGE_X99", all_logs)
        self.assertNotIn("SENTINEL_CRASH_SECRET_987654321", all_logs)
        self.assertNotIn("ConfidentialDrug", all_logs)

    # 33. Occurrence delivery attaches compact inline keyboard with exact callback data
    async def test_33_delivery_attaches_compact_inline_keyboard(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=10, context_type="medication",
            name="Aspirin", local_time="08:00", timezone_name="UTC",
            days_of_week=[0], dosage="1 tab"
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 10, planned)

        sent_msg = MagicMock(message_id=777)
        self.mock_bot.send_message.return_value = sent_msg

        await self.service._deliver_task_occurrence(occ.id)

        self.mock_bot.send_message.assert_awaited_once()
        kb = self.mock_bot.send_message.call_args.kwargs["reply_markup"]
        self.assertIsNotNone(kb)
        self.assertEqual(kb.inline_keyboard[0][0].text, "✅ Прийняв")
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, f"occ:done:{occ.id}")
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, f"occ:s15:{occ.id}")
        self.assertEqual(kb.inline_keyboard[1][1].callback_data, f"occ:s30:{occ.id}")
        self.assertEqual(kb.inline_keyboard[2][0].callback_data, f"occ:skip:{occ.id}")


class TestScheduledSchedulerConcurrency(unittest.IsolatedAsyncioTestCase):
    """File-backed SQLite with WAL mode for real delivery concurrency race check (Scenario 11)."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "scheduler_race_test.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
            await conn.execute(text("PRAGMA busy_timeout=5000;"))
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.utils.scheduled_tasks.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.scheduler.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

        self.service = SchedulerService()
        self.mock_bot = AsyncMock()
        self.mock_app = MagicMock()
        self.mock_app.bot = self.mock_bot
        self.service.start(self.mock_app)

    async def asyncTearDown(self):
        if self.service.scheduler.running:
            self.service.scheduler.shutdown(wait=False)
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()
        self.tmp_dir.cleanup()

    # 11. Two concurrent delivery calls produce exactly one Telegram send and one durable delivered occurrence.
    async def test_11_concurrent_delivery_calls_single_send(self):
        task = await create_scheduled_task(
            user_id=100, chat_id=200, context_type="medication",
            name="Race Pill", local_time="08:00", timezone_name="UTC",
            days_of_week=[0], dosage="50mg"
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 100, 200, planned)

        send_count = 0

        async def slow_send_message(*args, **kwargs):
            nonlocal send_count
            send_count += 1
            await asyncio.sleep(0.05)
            return MagicMock(message_id=888)

        self.mock_bot.send_message.side_effect = slow_send_message

        # Fire 5 concurrent delivery calls for the same occurrence
        await asyncio.gather(*[
            self.service._deliver_task_occurrence(occ.id)
            for _ in range(5)
        ])

        # Exactly one call sent the message
        self.assertEqual(send_count, 1)

        # Occurrence in DB is delivered with message_id=888
        async with self.SessionLocal() as session:
            refreshed = await session.get(TaskOccurrence, occ.id)
            self.assertEqual(refreshed.status, OCCURRENCE_STATUS_DELIVERED)
            self.assertEqual(refreshed.telegram_message_id, 888)


if __name__ == "__main__":
    unittest.main()
