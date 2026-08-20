import unittest
import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN"
os.environ["ENCRYPTION_KEY"] = "8Z6wY6uP04B4uE6_7V8M3aQ1bC2dE3fG4hI5jK6lM7o="
os.environ["ADMIN_IDS"] = "111,222"

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from bot.database.models import Base, User, DownloadQueue
from bot.handlers.common import get_user_model_settings
from bot.handlers.settings import toggle_video_repost
from bot.handlers.commands import video_cmd
from bot.handlers.text import handle_text

class TestVideoRepost(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.temp_db.name}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.patchers = [
            patch("bot.database.session.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.common.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.settings.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.commands.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.text.AsyncSessionLocal", self.SessionLocal),
            patch("bot.utils.helpers.AsyncSessionLocal", self.SessionLocal),
            patch("bot.handlers.commands.ADMIN_IDS", [111, 222]),
            patch("bot.handlers.settings.ADMIN_IDS", [111, 222]),
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

    async def test_get_user_model_settings_defaults(self):
        # Private chat default
        settings_p = await get_user_model_settings(123)
        self.assertIn("video_repost", settings_p)
        self.assertTrue(settings_p["video_repost"])

        # Group chat default
        settings_g = await get_user_model_settings(-100123)
        self.assertIn("video_repost", settings_g)
        self.assertTrue(settings_g["video_repost"])

    async def test_toggle_video_repost_callback(self):
        async with self.SessionLocal() as session:
            user = User(id=-100123, username="grp", full_name="Group", settings={"video_repost": True})
            session.add(user)
            await session.commit()

        update = MagicMock()
        update.effective_chat.id = -100123
        update.effective_chat.type = "supergroup"
        update.effective_chat.title = "Group"
        update.effective_chat.username = "grp"
        update.effective_user.id = 111
        update.effective_user.username = "admin"
        update.effective_user.first_name = "Admin"
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        context = MagicMock()
        context.bot.get_chat_member = AsyncMock()
        member_mock = MagicMock()
        member_mock.status = "administrator"
        context.bot.get_chat_member.return_value = member_mock

        # First toggle: True -> False
        await toggle_video_repost(update, context)
        async with self.SessionLocal() as session:
            u = await session.get(User, -100123)
            self.assertFalse(u.settings.get("video_repost"))

        # Second toggle: False -> True
        await toggle_video_repost(update, context)
        async with self.SessionLocal() as session:
            u = await session.get(User, -100123)
            self.assertTrue(u.settings.get("video_repost"))

    async def test_video_cmd_single_chat_on_off(self):
        update = MagicMock()
        update.effective_chat.id = -100555
        update.effective_chat.type = "supergroup"
        update.effective_chat.title = "Test Group"
        update.effective_chat.username = "testgroup"
        update.effective_user.id = 111
        update.effective_user.username = "admin"
        update.effective_user.first_name = "Admin"
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.get_chat_member = AsyncMock()
        member_mock = MagicMock()
        member_mock.status = "creator"
        context.bot.get_chat_member.return_value = member_mock

        # Turn OFF
        context.args = ["off"]
        await video_cmd(update, context)
        update.message.reply_text.assert_called()
        args, _ = update.message.reply_text.call_args
        self.assertIn("вимкнено", args[0])

        async with self.SessionLocal() as session:
            u = await session.get(User, -100555)
            self.assertFalse(u.settings.get("video_repost"))

        # Turn ON
        context.args = ["on"]
        await video_cmd(update, context)
        args, _ = update.message.reply_text.call_args
        self.assertIn("увімкнено", args[0])

        async with self.SessionLocal() as session:
            u = await session.get(User, -100555)
            self.assertTrue(u.settings.get("video_repost"))

    async def test_video_cmd_bulk_all_groups(self):
        # Create multiple group users and one private user
        async with self.SessionLocal() as session:
            g1 = User(id=-1001, username="g1", full_name="Group 1", settings={"video_repost": True})
            g2 = User(id=-1002, username="g2", full_name="Group 2", settings={"video_repost": True})
            p1 = User(id=999, username="p1", full_name="Person 1", settings={"video_repost": True})
            session.add_all([g1, g2, p1])
            await session.commit()

        update = MagicMock()
        update.effective_chat.id = 111  # admin user
        update.effective_chat.type = "private"
        update.effective_chat.username = "admin"
        update.effective_chat.title = "Admin"
        update.effective_user.id = 111
        update.effective_user.username = "admin"
        update.effective_user.first_name = "Admin"
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["all", "off"]

        # Run bulk disable
        await video_cmd(update, context)
        update.message.reply_text.assert_called()
        args, _ = update.message.reply_text.call_args
        self.assertIn("Масове налаштування застосовано", args[0])
        self.assertIn("2 груп", args[0])

        async with self.SessionLocal() as session:
            res_g1 = await session.get(User, -1001)
            res_g2 = await session.get(User, -1002)
            res_p1 = await session.get(User, 999)
            self.assertFalse(res_g1.settings.get("video_repost"))
            self.assertFalse(res_g2.settings.get("video_repost"))
            self.assertTrue(res_p1.settings.get("video_repost"))  # Private chat unchanged

    async def test_video_cmd_non_admin_forbidden(self):
        update = MagicMock()
        update.effective_chat.id = -100999
        update.effective_chat.type = "supergroup"
        update.effective_chat.title = "Grp"
        update.effective_chat.username = "grp"
        update.effective_user.id = 77777  # Not admin
        update.effective_user.username = "user7"
        update.effective_user.first_name = "User"
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.get_chat_member = AsyncMock()
        member_mock = MagicMock()
        member_mock.status = "member"
        context.bot.get_chat_member.return_value = member_mock

        context.args = ["off"]
        await video_cmd(update, context)
        update.message.reply_text.assert_called()
        args, _ = update.message.reply_text.call_args
        self.assertIn("доступна лише адміністраторам", args[0])

    async def test_handle_text_userbot_disabled(self):
        # Chat with video_repost = False
        async with self.SessionLocal() as session:
            u = User(id=-100888, username="grp", full_name="Group", settings={"video_repost": False})
            session.add(u)
            await session.commit()

        update = MagicMock()
        update.effective_chat.id = -100888
        update.effective_chat.type = "supergroup"
        update.effective_chat.username = "grp"
        update.effective_chat.title = "Group"
        update.effective_user.id = 123
        update.effective_user.username = "alice"
        update.effective_user.first_name = "Alice"
        update.message = MagicMock()
        update.message.text = "https://www.tiktok.com/@user/video/1234567890"
        update.message.reply_to_message = None
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.id = 999999
        context.bot.username = "testbot"

        await handle_text(update, context)

        # Verify nothing was added to DownloadQueue
        async with self.SessionLocal() as session:
            result = await session.execute(select(DownloadQueue))
            items = result.scalars().all()
            self.assertEqual(len(items), 0)

    async def test_handle_text_userbot_enabled(self):
        # Chat with video_repost = True
        async with self.SessionLocal() as session:
            u = User(id=-100777, username="grp", full_name="Group", settings={"video_repost": True})
            session.add(u)
            await session.commit()

        update = MagicMock()
        update.effective_chat.id = -100777
        update.effective_chat.type = "supergroup"
        update.effective_chat.username = "grp"
        update.effective_chat.title = "Group"
        update.effective_user.id = 123
        update.effective_user.username = "bob"
        update.effective_user.first_name = "Bob"
        update.message = MagicMock()
        update.message.message_id = 42
        update.message.text = "https://www.instagram.com/reel/C123456/"
        update.message.reply_to_message = None
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.id = 999999
        context.bot.username = "testbot"

        await handle_text(update, context)

        # Verify task was added to DownloadQueue
        async with self.SessionLocal() as session:
            result = await session.execute(select(DownloadQueue))
            items = result.scalars().all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].user_id, -100777)
            self.assertIn("instagram.com/reel/C123456/", items[0].link)

    @patch("bot.handlers.text.download_media_direct")
    async def test_handle_text_direct_dl_toggle(self, mock_direct_dl):
        mock_direct_dl.return_value = None

        # 1. Disabled
        async with self.SessionLocal() as session:
            u = User(id=-100666, username="grp", full_name="Group", settings={"video_repost": False})
            session.add(u)
            await session.commit()

        update = MagicMock()
        update.effective_chat.id = -100666
        update.effective_chat.type = "supergroup"
        update.effective_chat.username = "grp"
        update.effective_chat.title = "Group"
        update.effective_user.id = 123
        update.effective_user.username = "charlie"
        update.effective_user.first_name = "Charlie"
        update.message = MagicMock()
        update.message.text = "https://twitter.com/user/status/123456789"
        update.message.reply_to_message = None
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.id = 999999
        context.bot.username = "testbot"

        await handle_text(update, context)
        mock_direct_dl.assert_not_called()

        # 2. Enabled
        async with self.SessionLocal() as session:
            u = await session.get(User, -100666)
            u.settings = {"video_repost": True}
            await session.commit()

        await handle_text(update, context)
        mock_direct_dl.assert_called_once_with("https://twitter.com/user/status/123456789")

if __name__ == "__main__":
    unittest.main()
