import logging
import asyncio
import re
import html
from typing import List
from urllib.parse import urlparse
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

def extract_source_links(search_text: str, max_links: int = 5) -> List[str]:
    """
    Витягує список безпечних абсолютних HTTP/HTTPS посилань з тексту результатів пошуку.
    Дедуплікує зі збереженням первинного порядку, повертає щонайбільше max_links.
    """
    if not search_text or not isinstance(search_text, str):
        return []

    # Спочатку шукаємо шаблони 'LINK: <url>'
    found_urls = re.findall(r'LINK:\s*(https?://[^\s<>"]+)', search_text, re.IGNORECASE)
    if not found_urls:
        found_urls = re.findall(r'https?://[^\s<>"]+', search_text, re.IGNORECASE)

    valid_urls = []
    seen = set()
    for raw_url in found_urls:
        clean_url = raw_url.rstrip('.,;:)"\'')
        try:
            parsed = urlparse(clean_url)
            if parsed.scheme in ('http', 'https') and parsed.netloc:
                if clean_url not in seen:
                    seen.add(clean_url)
                    valid_urls.append(clean_url)
                    if len(valid_urls) >= max_links:
                        break
        except Exception:
            continue

    return valid_urls

def format_sources_html(urls: List[str]) -> str:
    """
    Форматує список посилань у компактний Telegram HTML-блок.
    Повертає порожній рядок, якщо посилань немає.
    """
    if not urls:
        return ""

    lines = ["\n\n<b>Джерела:</b>"]
    for i, url in enumerate(urls, 1):
        safe_url = html.escape(url, quote=True)
        lines.append(f'• <a href="{safe_url}">Джерело {i}</a>')

    return "\n".join(lines)

async def perform_search(query: str, max_results: int = 5) -> str:
    """Виконує пошук і повертає результати з посиланнями"""
    try:
        loop = asyncio.get_running_loop()
        
        def _search():
            with DDGS() as ddgs:
                # Отримуємо результати текстом
                return list(ddgs.text(query, region="ua-uk", max_results=max_results))

        results = await loop.run_in_executor(None, _search)

        if not results:
            return "Search returned no results."

        formatted = f"🔎 WEB SEARCH RESULTS FOR: '{query}'\n\n"
        for i, res in enumerate(results, 1):
            title = res.get('title', 'No title')
            snippet = res.get('body', 'No content')
            link = res.get('href', '#')
            formatted += f"[{i}] {title}\nLINK: {link}\nDETAILS: {snippet}\n\n"
            
        return formatted

    except Exception as e:
        logger.error(f"Search error: {e}")
        return f"Search failed: {str(e)}"