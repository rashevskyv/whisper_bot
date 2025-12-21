
import os
import logging
import asyncio
from config import TEMP_DIR

logger = logging.getLogger(__name__)

async def download_file(telegram_file, file_id: str) -> str:
    """
    Завантажує файл з Telegram на диск.
    Повертає шлях до файлу.
    """
    # Визначаємо розширення файлу
    file_ext = os.path.splitext(telegram_file.file_path)[1]
    if not file_ext:
        file_ext = ".temp"
        
    file_path = os.path.join(TEMP_DIR, f"{file_id}{file_ext}")
    
    await telegram_file.download_to_drive(file_path)
    return file_path

async def extract_audio(video_path: str) -> str:
    """
    Витягує аудіо з відеофайлу за допомогою FFmpeg.
    Повертає шлях до аудіофайлу (.mp3).
    """
    base_name = os.path.splitext(video_path)[0]
    audio_path = f"{base_name}.mp3"

    # Команда FFmpeg: -i input -q:a 0 (high quality) -map a (take audio) output
    # -y перезаписати якщо існує
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vn", # No video
        "-acodec", "libmp3lame",
        "-q:a", "2",
        audio_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logger.error(f"FFmpeg error: {stderr.decode()}")
        raise RuntimeError("Помилка конвертації відео")

    return audio_path

def cleanup_files(paths: list):
    """Видаляє тимчасові файли"""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.error(f"Error removing file {path}: {e}")