
import os
import shutil
import logging
import asyncio
from typing import List, Optional
from config import TEMP_DIR

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB OpenAI limit

def get_ffmpeg_exe() -> str:
    """
    Знаходить виконуваний файл FFmpeg:
    1. У системному PATH (shutil.which)
    2. У встановленому пакеті imageio-ffmpeg (через moviepy)
    Якщо FFmpeg не знайдено, викидає RuntimeError.
    """
    # 1. Перевірка системного PATH
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path and os.path.isfile(ffmpeg_path):
        return ffmpeg_path

    # 2. Перевірка imageio-ffmpeg
    try:
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_path and os.path.isfile(ffmpeg_path):
            return ffmpeg_path
    except Exception as e:
        logger.debug(f"imageio_ffmpeg lookup failed: {e}")

    raise RuntimeError("Для транскрибації відео потрібен FFmpeg.")

async def _run_ffmpeg_convert(ffmpeg_bin: str, video_path: str, audio_path: str, bitrate: str = "64k") -> None:
    """Виконує конвертацію аудіо з відео за допомогою FFmpeg."""
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", bitrate,
        audio_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logger.error(f"FFmpeg conversion error (code {process.returncode}): {stderr.decode(errors='replace')}")
        raise RuntimeError("Помилка конвертації відео")

async def extract_audio(video_path: str) -> str:
    """
    Витягує аудіо з відеофайлу за допомогою FFmpeg.
    Формат: MP3, моно, 16kHz, оптимізований бітрейт для розпізнавання мови.
    Якщо розмір перевищує 25 МБ, виконує одну повторну спробу з нижчим бітрейтом.
    """
    ffmpeg_bin = get_ffmpeg_exe()
    base_name = os.path.splitext(video_path)[0]
    audio_path = f"{base_name}.mp3"

    # Спроба 1: стандартний бітрейт для мови (64k)
    await _run_ffmpeg_convert(ffmpeg_bin, video_path, audio_path, bitrate="64k")

    # Перевірка розміру файлу
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > MAX_AUDIO_SIZE_BYTES:
        logger.warning(
            f"Extracted audio size ({os.path.getsize(audio_path)} bytes) exceeds 25 MB. "
            "Retrying with lower bitrate (24k)..."
        )
        # Спроба 2: нижчий бітрейт (24k)
        await _run_ffmpeg_convert(ffmpeg_bin, video_path, audio_path, bitrate="24k")

        if os.path.exists(audio_path) and os.path.getsize(audio_path) > MAX_AUDIO_SIZE_BYTES:
            raise RuntimeError("Розмір файлу перевищує ліміт 25 МБ для транскрибації.")

    return audio_path

def validate_audio_size(file_path: str) -> None:
    """Перевіряє розмір аудіофайлу перед відправкою в API (ліміт 25 МБ)."""
    if os.path.exists(file_path) and os.path.getsize(file_path) > MAX_AUDIO_SIZE_BYTES:
        raise RuntimeError("Розмір файлу перевищує ліміт 25 МБ для транскрибації.")

async def download_file(telegram_file, file_id: str) -> str:
    """
    Завантажує файл з Telegram на диск.
    Повертає шлях до файлу.
    """
    file_ext = os.path.splitext(telegram_file.file_path)[1]
    if not file_ext:
        file_ext = ".temp"

    file_path = os.path.join(TEMP_DIR, f"{file_id}{file_ext}")
    await telegram_file.download_to_drive(file_path)
    return file_path

def cleanup_files(paths: list):
    """Видаляє тимчасові файли"""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.error(f"Error removing file {path}: {e}")