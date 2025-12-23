import os
import logging
import asyncio
import yt_dlp
from config import TEMP_DIR

logger = logging.getLogger(__name__)

async def download_media_direct(url: str) -> dict:
    """
    Завантажує відео/фото через yt-dlp.
    Використовує маскування під Android для обходу 403 помилок YouTube.
    """
    loop = asyncio.get_running_loop()
    
    # Налаштування yt-dlp для обходу блокувань
    ydl_opts = {
        'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
        'format': 'best[filesize<50M]/best', # Пріоритет: до 50МБ, або найкраще
        'max_filesize': 50 * 1024 * 1024, # Жорсткий ліміт для скачування
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'ignoreerrors': True,
        
        # --- ANTI-BLOCK SETTINGS ---
        # Емуляція клієнта Android (найкраще працює проти 403)
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'player_skip': ['webpage', 'configs', 'js'],
                'zeroday': ['1']
            }
        },
        # Фейковий User-Agent
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        }
    }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                # 1. Спроба отримати інфо
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                
                # Перевірка на тривалість (опціонально, щоб не качати фільми)
                duration = info.get('duration', 0)
                if duration > 1200: # 20 хвилин
                    logger.warning("Video too long, skipping")
                    return None

                # 2. Скачування
                info = ydl.extract_info(url, download=True)
                if not info:
                    return None

                filename = ydl.prepare_filename(info)
                
                if not os.path.exists(filename):
                    return None

                # Формуємо підпис
                title = info.get('title', 'Video')
                caption = f"🎥 <b>{title}</b>\n🔗 <a href='{url}'>Original Link</a>"

                return {
                    'path': filename,
                    'type': 'video', 
                    'title': title,
                    'caption': caption
                }
            except yt_dlp.utils.DownloadError as e:
                logger.warning(f"yt-dlp download warning: {e}")
                return None
            except Exception as e:
                logger.error(f"General download error: {e}")
                return None

    return await loop.run_in_executor(None, _download)