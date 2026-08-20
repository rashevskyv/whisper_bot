import unittest
import asyncio
import os
import tempfile
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Set env for test database
os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN"
os.environ["ENCRYPTION_KEY"] = "8Z6wY6uP04B4uE6_7V8M3aQ1bC2dE3fG4hI5jK6lM7o="
os.environ["ADMIN_IDS"] = "111,222"

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import delete
from bot.database.models import Base, DownloadQueue, User
from bot.utils.queue_manager import get_queue_stats, clear_pending_tasks, clear_all_tasks
from bot.handlers.commands import queue_cmd
from bot.handlers.settings import queue_menu, queue_clear_pending, queue_clear_all

class TestQueueManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.temp_db.name}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.database.session.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.queue_manager.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.settings.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.commands.AsyncSessionLocal", self.SessionLocal),
        ]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        await self.engine.dispose()
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except:
                pass

    async def test_get_queue_stats_empty(self):
        stats = await get_queue_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats["done"], 0)
        self.assertEqual(stats["error"], 0)
        self.assertEqual(stats["total"], 0)

    async def test_get_queue_stats_populated(self):
        async with self.SessionLocal() as session:
            session.add_all([
                DownloadQueue(user_id=1, link="http://example.com/1", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/2", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/3", status="processing"),
                DownloadQueue(user_id=1, link="http://example.com/4", status="done"),
                DownloadQueue(user_id=1, link="http://example.com/5", status="timeout"),
                DownloadQueue(user_id=1, link="http://example.com/6", status="error"),
            ])
            await session.commit()

        stats = await get_queue_stats()
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["processing"], 1)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(stats["error"], 2)
        self.assertEqual(stats["total"], 6)

    async def test_clear_pending_tasks(self):
        async with self.SessionLocal() as session:
            session.add_all([
                DownloadQueue(user_id=1, link="http://example.com/1", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/2", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/3", status="processing"),
                DownloadQueue(user_id=1, link="http://example.com/4", status="done"),
            ])
            await session.commit()

        deleted = await clear_pending_tasks()
        self.assertEqual(deleted, 3)

        stats = await get_queue_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(stats["total"], 1)

    async def test_clear_all_tasks(self):
        async with self.SessionLocal() as session:
            session.add_all([
                DownloadQueue(user_id=1, link="http://example.com/1", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/2", status="done"),
            ])
            await session.commit()

        deleted = await clear_all_tasks()
        self.assertEqual(deleted, 2)

        stats = await get_queue_stats()
        self.assertEqual(stats["total"], 0)

    async def test_queue_command_clear(self):
        async with self.SessionLocal() as session:
            session.add_all([
                DownloadQueue(user_id=1, link="http://example.com/1", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/2", status="processing"),
            ])
            await session.commit()

        update = MagicMock()
        update.effective_chat.type = "private"
        update.effective_chat.id = 111
        update.effective_user.id = 111
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["clear"]

        await queue_cmd(update, context)

        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("Чергу очищено", args[0])
        self.assertIn("2", args[0])

        stats = await get_queue_stats()
        self.assertEqual(stats["total"], 0)

    async def test_queue_command_view(self):
        update = MagicMock()
        update.effective_chat.type = "private"
        update.effective_chat.id = 111
        update.effective_user.id = 111
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = []

        await queue_cmd(update, context)

        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("Черга завантажень", args[0])
        self.assertIsNotNone(kwargs.get("reply_markup"))

    async def test_queue_menu_callbacks(self):
        async with self.SessionLocal() as session:
            session.add_all([
                DownloadQueue(user_id=1, link="http://example.com/1", status="pending"),
                DownloadQueue(user_id=1, link="http://example.com/2", status="done"),
            ])
            await session.commit()

        update = MagicMock()
        update.effective_chat.type = "private"
        update.effective_chat.id = 111
        update.effective_user.id = 111
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        context = MagicMock()

        # Test clear pending callback
        await queue_clear_pending(update, context)
        update.callback_query.answer.assert_called()

        stats = await get_queue_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["done"], 1)

        # Test clear all callback
        await queue_clear_all(update, context)
        stats = await get_queue_stats()
        self.assertEqual(stats["total"], 0)

if __name__ == "__main__":
    unittest.main()
