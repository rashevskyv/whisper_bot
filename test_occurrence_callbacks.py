import os
import sys
import unittest
import asyncio
import tempfile
import logging
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, text
from bot.database.models import Base, ScheduledTask, TaskOccurrence, Reminder, ActionDraft
from bot.utils.scheduled_tasks import (
    create_scheduled_task,
    get_or_create_task_occurrence,
    get_task_occurrence,
    transition_task_occurrence_terminal,
    snooze_task_occurrence,
    claim_task_occurrence_for_delivery,
    complete_task_occurrence_delivery,
    mark_task_occurrence_missed,
    OCCURRENCE_STATUS_SCHEDULED,
    OCCURRENCE_STATUS_DELIVERED,
    OCCURRENCE_STATUS_DONE,
    OCCURRENCE_STATUS_SKIPPED,
    OCCURRENCE_STATUS_SNOOZED,
    OCCURRENCE_STATUS_MISSED,
    CONTEXT_TYPE_MEDICATION,
    CONTEXT_TYPE_GENERIC,
)
from bot.utils.scheduler import SchedulerService, build_occurrence_inline_keyboard, scheduler_service
from bot.handlers.callbacks import handle_callback


def make_mock_update(callback_data: str, user_id: int = 123, chat_id: int = 456, message_id: int = 999, text_html: str = "<b>Task</b>"):
    update = MagicMock()
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = message_id
    query.message.text = "Task"
    query.message.text_html = text_html
    query.message.edit_text = AsyncMock()
    query.message.edit_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=chat_id)
    return update, query


class TestOccurrenceCallbacks(unittest.IsolatedAsyncioTestCase):
    """Behavioral and callback tests for recurring task occurrences."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "occ_callback_test.db")
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
            patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal),
            patch("bot.database.session.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

        self.scheduler_svc = SchedulerService()
        self.mock_bot = MagicMock()
        self.mock_bot.send_message = AsyncMock()
        self.mock_app = MagicMock()
        self.mock_app.bot = self.mock_bot
        self.scheduler_svc.start(self.mock_app)

    async def asyncTearDown(self):
        if self.scheduler_svc.scheduler.running:
            self.scheduler_svc.scheduler.shutdown(wait=False)
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()
        self.tmp_dir.cleanup()

    # 1. Medication delivery keyboard contains all four buttons and exact callback payloads
    async def test_1_medication_delivery_keyboard(self):
        kb = build_occurrence_inline_keyboard(42, CONTEXT_TYPE_MEDICATION)
        self.assertEqual(len(kb.inline_keyboard), 3)
        # Row 1: ✅ Прийняв
        self.assertEqual(len(kb.inline_keyboard[0]), 1)
        self.assertEqual(kb.inline_keyboard[0][0].text, "✅ Прийняв")
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "occ:done:42")
        # Row 2: ⏰ +15 хв, ⏰ +30 хв
        self.assertEqual(len(kb.inline_keyboard[1]), 2)
        self.assertEqual(kb.inline_keyboard[1][0].text, "⏰ +15 хв")
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, "occ:s15:42")
        self.assertEqual(kb.inline_keyboard[1][1].text, "⏰ +30 хв")
        self.assertEqual(kb.inline_keyboard[1][1].callback_data, "occ:s30:42")
        # Row 3: ⏭ Пропустив
        self.assertEqual(len(kb.inline_keyboard[2]), 1)
        self.assertEqual(kb.inline_keyboard[2][0].text, "⏭ Пропустив")
        self.assertEqual(kb.inline_keyboard[2][0].callback_data, "occ:skip:42")

    # 2. Generic delivery uses ✅ Виконав
    async def test_2_generic_delivery_keyboard(self):
        kb = build_occurrence_inline_keyboard(42, CONTEXT_TYPE_GENERIC)
        self.assertEqual(kb.inline_keyboard[0][0].text, "✅ Виконав")
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "occ:done:42")

    # 3. Every callback payload is below 64 bytes
    async def test_3_callback_payload_lengths(self):
        for occ_id in (1, 999999999):
            kb = build_occurrence_inline_keyboard(occ_id, CONTEXT_TYPE_MEDICATION)
            for row in kb.inline_keyboard:
                for btn in row:
                    self.assertLess(len(btn.callback_data.encode("utf-8")), 64)

    # 4. Successful delivery persists telegram_message_id and attaches keyboard
    async def test_4_successful_delivery_persists_msg_id_and_markup(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="medication", name="Aspirin",
            local_time="08:00", timezone_name="UTC", days_of_week=[0], dosage="100mg"
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)

        sent_mock = MagicMock(message_id=888)
        self.mock_bot.send_message = AsyncMock(return_value=sent_mock)

        with patch("bot.utils.scheduler.scheduler_service", self.scheduler_svc):
            await self.scheduler_svc._deliver_task_occurrence(occ.id)

        self.mock_bot.send_message.assert_awaited_once()
        call_kwargs = self.mock_bot.send_message.call_args.kwargs
        self.assertIn("reply_markup", call_kwargs)
        kb = call_kwargs["reply_markup"]
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, f"occ:done:{occ.id}")

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DELIVERED)
        self.assertEqual(refetched.telegram_message_id, 888)

    # 5. Malformed occ: callback performs no DB or scheduler mutation
    async def test_5_malformed_callbacks_rejected(self):
        malformed_datas = [
            "occ:done",
            "occ:done:1:extra",
            "occ:unknown:1",
            "occ:done:abc",
            "occ:done:0",
            "occ:done:-5",
        ]
        for data in malformed_datas:
            update, query = make_mock_update(data, user_id=1, chat_id=2, message_id=888)
            with patch("bot.utils.scheduler.scheduler_service.schedule_task_occurrence") as mock_sched:
                await handle_callback(update, MagicMock())
                query.answer.assert_called_with("❌ Некоректні дані запиту.", show_alert=True)
                mock_sched.assert_not_called()

    # 6. Foreign user cannot transition occurrence
    async def test_6_foreign_user_cannot_transition(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Run",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # Foreign user 999 clicks
        update, query = make_mock_update(f"occ:done:{occ.id}", user_id=999, chat_id=2, message_id=888)
        await handle_callback(update, MagicMock())

        query.answer.assert_called_with("❌ Завдання не знайдено або воно вам не належить.", show_alert=True)
        # Foreign user does NOT remove markup for the owner
        query.message.edit_reply_markup.assert_not_called()
        query.message.edit_text.assert_not_called()

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DELIVERED)

    # 7. Wrong chat cannot transition occurrence
    async def test_7_wrong_chat_cannot_transition(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Run",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # Correct user but wrong chat 999
        update, query = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=999, message_id=888)
        await handle_callback(update, MagicMock())

        query.answer.assert_called_with("❌ Завдання не знайдено або воно вам не належить.", show_alert=True)
        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DELIVERED)

    # 8. Callback from a different Telegram message ID cannot transition
    async def test_8_wrong_message_id_cannot_transition(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Run",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # Message id 999 != stored 888
        update, query = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=999)
        await handle_callback(update, MagicMock())

        query.answer.assert_called_with("⚠️ Дія недоступна для цього повідомлення.", show_alert=True)
        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DELIVERED)

    # 9. Delivered -> done succeeds once and edits message
    async def test_9_delivered_to_done_succeeds_once(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="medication", name="Vitamin",
            local_time="08:00", timezone_name="UTC", days_of_week=[0], dosage="1 tab"
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        update, query = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=888, text_html="💊 <b>Vitamin</b>")
        await handle_callback(update, MagicMock())

        query.answer.assert_called_with("✅ Виконано!")
        query.message.edit_text.assert_awaited_once()
        new_text = query.message.edit_text.call_args.args[0]
        self.assertIn("✅ Позначено виконаним.", new_text)
        self.assertIn("💊 <b>Vitamin</b>", new_text)

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DONE)

    # 10. Repeated done is idempotent and informs user
    async def test_10_repeated_done_is_idempotent(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Walk",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # First click
        update, query = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=888)
        await handle_callback(update, MagicMock())

        # Second click
        update2, query2 = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=888)
        await handle_callback(update2, MagicMock())

        query2.answer.assert_called_with("✅ Це завдання вже відзначено як виконане.", show_alert=True)
        query2.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DONE)

    # 11. Delivered -> skipped succeeds once
    async def test_11_delivered_to_skipped_succeeds_once(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Workout",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        update, query = make_mock_update(f"occ:skip:{occ.id}", user_id=1, chat_id=2, message_id=888)
        await handle_callback(update, MagicMock())

        query.answer.assert_called_with("⏭ Пропущено")
        query.message.edit_text.assert_awaited_once()
        self.assertIn("⏭ Подію пропущено.", query.message.edit_text.call_args.args[0])

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_SKIPPED)

    # 12. Repeated skip is idempotent
    async def test_12_repeated_skip_is_idempotent(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Workout",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        update, query = make_mock_update(f"occ:skip:{occ.id}", user_id=1, chat_id=2, message_id=888)
        await handle_callback(update, MagicMock())

        update2, query2 = make_mock_update(f"occ:skip:{occ.id}", user_id=1, chat_id=2, message_id=888)
        await handle_callback(update2, MagicMock())

        query2.answer.assert_called_with("⏭ Це завдання вже пропущено.", show_alert=True)
        query2.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

    # 13. Snooze 15 persists aware UTC due_at, clears message ID, and schedules DateTrigger job
    async def test_13_snooze_15_persists_due_at_clears_msg_id_registers_job(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Read",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        before_snooze = datetime.now(timezone.utc)
        with patch("bot.handlers.callbacks.scheduler_service", self.scheduler_svc):
            update, query = make_mock_update(f"occ:s15:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update, MagicMock())
        after_snooze = datetime.now(timezone.utc)

        query.answer.assert_called_with("⏰ Відкладено на 15 хв")
        query.message.edit_text.assert_awaited_once()
        self.assertIn("⏰ Відкладено на 15 хв.", query.message.edit_text.call_args.args[0])

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_SNOOZED)
        self.assertIsNone(refetched.telegram_message_id)
        self.assertEqual(refetched.planned_at, planned)
        self.assertGreaterEqual(refetched.due_at, before_snooze + timedelta(minutes=15))
        self.assertLessEqual(refetched.due_at, after_snooze + timedelta(minutes=15))

        # Check job registered in scheduler
        job = self.scheduler_svc.scheduler.get_job(f"task_occurrence:{occ.id}")
        self.assertIsNotNone(job)
        self.assertEqual(job.next_run_time, refetched.due_at)

    # 14. Snooze 30 does the same
    async def test_14_snooze_30_persists_due_at_and_registers_job(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="medication", name="Pill",
            local_time="08:00", timezone_name="UTC", days_of_week=[0], dosage="2 tabs"
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        before_snooze = datetime.now(timezone.utc)
        with patch("bot.handlers.callbacks.scheduler_service", self.scheduler_svc):
            update, query = make_mock_update(f"occ:s30:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update, MagicMock())
        after_snooze = datetime.now(timezone.utc)

        query.answer.assert_called_with("⏰ Відкладено на 30 хв")
        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_SNOOZED)
        self.assertGreaterEqual(refetched.due_at, before_snooze + timedelta(minutes=30))
        self.assertLessEqual(refetched.due_at, after_snooze + timedelta(minutes=30))

    # 15. Repeated snooze does not move due_at again
    async def test_15_repeated_snooze_does_not_move_due_at(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Study",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        with patch("bot.handlers.callbacks.scheduler_service", self.scheduler_svc):
            update, query = make_mock_update(f"occ:s15:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update, MagicMock())

        refetched_1 = await get_task_occurrence(occ.id, 1, 2)
        first_due_at = refetched_1.due_at

        # Repeated click
        with patch("bot.handlers.callbacks.scheduler_service", self.scheduler_svc):
            update2, query2 = make_mock_update(f"occ:s30:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update2, MagicMock())

        query2.answer.assert_called_with("⏰ Це завдання вже відкладено.", show_alert=True)
        refetched_2 = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched_2.due_at, first_due_at)
        self.assertEqual(refetched_2.status, OCCURRENCE_STATUS_SNOOZED)

    # 16. Done and skip do not schedule a DateTrigger
    async def test_16_done_and_skip_do_not_schedule_datetrigger(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Chore",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        with patch.object(self.scheduler_svc, "schedule_task_occurrence") as mock_sched, \
             patch("bot.handlers.callbacks.scheduler_service", self.scheduler_svc):
            update, query = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update, MagicMock())
            mock_sched.assert_not_called()

    # 17. Scheduler registration failure leaves DB state snoozed and returns safe restart-recovery UX
    async def test_17_scheduler_registration_failure_leaves_db_snoozed(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Exercise",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        with patch("bot.handlers.callbacks.scheduler_service.schedule_task_occurrence", side_effect=RuntimeError("Scheduler offline")):
            update, query = make_mock_update(f"occ:s15:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update, MagicMock())

        query.answer.assert_called_with("⏰ Відкладено на 15 хв (буде відновлено після перезапуску)", show_alert=True)
        query.message.edit_text.assert_awaited_once()
        self.assertIn("буде відновлено після перезапуску", query.message.edit_text.call_args.args[0])

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_SNOOZED)

    # 18. Future snoozed occurrence is restored after restart
    async def test_18_future_snoozed_occurrence_restored_after_restart(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Future Task",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        future_due = datetime.now(timezone.utc) + timedelta(minutes=20)
        async with self.SessionLocal() as session:
            await session.execute(
                text("UPDATE task_occurrences SET status = 'snoozed', due_at = :due, telegram_message_id = NULL WHERE id = :id"),
                {"due": future_due, "id": occ.id}
            )
            await session.commit()

        # Restart simulation
        await self.scheduler_svc.restore_scheduled_tasks()

        job = self.scheduler_svc.scheduler.get_job(f"task_occurrence:{occ.id}")
        self.assertIsNotNone(job)
        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_SNOOZED)

    # 19. Overdue snoozed occurrence follows existing missed policy after restart
    async def test_19_overdue_snoozed_occurrence_marked_missed_after_restart(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Past Task",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        past_due = datetime.now(timezone.utc) - timedelta(minutes=20)
        async with self.SessionLocal() as session:
            await session.execute(
                text("UPDATE task_occurrences SET status = 'snoozed', due_at = :due, telegram_message_id = NULL WHERE id = :id"),
                {"due": past_due, "id": occ.id}
            )
            await session.commit()

        # Restart simulation
        await self.scheduler_svc.restore_scheduled_tasks()

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_MISSED)

    # 20. Re-delivery after snooze stores new telegram_message_id and shows fresh buttons
    async def test_20_redelivery_after_snooze_stores_new_message_id(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Re-deliver",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # Snooze it
        now = datetime.now(timezone.utc)
        await snooze_task_occurrence(occ.id, 1, 2, 888, 15, now)

        # Re-deliver firing
        sent_mock = MagicMock(message_id=999)
        self.mock_bot.send_message = AsyncMock(return_value=sent_mock)
        with patch("bot.utils.scheduler.scheduler_service", self.scheduler_svc):
            await self.scheduler_svc._deliver_task_occurrence(occ.id)

        refetched = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched.status, OCCURRENCE_STATUS_DELIVERED)
        self.assertEqual(refetched.telegram_message_id, 999)

        # Old message_id=888 no longer transitions
        update_old, query_old = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=888)
        await handle_callback(update_old, MagicMock())
        query_old.answer.assert_called_with("⚠️ Дія недоступна для цього повідомлення.", show_alert=True)

        # New message_id=999 transitions
        update_new, query_new = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=999)
        await handle_callback(update_new, MagicMock())
        query_new.answer.assert_called_with("✅ Виконано!")
        refetched_final = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(refetched_final.status, OCCURRENCE_STATUS_DONE)

    # 21. State-aware handling for missed and scheduled occurrences
    async def test_21_state_aware_handling_missed_and_scheduled(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="States",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)

        # In status 'scheduled'
        update_sched, query_sched = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=111)
        await handle_callback(update_sched, MagicMock())
        query_sched.answer.assert_called_with("⏳ Дія наразі недоступна для цього завдання.", show_alert=True)

        # In status 'missed'
        async with self.SessionLocal() as session:
            await session.execute(
                text("UPDATE task_occurrences SET status = 'missed' WHERE id = :id"),
                {"id": occ.id}
            )
            await session.commit()

        update_missed, query_missed = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=111)
        await handle_callback(update_missed, MagicMock())
        query_missed.answer.assert_called_with("⚠️ Термін виконання цього завдання минув.", show_alert=True)
        query_missed.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

    # 22. Done vs skip concurrency race has exactly one winner
    async def test_22_concurrency_race_done_vs_skip(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Race1",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # Run done and skip concurrently
        res_done, res_skip = await asyncio.gather(
            transition_task_occurrence_terminal(occ.id, 1, 2, 888, OCCURRENCE_STATUS_DONE),
            transition_task_occurrence_terminal(occ.id, 1, 2, 888, OCCURRENCE_STATUS_SKIPPED),
        )

        winners = [r for r in [res_done, res_skip] if r[1] is True]
        losers = [r for r in [res_done, res_skip] if r[1] is False]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)

        final_occ = await get_task_occurrence(occ.id, 1, 2)
        self.assertIn(final_occ.status, (OCCURRENCE_STATUS_DONE, OCCURRENCE_STATUS_SKIPPED))
        self.assertEqual(final_occ.status, winners[0][0].status)

    # 23. Done vs snooze concurrency race has exactly one winner
    async def test_23_concurrency_race_done_vs_snooze(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Race2",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        now = datetime.now(timezone.utc)
        res_done, res_snooze = await asyncio.gather(
            transition_task_occurrence_terminal(occ.id, 1, 2, 888, OCCURRENCE_STATUS_DONE),
            snooze_task_occurrence(occ.id, 1, 2, 888, 15, now),
        )

        winners = [r for r in [res_done, res_snooze] if r[1] is True]
        self.assertEqual(len(winners), 1)

        final_occ = await get_task_occurrence(occ.id, 1, 2)
        if res_done[1] is True:
            self.assertEqual(final_occ.status, OCCURRENCE_STATUS_DONE)
            self.assertEqual(final_occ.due_at, planned)
        else:
            self.assertEqual(final_occ.status, OCCURRENCE_STATUS_SNOOZED)
            self.assertGreater(final_occ.due_at, planned)

    # 24. Snooze 15 vs snooze 30 concurrency race has exactly one winner
    async def test_24_concurrency_race_snooze15_vs_snooze30(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Race3",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        now = datetime.now(timezone.utc)
        res_15, res_30 = await asyncio.gather(
            snooze_task_occurrence(occ.id, 1, 2, 888, 15, now),
            snooze_task_occurrence(occ.id, 1, 2, 888, 30, now),
        )

        winners = [r for r in [res_15, res_30] if r[1] is True]
        self.assertEqual(len(winners), 1)

        final_occ = await get_task_occurrence(occ.id, 1, 2)
        self.assertEqual(final_occ.status, OCCURRENCE_STATUS_SNOOZED)
        if res_15[1] is True:
            self.assertEqual(final_occ.due_at, now + timedelta(minutes=15))
        else:
            self.assertEqual(final_occ.due_at, now + timedelta(minutes=30))

    # 25. Two simultaneous done calls have exactly one winner and no row duplication
    async def test_25_concurrency_race_two_simultaneous_done(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Race4",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        res1, res2 = await asyncio.gather(
            transition_task_occurrence_terminal(occ.id, 1, 2, 888, OCCURRENCE_STATUS_DONE),
            transition_task_occurrence_terminal(occ.id, 1, 2, 888, OCCURRENCE_STATUS_DONE),
        )

        winners = [r for r in [res1, res2] if r[1] is True]
        self.assertEqual(len(winners), 1)

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(TaskOccurrence.id)))
            self.assertEqual(count, 1)

    # 26. Two simultaneous snooze calls have exactly one winner
    async def test_26_concurrency_race_two_simultaneous_snooze(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="Race5",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        now = datetime.now(timezone.utc)
        res1, res2 = await asyncio.gather(
            snooze_task_occurrence(occ.id, 1, 2, 888, 15, now),
            snooze_task_occurrence(occ.id, 1, 2, 888, 15, now),
        )

        winners = [r for r in [res1, res2] if r[1] is True]
        self.assertEqual(len(winners), 1)

    # 27. Repeated/concurrent callbacks create no new occurrence rows
    async def test_27_repeated_callbacks_create_no_new_occurrence_rows(self):
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="generic", name="NoNewRows",
            local_time="08:00", timezone_name="UTC", days_of_week=[0]
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        # 5 parallel calls
        await asyncio.gather(*[
            transition_task_occurrence_terminal(occ.id, 1, 2, 888, OCCURRENCE_STATUS_DONE)
            for _ in range(5)
        ])

        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(TaskOccurrence.id)))
            self.assertEqual(count, 1)

    # 28. Security and privacy: logs and errors contain no sentinel medication/dosage/exception secrets
    async def test_28_privacy_logs_never_leak_secrets(self):
        SENTINEL_MED = "SENTINEL_DRUG_XYZ123"
        SENTINEL_DOSAGE = "DOSAGE_500MG_SECRET"
        task = await create_scheduled_task(
            user_id=1, chat_id=2, context_type="medication", name=SENTINEL_MED,
            local_time="08:00", timezone_name="UTC", days_of_week=[0], dosage=SENTINEL_DOSAGE
        )
        planned = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        occ, _ = await get_or_create_task_occurrence(task.id, 1, 2, planned)
        await claim_task_occurrence_for_delivery(occ.id)
        await complete_task_occurrence_delivery(occ.id, 888)

        with self.assertLogs("bot", level="INFO") as cm:
            update, query = make_mock_update(f"occ:done:{occ.id}", user_id=1, chat_id=2, message_id=888)
            await handle_callback(update, MagicMock())

        for record in cm.records:
            self.assertNotIn(SENTINEL_MED, record.getMessage())
            self.assertNotIn(SENTINEL_DOSAGE, record.getMessage())

    # 29. Existing reminder callbacks unaffected
    async def test_29_existing_reminder_callbacks_unaffected(self):
        with patch.object(scheduler_service, "delete_reminder_by_id", new_callable=AsyncMock) as mock_del, \
             patch.object(scheduler_service, "get_active_reminders", new_callable=AsyncMock, return_value=[]):
            update, query = make_mock_update("del_rem_777", user_id=1, chat_id=2)
            await handle_callback(update, MagicMock())
            mock_del.assert_awaited_once_with(777, chat_id=2)
            query.answer.assert_called_with("Нагадування видалено!")

    # 30. Existing draft callbacks unaffected
    async def test_30_existing_draft_callbacks_unaffected(self):
        update, query = make_mock_update("draft:no:99999", user_id=1, chat_id=2)
        await handle_callback(update, MagicMock())
        query.answer.assert_called_with("❌ Чернетку не знайдено або вона вам не належить.", show_alert=True)


if __name__ == "__main__":
    unittest.main()
