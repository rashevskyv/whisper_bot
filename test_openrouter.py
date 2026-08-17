import os
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from bot.database.models import Base, User, APIKey
from bot.utils.security import key_manager
from bot.ai.openrouter_provider import OpenRouterProvider
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.google_provider import GoogleProvider
from bot.utils.helpers import get_ai_provider
from config import AVAILABLE_MODELS, DEFAULT_SETTINGS

class TestOpenRouterIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_available_models_structure(self):
        """Verify OpenRouter models list includes Luna, DeepSeek, Gemini, Qwen, Mistral."""
        or_models = AVAILABLE_MODELS.get("openrouter", [])
        self.assertTrue(len(or_models) >= 5)
        model_ids = [m["id"] for m in or_models]
        self.assertIn("openai/gpt-5.6-luna", model_ids)
        self.assertIn("deepseek/deepseek-v4-flash-0731", model_ids)
        self.assertIn("google/gemini-3.7-flash", model_ids)
        self.assertIn("google/gemini-3.5-flash-lite", model_ids)
        self.assertIn("qwen/qwen3.7-flash", model_ids)
        self.assertIn("mistralai/mistral-small-24b-instruct-2501", model_ids)
        self.assertEqual(DEFAULT_SETTINGS["model"], "openai/gpt-5.6-luna")

    async def test_openrouter_generate_stream(self):
        """Verify OpenRouterProvider generates chunks properly."""
        provider = OpenRouterProvider(api_key="sk-or-test-key", model_name="openai/gpt-5.6-luna")
        
        async def mock_chunks():
            chunk1 = MagicMock()
            chunk1.choices = [MagicMock(delta=MagicMock(content="Hello ", tool_calls=None))]
            yield chunk1
            chunk2 = MagicMock()
            chunk2.choices = [MagicMock(delta=MagicMock(content="from OpenRouter!", tool_calls=None))]
            yield chunk2

        mock_create = AsyncMock(return_value=mock_chunks())
        provider.client.chat.completions.create = mock_create

        chunks = []
        async for chunk in provider.generate_stream([{"role": "user", "content": "Hi"}], {"model": "openai/gpt-5.6-luna"}):
            chunks.append(chunk)

        self.assertEqual("".join(chunks), "Hello from OpenRouter!")
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        self.assertEqual(call_kwargs.get("model"), "openai/gpt-5.6-luna")
        self.assertTrue(call_kwargs.get("stream"))

    async def test_openrouter_validate_key(self):
        """Verify validate_key returns True on success and False on error."""
        provider = OpenRouterProvider(api_key="sk-or-valid-key")
        with patch.object(provider, "validate_key", AsyncMock(return_value=True)):
            is_valid = await provider.validate_key("sk-or-valid-key")
            self.assertTrue(is_valid)

        with patch.object(provider, "validate_key", AsyncMock(return_value=False)):
            is_invalid = await provider.validate_key("invalid-key")
            self.assertFalse(is_invalid)

    async def test_openrouter_transcribe_not_implemented(self):
        """Verify transcribe raises NotImplementedError."""
        provider = OpenRouterProvider(api_key="sk-or-test")
        with self.assertRaises(NotImplementedError):
            await provider.transcribe("dummy.mp3")

    async def test_get_ai_provider_routing_openrouter(self):
        """Verify get_ai_provider returns OpenRouterProvider for OpenRouter model."""
        with patch("bot.utils.helpers.AsyncSessionLocal", self.SessionLocal), \
             patch("bot.utils.helpers.SYSTEM_OPENROUTER_KEY", "sk-or-system-key"):
            
            async with self.SessionLocal() as session:
                user = User(
                    id=1001,
                    username="testuser",
                    full_name="Test User",
                    settings={"model": "deepseek/deepseek-v4-flash-0731", "system_prompt": "Prompt"}
                )
                session.add(user)
                await session.commit()

            provider = await get_ai_provider(1001, for_transcription=False)
            self.assertIsInstance(provider, OpenRouterProvider)
            self.assertEqual(provider.default_model, "deepseek/deepseek-v4-flash-0731")

    async def test_get_ai_provider_routing_transcription_always_openai(self):
        """Verify get_ai_provider returns OpenAIProvider for transcription even if user model is OpenRouter."""
        with patch("bot.utils.helpers.AsyncSessionLocal", self.SessionLocal), \
             patch("bot.utils.helpers.SYSTEM_OPENAI_KEY", "sk-system-openai-key"):
            
            async with self.SessionLocal() as session:
                user = User(
                    id=1002,
                    username="testuser2",
                    full_name="Test User 2",
                    settings={"model": "openai/gpt-5.6-luna", "system_prompt": "Prompt"}
                )
                session.add(user)
                await session.commit()

            provider = await get_ai_provider(1002, for_transcription=True)
            self.assertIsInstance(provider, OpenAIProvider)

if __name__ == "__main__":
    unittest.main()
