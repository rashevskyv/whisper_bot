import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.utils.media import get_ffmpeg_exe, validate_audio_size, MAX_AUDIO_SIZE_BYTES
from bot.ai.openai_provider import OpenAIProvider

class TestMediaAndTranscription(unittest.TestCase):
    def test_ffmpeg_resolution_order_path(self):
        """Verify that get_ffmpeg_exe prefers executable found in PATH."""
        with patch("shutil.which", return_value="C:\\fake\\ffmpeg.exe"), \
             patch("os.path.isfile", return_value=True):
            resolved = get_ffmpeg_exe()
            self.assertEqual(resolved, "C:\\fake\\ffmpeg.exe")

    def test_ffmpeg_resolution_bundled(self):
        """Verify that get_ffmpeg_exe falls back to imageio-ffmpeg when PATH has no ffmpeg."""
        with patch("shutil.which", return_value=None), \
             patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="C:\\bundled\\ffmpeg.exe"), \
             patch("os.path.isfile", return_value=True):
            resolved = get_ffmpeg_exe()
            self.assertEqual(resolved, "C:\\bundled\\ffmpeg.exe")

    def test_ffmpeg_resolution_missing(self):
        """Verify that get_ffmpeg_exe raises Ukrainian error when FFmpeg is not found."""
        with patch("shutil.which", return_value=None), \
             patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=Exception("not found")):
            with self.assertRaises(RuntimeError) as ctx:
                get_ffmpeg_exe()
            self.assertIn("Для транскрибації відео потрібен FFmpeg", str(ctx.exception))

    def test_validate_audio_size_pass(self):
        """Verify that audio files under 25MB pass validation."""
        with patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=MAX_AUDIO_SIZE_BYTES - 100):
            # Should not raise
            validate_audio_size("fake.mp3")

    def test_validate_audio_size_fail(self):
        """Verify that audio files over 25MB raise RuntimeError."""
        with patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=MAX_AUDIO_SIZE_BYTES + 100):
            with self.assertRaises(RuntimeError) as ctx:
                validate_audio_size("large.mp3")
            self.assertIn("перевищує ліміт 25 МБ", str(ctx.exception))

    def test_openai_transcribe_parameters_with_language(self):
        """Verify OpenAIProvider transcribe sends model='gpt-transcribe' and extra_body languages."""
        provider = OpenAIProvider(api_key="test-key")
        mock_create = AsyncMock(return_value=MagicMock(text="test transcription"))
        provider.client.audio.transcriptions.create = mock_create

        async def run_test():
            with patch("builtins.open", MagicMock()):
                res = await provider.transcribe("dummy.mp3", language="uk")
                self.assertEqual(res, "test transcription")
                mock_create.assert_called_once()
                call_kwargs = mock_create.call_args.kwargs
                self.assertEqual(call_kwargs.get("model"), "gpt-transcribe")
                self.assertEqual(call_kwargs.get("extra_body"), {"languages": ["uk"]})
                self.assertNotIn("language", call_kwargs)

        asyncio.run(run_test())

    def test_openai_transcribe_parameters_without_language(self):
        """Verify OpenAIProvider transcribe omits extra_body when language is None."""
        provider = OpenAIProvider(api_key="test-key")
        mock_create = AsyncMock(return_value=MagicMock(text="test transcription"))
        provider.client.audio.transcriptions.create = mock_create

        async def run_test():
            with patch("builtins.open", MagicMock()):
                res = await provider.transcribe("dummy.mp3", language=None)
                self.assertEqual(res, "test transcription")
                mock_create.assert_called_once()
                call_kwargs = mock_create.call_args.kwargs
                self.assertEqual(call_kwargs.get("model"), "gpt-transcribe")
                self.assertNotIn("extra_body", call_kwargs)
                self.assertNotIn("language", call_kwargs)

        asyncio.run(run_test())

    def test_openai_transcribe_raises_on_api_error(self):
        """Verify OpenAIProvider transcribe re-raises exceptions rather than returning Error string."""
        provider = OpenAIProvider(api_key="test-key")
        mock_create = AsyncMock(side_effect=Exception("API connection failure"))
        provider.client.audio.transcriptions.create = mock_create

        async def run_test():
            with patch("builtins.open", MagicMock()):
                with self.assertRaises(Exception) as ctx:
                    await provider.transcribe("dummy.mp3", language="uk")
                self.assertIn("API connection failure", str(ctx.exception))

        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
