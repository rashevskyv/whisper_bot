import os
import logging
import asyncio
import yt_dlp
from config import TEMP_DIR

logger = logging.getLogger(__name__)

async def download_media_direct(url: str) -> dict:
    loop = asyncio.get_running_loop()
    
    logger.info(f"📥 [Downloader] Starting download for: {url}")
    
    ydl_opts = {
        'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
        'format': 'best[filesize<50M]/best',
        'max_filesize': 50 * 1024 * 1024,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'ignoreerrors': False, # Want to catch errors
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'web_creator'],
                'player_skip': ['webpage', 'configs', 'js'],
                'zeroday': ['1']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    logger.warning(f"⚠️ [Downloader] No info extracted for {url}")
                    return None
                
                info = ydl.extract_info(url, download=True)
                if not info: return None

                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    logger.error(f"❌ [Downloader] File not found after download: {filename}")
                    return None

                return {
                    'path': filename,
                    'type': 'video', 
                    'title': info.get('title', ''),
                    'caption': None 
                }
            except Exception as e:
                # ЛОГУЄМО КОНКРЕТНУ ПОМИЛКУ ЗАВАНТАЖЕННЯ (Twitter/YouTube)
                logger.error(f"❌ [Downloader] yt-dlp failed for {url}: {e}")
                return None

    return await loop.run_in_executor(None, _download)