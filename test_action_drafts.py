import os
import sys
import asyncio
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tempfile
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, text
from bot.database.models import Base, ActionDraft
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    get_active_action_draft,
    update_action_draft_information,
    confirm_action_draft,
    cancel_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_EXPIRED,
)


class TestActionDrafts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()

    # 1. Model fields persist and create_all creates table
    async def test_model_fields_and_table_creation(self):
        """Verify ActionDraft persists all fields and table structure is created."""
        draft = await create_action_draft(
            user_id=123,
            chat_id=-456,
            action_type="schedule_reminder",
            payload={"iso_time_utc": "2026-09-04T12:00:00Z", "text": "Take vitamins"},
            missing_fields=["dosage"],
            source_message_id=789,
            ttl_seconds=1800,
        )

        self.assertIsNotNone(draft.id)
        self.assertEqual(draft.user_id, 123)
        self.assertEqual(draft.chat_id, -456)
        self.assertEqual(draft.source_message_id, 789)
        self.assertEqual(draft.action_type, "schedule_reminder")
        self.assertEqual(draft.payload, {"iso_time_utc": "2026-09-04T12:00:00Z", "text": "Take vitamins"})
        self.assertEqual(draft.missing_fields, ["dosage"])
        self.assertEqual(draft.status, DRAFT_STATUS_AWAITING_INFO)
        self.assertIsNotNone(draft.expires_at)
        self.assertIsNotNone(draft.created_at)
        self.assertIsNotNone(draft.updated_at)

    # 2. Missing fields choose awaiting_info; no missing fields choose pending_confirmation
    async def test_status_selection_based_on_missing_fields(self):
        """Verify missing fields selects awaiting_info, otherwise pending_confirmation."""
        draft_awaiting = await create_action_draft(
            user_id=1,
            chat_id=10,
            action_type="schedule_reminder",
            payload={"text": "Doctor"},
            missing_fields=["time"],
        )
        self.assertEqual(draft_awaiting.status, DRAFT_STATUS_AWAITING_INFO)

        draft_pending = await create_action_draft(
            user_id=2,
            chat_id=20,
            action_type="schedule_reminder",
            payload={"iso_time_utc": "2026-09-04T10:00:00Z", "text": "Dentist"},
            missing_fields=[],
        )
        self.assertEqual(draft_pending.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        draft_none = await create_action_draft(
            user_id=3,
            chat_id=30,
            action_type="delete_reminder",
            payload={"reminder_id": 55},
            missing_fields=None,
        )
        self.assertEqual(draft_none.status, DRAFT_STATUS_PENDING_CONFIRMATION)

    # 3. Creating a replacement cancels prior active draft for same user/chat
    async def test_replacement_cancels_prior_active_draft(self):
        """Verify creating a replacement draft for the same user/chat cancels the previous active draft."""
        d1 = await create_action_draft(
            user_id=5,
            chat_id=50,
            action_type="schedule_reminder",
            payload={"text": "First draft"},
            missing_fields=["time"],
        )
        self.assertEqual(d1.status, DRAFT_STATUS_AWAITING_INFO)

        d2 = await create_action_draft(
            user_id=5,
            chat_id=50,
            action_type="schedule_reminder",
            payload={"iso_time_utc": "2026-09-04T12:00:00Z", "text": "Replacement draft"},
            missing_fields=None,
        )
        self.assertEqual(d2.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        # Check d1 in DB
        d1_reloaded = await get_action_draft(d1.id, user_id=5, chat_id=50)
        self.assertIsNotNone(d1_reloaded)
        self.assertEqual(d1_reloaded.status, DRAFT_STATUS_CANCELLED)

        # Active draft for user 5, chat 50 should now be d2
        active = await get_active_action_draft(user_id=5, chat_id=50)
        self.assertIsNotNone(active)
        self.assertEqual(active.id, d2.id)

    # 4. Drafts belonging to another user or chat remain untouched
    async def test_draft_isolation_across_users_and_chats(self):
        """Verify drafts for different users or chats are isolated and remain active concurrently."""
        d_user1 = await create_action_draft(user_id=10, chat_id=100, action_type="schedule_reminder", payload={"a": 1})
        d_user2 = await create_action_draft(user_id=20, chat_id=100, action_type="schedule_reminder", payload={"b": 2})
        d_chat2 = await create_action_draft(user_id=10, chat_id=200, action_type="schedule_reminder", payload={"c": 3})

        # All three should be active
        self.assertEqual(d_user1.status, DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(d_user2.status, DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(d_chat2.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        # Replacing d_user1 should not cancel d_user2 or d_chat2
        d_user1_repl = await create_action_draft(user_id=10, chat_id=100, action_type="delete_reminder", payload={"reminder_id": 1})

        active_u1 = await get_active_action_draft(user_id=10, chat_id=100)
        active_u2 = await get_active_action_draft(user_id=20, chat_id=100)
        active_c2 = await get_active_action_draft(user_id=10, chat_id=200)

        self.assertEqual(active_u1.id, d_user1_repl.id)
        self.assertEqual(active_u2.id, d_user2.id)
        self.assertEqual(active_c2.id, d_chat2.id)

    # 5. Exact owner/user/chat lookup prevents cross-user and cross-chat access
    async def test_exact_owner_user_chat_lookup_prevents_cross_access(self):
        """Verify lookup, update, confirm, and cancel operations enforce exact ownership."""
        draft = await create_action_draft(user_id=15, chat_id=150, action_type="schedule_reminder", payload={"x": 1})

        # Wrong user
        self.assertIsNone(await get_action_draft(draft.id, user_id=99, chat_id=150))
        self.assertIsNone(await get_active_action_draft(user_id=99, chat_id=150))
        self.assertIsNone(await update_action_draft_information(draft.id, user_id=99, chat_id=150, payload_updates={"x": 2}))
        d_conf, won = await confirm_action_draft(draft.id, user_id=99, chat_id=150)
        self.assertIsNone(d_conf)
        self.assertFalse(won)
        self.assertIsNone(await cancel_action_draft(draft.id, user_id=99, chat_id=150))

        # Wrong chat
        self.assertIsNone(await get_action_draft(draft.id, user_id=15, chat_id=999))
        self.assertIsNone(await get_active_action_draft(user_id=15, chat_id=999))
        self.assertIsNone(await update_action_draft_information(draft.id, user_id=15, chat_id=999, payload_updates={"x": 2}))
        d_conf, won = await confirm_action_draft(draft.id, user_id=15, chat_id=999)
        self.assertIsNone(d_conf)
        self.assertFalse(won)
        self.assertIsNone(await cancel_action_draft(draft.id, user_id=15, chat_id=999))

        # Original draft remains active and unchanged
        reloaded = await get_active_action_draft(user_id=15, chat_id=150)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.id, draft.id)
        self.assertEqual(reloaded.status, DRAFT_STATUS_PENDING_CONFIRMATION)

    # 6. Expired active drafts become expired and are no longer returned as active
    async def test_expired_drafts_become_expired(self):
        """Verify expired drafts transition to expired and are not returned as active."""
        draft = await create_action_draft(user_id=33, chat_id=330, action_type="schedule_reminder", payload={"x": 1}, ttl_seconds=10)

        # Manually backdate expires_at in database to simulate expiry
        past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        async with self.SessionLocal() as session:
            d = await session.get(ActionDraft, draft.id)
            d.expires_at = past_time
            await session.commit()

        # get_active_action_draft should detect expiry, persist status=expired, and return None
        active = await get_active_action_draft(user_id=33, chat_id=330)
        self.assertIsNone(active)

        # get_action_draft should return the expired draft
        reloaded = await get_action_draft(draft.id, user_id=33, chat_id=330)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.status, DRAFT_STATUS_EXPIRED)

    # 7. Information updates merge payload and transition to pending_confirmation once requirements complete
    async def test_information_updates_merge_payload_and_transition(self):
        """Verify update_action_draft_information merges payload and updates status appropriately."""
        draft = await create_action_draft(
            user_id=44,
            chat_id=440,
            action_type="schedule_reminder",
            payload={"text": "Dinner"},
            missing_fields=["time", "location"],
        )
        self.assertEqual(draft.status, DRAFT_STATUS_AWAITING_INFO)

        # Partial update: provide location, time still missing
        upd1 = await update_action_draft_information(
            draft.id,
            user_id=44,
            chat_id=440,
            payload_updates={"location": "Downtown"},
            missing_fields=["time"],
        )
        self.assertIsNotNone(upd1)
        self.assertEqual(upd1.status, DRAFT_STATUS_AWAITING_INFO)
        self.assertEqual(upd1.payload, {"text": "Dinner", "location": "Downtown"})
        self.assertEqual(upd1.missing_fields, ["time"])

        # Final update: provide time, no missing fields left
        upd2 = await update_action_draft_information(
            draft.id,
            user_id=44,
            chat_id=440,
            payload_updates={"iso_time_utc": "2026-09-04T19:00:00Z"},
            missing_fields=[],
        )
        self.assertIsNotNone(upd2)
        self.assertEqual(upd2.status, DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(upd2.payload, {"text": "Dinner", "location": "Downtown", "iso_time_utc": "2026-09-04T19:00:00Z"})
        self.assertEqual(upd2.missing_fields, [])

    # 8. awaiting_info cannot be confirmed
    async def test_awaiting_info_cannot_be_confirmed(self):
        """Verify confirm_action_draft rejects drafts in awaiting_info status."""
        draft = await create_action_draft(
            user_id=55,
            chat_id=550,
            action_type="schedule_reminder",
            payload={"text": "Task"},
            missing_fields=["time"],
        )
        self.assertEqual(draft.status, DRAFT_STATUS_AWAITING_INFO)

        res_draft, won = await confirm_action_draft(draft.id, user_id=55, chat_id=550)
        self.assertIsNotNone(res_draft)
        self.assertFalse(won)
        self.assertEqual(res_draft.status, DRAFT_STATUS_AWAITING_INFO)

    # 9. Confirm is idempotent and exposes whether the current call won the transition
    async def test_confirm_is_idempotent_and_identifies_winner(self):
        """Verify confirm transitions pending_confirmation to confirmed once and repeated calls return (draft, False)."""
        draft = await create_action_draft(
            user_id=66,
            chat_id=660,
            action_type="delete_reminder",
            payload={"reminder_id": 10},
            missing_fields=None,
        )
        self.assertEqual(draft.status, DRAFT_STATUS_PENDING_CONFIRMATION)

        # First call wins transition
        d1, won1 = await confirm_action_draft(draft.id, user_id=66, chat_id=660)
        self.assertIsNotNone(d1)
        self.assertTrue(won1)
        self.assertEqual(d1.status, DRAFT_STATUS_CONFIRMED)

        # Second call: idempotent, already confirmed
        d2, won2 = await confirm_action_draft(draft.id, user_id=66, chat_id=660)
        self.assertIsNotNone(d2)
        self.assertFalse(won2)
        self.assertEqual(d2.status, DRAFT_STATUS_CONFIRMED)

    # 10. Competing confirmations have exactly one winner
    async def test_competing_confirmations_have_single_winner(self):
        """Verify two concurrent confirmation calls result in exactly one winner."""
        draft = await create_action_draft(
            user_id=77,
            chat_id=770,
            action_type="schedule_reminder",
            payload={"iso_time_utc": "2026-09-04T12:00:00Z", "text": "Race condition test"},
            missing_fields=None,
        )

        results = await asyncio.gather(
            confirm_action_draft(draft.id, user_id=77, chat_id=770),
            confirm_action_draft(draft.id, user_id=77, chat_id=770),
        )

        winners = [won for _, won in results if won]
        self.assertEqual(len(winners), 1)

    # 11. Cancellation is idempotent and terminal states are not revived
    async def test_cancellation_idempotent_and_terminal_states_not_revived(self):
        """Verify cancellation is idempotent and cannot cancel confirmed or expired drafts."""
        draft = await create_action_draft(user_id=88, chat_id=880, action_type="schedule_reminder", payload={"x": 1})

        # Cancel active draft
        c1 = await cancel_action_draft(draft.id, user_id=88, chat_id=880)
        self.assertIsNotNone(c1)
        self.assertEqual(c1.status, DRAFT_STATUS_CANCELLED)

        # Repeated cancellation
        c2 = await cancel_action_draft(draft.id, user_id=88, chat_id=880)
        self.assertIsNotNone(c2)
        self.assertEqual(c2.status, DRAFT_STATUS_CANCELLED)

        # Update cannot revive cancelled draft
        upd = await update_action_draft_information(draft.id, user_id=88, chat_id=880, payload_updates={"x": 2})
        self.assertIsNone(upd)

        # Confirmed draft cannot be cancelled
        d_conf = await create_action_draft(user_id=89, chat_id=880, action_type="schedule_reminder", payload={"x": 1})
        await confirm_action_draft(d_conf.id, user_id=89, chat_id=880)
        c_conf = await cancel_action_draft(d_conf.id, user_id=89, chat_id=880)
        self.assertEqual(c_conf.status, DRAFT_STATUS_CONFIRMED)

    # 12. Invalid inputs fail before any database row is written
    async def test_invalid_inputs_fail_before_db_write(self):
        """Verify invalid arguments raise ValueError before any DB record is created."""
        invalid_cases = [
            {"user_id": -1, "chat_id": 1, "action_type": "schedule_reminder", "payload": {}},
            {"user_id": True, "chat_id": 1, "action_type": "schedule_reminder", "payload": {}},
            {"user_id": 1, "chat_id": 0, "action_type": "schedule_reminder", "payload": {}},
            {"user_id": 1, "chat_id": False, "action_type": "schedule_reminder", "payload": {}},
            {"user_id": 1, "chat_id": 1, "action_type": "invalid_action", "payload": {}},
            {"user_id": 1, "chat_id": 1, "action_type": "schedule_reminder", "payload": "not_dict"},
            {"user_id": 1, "chat_id": 1, "action_type": "schedule_reminder", "payload": {}, "missing_fields": ["", "  "]},
            {"user_id": 1, "chat_id": 1, "action_type": "schedule_reminder", "payload": {}, "source_message_id": -5},
            {"user_id": 1, "chat_id": 1, "action_type": "schedule_reminder", "payload": {}, "ttl_seconds": 0},
            {"user_id": 1, "chat_id": 1, "action_type": "schedule_reminder", "payload": {}, "ttl_seconds": 100000},
        ]

        for case in invalid_cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    await create_action_draft(**case)

        # Verify no rows written
        async with self.SessionLocal() as session:
            count = await session.scalar(select(func.count(ActionDraft.id)))
            self.assertEqual(count, 0)

    # 13. No scheduler, execute_tool, provider, or Telegram handler is invoked
    async def test_no_side_effects_invoked(self):
        """Verify C1 operations perform database lifecycle changes only and invoke no external side effects."""
        mock_exec = AsyncMock()
        mock_add = AsyncMock()
        mock_del = AsyncMock()

        with patch("bot.ai.tools.execute_tool", mock_exec), \
             patch("bot.utils.scheduler.scheduler_service.add_reminder", mock_add), \
             patch("bot.utils.scheduler.scheduler_service.delete_reminder_by_id", mock_del):

            # Full draft lifecycle
            d = await create_action_draft(user_id=99, chat_id=990, action_type="schedule_reminder", payload={"x": 1}, missing_fields=["f"])
            await update_action_draft_information(d.id, user_id=99, chat_id=990, missing_fields=[])
            await confirm_action_draft(d.id, user_id=99, chat_id=990)

            mock_exec.assert_not_called()
            mock_add.assert_not_called()
            mock_del.assert_not_called()


class TestActionDraftConcurrencyRaces(unittest.IsolatedAsyncioTestCase):
    """Concurrency race regression suite using file-backed SQLite in WAL mode."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "race_test.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()
        self.tmp_dir.cleanup()

    # 1. Confirm versus cancel race: terminal state is never overwritten, and both operations cannot win
    async def test_race_confirm_versus_cancel(self):
        """Verify racing confirm and cancel produce exactly one winner and terminal state is not overwritten."""
        draft = await create_action_draft(
            user_id=101,
            chat_id=201,
            action_type="schedule_reminder",
            payload={"text": "Doctor"},
            missing_fields=None,
        )

        res_confirm, res_cancel = await asyncio.gather(
            confirm_action_draft(draft.id, user_id=101, chat_id=201),
            cancel_action_draft(draft.id, user_id=101, chat_id=201),
        )

        d_conf, won = res_confirm
        d_canc = res_cancel

        # Query ground-truth in DB
        async with self.SessionLocal() as session:
            final_draft = await session.get(ActionDraft, draft.id)

        self.assertIn(final_draft.status, (DRAFT_STATUS_CONFIRMED, DRAFT_STATUS_CANCELLED))

        if won:
            # Confirm won: final state must remain confirmed, cancel could not overwrite it
            self.assertEqual(final_draft.status, DRAFT_STATUS_CONFIRMED)
            self.assertEqual(d_conf.status, DRAFT_STATUS_CONFIRMED)
            self.assertEqual(d_canc.status, DRAFT_STATUS_CONFIRMED)
        else:
            # Cancel won: confirmation must report won=False, final state must remain cancelled
            self.assertFalse(won)
            self.assertEqual(final_draft.status, DRAFT_STATUS_CANCELLED)
            self.assertEqual(d_canc.status, DRAFT_STATUS_CANCELLED)

    # 2. Confirm versus information update: a successful confirmation cannot be revived to an active state
    async def test_race_confirm_versus_update_information(self):
        """Verify update_action_draft_information cannot revive a draft confirmed by a concurrent transaction."""
        draft = await create_action_draft(
            user_id=102,
            chat_id=202,
            action_type="schedule_reminder",
            payload={"text": "Meeting"},
            missing_fields=None,
        )

        res_confirm, res_update = await asyncio.gather(
            confirm_action_draft(draft.id, user_id=102, chat_id=202),
            update_action_draft_information(draft.id, user_id=102, chat_id=202, payload_updates={"time": "15:00"}),
        )

        async with self.SessionLocal() as session:
            final_draft = await session.get(ActionDraft, draft.id)

        # If confirmation succeeded, final draft MUST be confirmed, never active
        d_conf, won = res_confirm
        if won:
            self.assertEqual(final_draft.status, DRAFT_STATUS_CONFIRMED)
        self.assertNotIn(final_draft.status, (DRAFT_STATUS_AWAITING_INFO, DRAFT_STATUS_PENDING_CONFIRMATION))

        # Subsequent update on the confirmed draft must be rejected and cannot revive it
        revive_attempt = await update_action_draft_information(draft.id, user_id=102, chat_id=202, payload_updates={"time": "16:00"})
        self.assertIsNone(revive_attempt)
        async with self.SessionLocal() as session:
            check_draft = await session.get(ActionDraft, draft.id)
            self.assertEqual(check_draft.status, DRAFT_STATUS_CONFIRMED)

    # 3. Confirm versus replacement creation: old row ends confirmed or cancelled, exactly one active draft exists
    async def test_race_confirm_versus_replacement_create(self):
        """Verify racing confirm and replacement create leaves old draft in terminal state and exactly one active draft."""
        draft1 = await create_action_draft(
            user_id=103,
            chat_id=203,
            action_type="schedule_reminder",
            payload={"text": "Original"},
            missing_fields=None,
        )

        res_confirm, draft2 = await asyncio.gather(
            confirm_action_draft(draft1.id, user_id=103, chat_id=203),
            create_action_draft(user_id=103, chat_id=203, action_type="schedule_reminder", payload={"text": "Replacement"}),
        )

        async with self.SessionLocal() as session:
            d1_final = await session.get(ActionDraft, draft1.id)
            active_res = await session.execute(
                select(ActionDraft).where(
                    ActionDraft.user_id == 103,
                    ActionDraft.chat_id == 203,
                    ActionDraft.status.in_((DRAFT_STATUS_AWAITING_INFO, DRAFT_STATUS_PENDING_CONFIRMATION)),
                )
            )
            active_drafts = active_res.scalars().all()

        # Old draft must end as terminal state (confirmed or cancelled), never active
        self.assertIn(d1_final.status, (DRAFT_STATUS_CONFIRMED, DRAFT_STATUS_CANCELLED))
        # Exactly one active draft exists (which is draft2)
        self.assertEqual(len(active_drafts), 1)
        self.assertEqual(active_drafts[0].id, draft2.id)

    # 4. Two concurrent creates for the same user/chat do not leak IntegrityError and leave exactly one active draft
    async def test_race_concurrent_creates_no_integrity_error(self):
        """Verify concurrent creates complete without unhandled IntegrityError and leave exactly one active draft."""
        created_drafts = await asyncio.gather(*[
            create_action_draft(
                user_id=104,
                chat_id=204,
                action_type="schedule_reminder",
                payload={"index": i},
                missing_fields=None,
            )
            for i in range(5)
        ])

        self.assertEqual(len(created_drafts), 5)

        async with self.SessionLocal() as session:
            active_res = await session.execute(
                select(ActionDraft).where(
                    ActionDraft.user_id == 104,
                    ActionDraft.chat_id == 204,
                    ActionDraft.status.in_((DRAFT_STATUS_AWAITING_INFO, DRAFT_STATUS_PENDING_CONFIRMATION)),
                )
            )
            active_drafts = active_res.scalars().all()

            # Query all drafts for this user/chat
            all_res = await session.execute(
                select(ActionDraft).where(
                    ActionDraft.user_id == 104,
                    ActionDraft.chat_id == 204,
                )
            )
            all_drafts = all_res.scalars().all()

        # Database-level partial unique index enforces at most 1 active draft
        self.assertEqual(len(active_drafts), 1)
        self.assertEqual(len(all_drafts), 5)
        cancelled_drafts = [d for d in all_drafts if d.status == DRAFT_STATUS_CANCELLED]
        self.assertEqual(len(cancelled_drafts), 4)

    # 5. Automatic expiry cannot overwrite a concurrently committed confirmed/cancelled state
    async def test_race_automatic_expiry_cannot_overwrite_terminal_state(self):
        """Verify automatic expiry does not overwrite confirmed or cancelled states."""
        draft = await create_action_draft(
            user_id=105,
            chat_id=205,
            action_type="schedule_reminder",
            payload={"text": "Near expiry"},
            ttl_seconds=1,
        )

        # Confirm draft
        d_conf, won = await confirm_action_draft(draft.id, user_id=105, chat_id=205)
        self.assertTrue(won)
        self.assertEqual(d_conf.status, DRAFT_STATUS_CONFIRMED)

        # Backdate expiry in DB
        async with self.SessionLocal() as session:
            d = await session.get(ActionDraft, draft.id)
            d.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
            await session.commit()

        # get_action_draft and get_active_action_draft must NOT overwrite confirmed to expired
        loaded = await get_action_draft(draft.id, user_id=105, chat_id=205)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, DRAFT_STATUS_CONFIRMED)

        active = await get_active_action_draft(user_id=105, chat_id=205)
        self.assertIsNone(active)

        async with self.SessionLocal() as session:
            d_final = await session.get(ActionDraft, draft.id)
            self.assertEqual(d_final.status, DRAFT_STATUS_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
