import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_
from bot.database.models import Base, User, DailyTranscriptionUsage
from bot.utils.search import extract_source_links, format_sources_html
from bot.utils.limits import get_daily_transcription_used_seconds, check_transcription_limit, record_transcription_usage
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.google_provider import GoogleProvider
from config import DAILY_TRANSCRIPTION_LIMIT_SECONDS

class MockAsyncStream:
    def __init__(self, items, usage=None):
        self.items = items
        self.usage = usage
        self.usage_metadata = usage

    def __aiter__(self):
        self._iter = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def resolve(self):
        pass

class TestPhase3SourcesAndLimits(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create in-memory SQLite engine for tests
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self):
        await self.engine.dispose()

    # --- 1. Source Link Extraction and Formatting Tests ---

    def test_extract_source_links_valid_and_deduplication(self):
        """Verify extraction of valid HTTP/HTTPS URLs with order preservation and deduplication."""
        text = (
            "[1] Python 3.12 Release\n"
            "LINK: https://python.org/release/3.12\n"
            "DETAILS: Some details\n\n"
            "[2] Duplicate Python\n"
            "LINK: https://python.org/release/3.12\n"
            "DETAILS: Duplicate\n\n"
            "[3] OpenAI\n"
            "LINK: https://openai.com\n"
            "DETAILS: OpenAI details\n"
        )
        links = extract_source_links(text)
        self.assertEqual(links, ["https://python.org/release/3.12", "https://openai.com"])

    def test_extract_source_links_rejection_and_cap(self):
        """Verify rejection of invalid schemes and cap at 5 links."""
        text = (
            "LINK: ftp://invalid.com\n"
            "LINK: javascript:alert(1)\n"
            "LINK: file:///etc/passwd\n"
            "LINK: https://site1.com\n"
            "LINK: https://site2.com\n"
            "LINK: https://site3.com\n"
            "LINK: http://site4.com\n"
            "LINK: https://site5.com\n"
            "LINK: https://site6.com\n"
        )
        links = extract_source_links(text, max_links=5)
        self.assertEqual(len(links), 5)
        self.assertEqual(links, [
            "https://site1.com",
            "https://site2.com",
            "https://site3.com",
            "http://site4.com",
            "https://site5.com"
        ])

    def test_format_sources_html_escaping(self):
        """Verify safe HTML escaping in formatted sources block."""
        urls = ["https://example.com/search?q=a&b=c", "https://test.com/<script>"]
        html_out = format_sources_html(urls)
        self.assertIn("<b>Джерела:</b>", html_out)
        self.assertIn('href="https://example.com/search?q=a&amp;b=c"', html_out)
        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)

    def test_format_sources_html_empty(self):
        """Verify empty string when no URLs provided."""
        self.assertEqual(format_sources_html([]), "")

    # --- 2. Provider Web Search Integration Tests ---

    async def test_openai_provider_sources_integration(self):
        """Verify OpenAIProvider yields source block when web_search is invoked."""
        provider = OpenAIProvider(api_key="test-key")

        # Mock chat completions response with a tool call and then final text
        fn_mock = MagicMock()
        fn_mock.name = "web_search"
        fn_mock.arguments = '{"query": "python news"}'

        tc_mock = MagicMock()
        tc_mock.index = 0
        tc_mock.id = "call_1"
        tc_mock.function = fn_mock

        tool_call_delta = MagicMock()
        tool_call_delta.delta.tool_calls = [tc_mock]
        tool_call_delta.delta.content = None
        tool_call_delta.choices = [tool_call_delta]

        final_text_delta = MagicMock()
        final_text_delta.delta.tool_calls = None
        final_text_delta.delta.content = "Here is the latest Python news."
        final_text_delta.choices = [final_text_delta]

        stream1 = MockAsyncStream([tool_call_delta])
        stream2 = MockAsyncStream([final_text_delta])
        provider.client.chat.completions.create = AsyncMock(side_effect=[stream1, stream2])

        fake_search_output = "LINK: https://python.org/news\nDETAILS: info"
        with patch("bot.ai.openai_provider.perform_search", AsyncMock(return_value=fake_search_output)):
            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "What's new in Python?"}],
                settings={"allow_search": True}
            ):
                chunks.append(chunk)

            full_reply = "".join(chunks)
            self.assertIn("Here is the latest Python news.", full_reply)
            self.assertIn("<b>Джерела:</b>", full_reply)
            self.assertIn('href="https://python.org/news"', full_reply)

    async def test_openai_provider_no_search_no_sources(self):
        """Verify OpenAIProvider does not append sources when no web_search occurred."""
        provider = OpenAIProvider(api_key="test-key")

        text_delta = MagicMock()
        text_delta.delta.tool_calls = None
        text_delta.delta.content = "Simple answer without search."
        text_delta.choices = [text_delta]

        stream = MockAsyncStream([text_delta])
        provider.client.chat.completions.create = AsyncMock(return_value=stream)

        chunks = []
        async for chunk in provider.generate_stream(
            messages=[{"role": "user", "content": "Hello"}],
            settings={"allow_search": True}
        ):
            chunks.append(chunk)

        full_reply = "".join(chunks)
        self.assertIn("Simple answer without search.", full_reply)
        self.assertNotIn("<b>Джерела:</b>", full_reply)

    async def test_google_provider_sources_integration(self):
        """Verify GoogleProvider yields source block when web_search is invoked."""
        provider = GoogleProvider(api_key="test-key")

        # Mock function call chunk
        fn_call_part = MagicMock()
        fn_call_part.name = "web_search"
        fn_call_part.args = {"query": "gemini update"}
        fn_candidate = MagicMock()
        fn_candidate.content.parts = [MagicMock(function_call=fn_call_part)]
        fn_chunk = MagicMock(candidates=[fn_candidate], text=None)

        # Mock final text chunk
        text_candidate = MagicMock()
        text_candidate.content.parts = [MagicMock(function_call=None)]
        text_chunk = MagicMock(candidates=[text_candidate], text="Gemini has been updated.")

        stream1 = MockAsyncStream([fn_chunk])
        stream2 = MockAsyncStream([text_chunk])

        mock_chat = MagicMock()
        mock_chat.send_message_async = AsyncMock(side_effect=[stream1, stream2])

        fake_search_output = "LINK: https://blog.google/technology/ai/gemini\nDETAILS: info"
        with patch("google.generativeai.GenerativeModel.start_chat", return_value=mock_chat), \
             patch("bot.ai.google_provider.perform_search", AsyncMock(return_value=fake_search_output)):
            chunks = []
            async for chunk in provider.generate_stream(
                messages=[{"role": "user", "content": "Gemini news"}],
                settings={"allow_search": True}
            ):
                chunks.append(chunk)

            full_reply = "".join(chunks)
            self.assertIn("Gemini has been updated.", full_reply)
            self.assertIn("<b>Джерела:</b>", full_reply)
            self.assertIn('href="https://blog.google/technology/ai/gemini"', full_reply)

    # --- 3. Daily Transcription Limit Tests ---

    async def test_daily_limit_fresh_allowance(self):
        """Verify fresh user has 0 seconds used and can transcribe within 3600s allowance."""
        with patch("bot.utils.limits.AsyncSessionLocal", self.SessionLocal):
            user_id = 1001
            used = await get_daily_transcription_used_seconds(user_id)
            self.assertEqual(used, 0)

            allowed, msg = await check_transcription_limit(user_id, duration_seconds=120)
            self.assertTrue(allowed)
            self.assertEqual(msg, "")

    async def test_daily_limit_exact_boundary_and_exhaustion(self):
        """Verify boundary behavior and refusal when 3600s is exceeded."""
        with patch("bot.utils.limits.AsyncSessionLocal", self.SessionLocal):
            user_id = 1002

            # Use 3500 seconds
            await record_transcription_usage(user_id, 3500)
            used = await get_daily_transcription_used_seconds(user_id)
            self.assertEqual(used, 3500)

            # Requesting 100 seconds (exact boundary: 3500 + 100 == 3600) -> Allowed
            allowed, msg = await check_transcription_limit(user_id, duration_seconds=100)
            self.assertTrue(allowed)

            # Requesting 101 seconds (3500 + 101 > 3600) -> Refused
            allowed, msg = await check_transcription_limit(user_id, duration_seconds=101)
            self.assertFalse(allowed)
            self.assertIn("Перевищено денний ліміт", msg)

            # Record another 100 seconds to reach 3600
            await record_transcription_usage(user_id, 100)
            # Now fully exhausted (used == 3600)
            allowed, msg = await check_transcription_limit(user_id, duration_seconds=10)
            self.assertFalse(allowed)
            self.assertIn("Ліміт транскрибації на сьогодні вичерпано", msg)

    async def test_daily_limit_user_isolation(self):
        """Verify usage limits are strictly isolated per user."""
        with patch("bot.utils.limits.AsyncSessionLocal", self.SessionLocal):
            user1 = 2001
            user2 = 2002

            # Exhaust user1
            await record_transcription_usage(user1, DAILY_TRANSCRIPTION_LIMIT_SECONDS)
            u1_allowed, _ = await check_transcription_limit(user1, duration_seconds=30)
            self.assertFalse(u1_allowed)

            # User2 must still have full allowance
            u2_allowed, _ = await check_transcription_limit(user2, duration_seconds=30)
            self.assertTrue(u2_allowed)
            self.assertEqual(await get_daily_transcription_used_seconds(user2), 0)

    async def test_daily_limit_utc_day_reset(self):
        """Verify usage from a previous UTC calendar day does not affect today's allowance."""
        with patch("bot.utils.limits.AsyncSessionLocal", self.SessionLocal):
            user_id = 3001
            yesterday_utc = (datetime.now(timezone.utc) - timedelta(days=1)).date()

            # Insert usage for yesterday
            async with self.SessionLocal() as session:
                u = User(id=user_id, settings={}, system_prompt='test')
                session.add(u)
                await session.flush()

                yesterday_usage = DailyTranscriptionUsage(
                    user_id=user_id,
                    usage_date=yesterday_utc,
                    seconds_used=DAILY_TRANSCRIPTION_LIMIT_SECONDS
                )
                session.add(yesterday_usage)
                await session.commit()

            # Today's usage must be 0 and allowed
            used_today = await get_daily_transcription_used_seconds(user_id)
            self.assertEqual(used_today, 0)

            allowed, _ = await check_transcription_limit(user_id, duration_seconds=60)
            self.assertTrue(allowed)

    async def test_daily_limit_unique_constraint_and_concurrent_recovery(self):
        """Verify unique constraint on (user_id, usage_date) and recovery from concurrent insert conflict."""
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy import UniqueConstraint
        from contextlib import asynccontextmanager

        # 1. Verify model metadata has unique constraint on user_id and usage_date
        unique_constraints = [
            c for c in DailyTranscriptionUsage.__table__.constraints
            if isinstance(c, UniqueConstraint)
        ]
        has_uq = any(
            set(col.name for col in uc.columns) == {"user_id", "usage_date"}
            for uc in unique_constraints
        )
        self.assertTrue(has_uq, "DailyTranscriptionUsage must have a UniqueConstraint on (user_id, usage_date)")

        with patch("bot.utils.limits.AsyncSessionLocal", self.SessionLocal):
            user_id = 4001
            today_utc = datetime.now(timezone.utc).date()

            # Ensure user exists
            async with self.SessionLocal() as session:
                u = User(id=user_id, settings={}, system_prompt='test')
                session.add(u)
                await session.commit()

            # 2. Verify direct duplicate insert fails with IntegrityError
            async with self.SessionLocal() as session:
                row1 = DailyTranscriptionUsage(user_id=user_id, usage_date=today_utc, seconds_used=100)
                session.add(row1)
                await session.commit()

                row2 = DailyTranscriptionUsage(user_id=user_id, usage_date=today_utc, seconds_used=200)
                session.add(row2)
                with self.assertRaises(IntegrityError):
                    await session.commit()

            # 3. Force the actual IntegrityError recovery branch in record_transcription_usage.
            recovered_usage = MagicMock(seconds_used=100)
            session = MagicMock()
            session.execute = AsyncMock(side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=recovered_usage)),
            ])
            session.commit = AsyncMock(side_effect=[IntegrityError("insert", {}, Exception("duplicate")), None])
            session.rollback = AsyncMock()

            @asynccontextmanager
            async def failed_insert_session():
                yield session

            with patch("bot.utils.limits.AsyncSessionLocal", return_value=failed_insert_session()):
                await record_transcription_usage(user_id, 300)

            self.assertEqual(recovered_usage.seconds_used, 400)
            self.assertEqual(session.execute.await_count, 2)
            self.assertEqual(session.commit.await_count, 2)
            session.rollback.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
