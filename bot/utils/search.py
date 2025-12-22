import logging
import asyncio
from duckduckgo_search import DDGS

# У нових версіях клас називається так само, але імпорт може йти через ddgs
# Про всяк випадок робимо fallback, щоб працювало і так, і так
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

async def perform_search(query: str, max_results: int = 5) -> str:
    """Виконує пошук і повертає результати з посиланнями"""
    try:
        loop = asyncio.get_running_loop()
        
        def _search():
            # Використовуємо context manager для стабільності
            with DDGS() as ddgs:
                # keywords замість query у деяких версіях, але text(query) стандарт
                return list(ddgs.text(query, region="ua-uk", max_results=max_results))

        results = await loop.run_in_executor(None, _search)

        if not results:
            return "Search returned no results. Try rephrasing."

        formatted = f"🔎 WEB SEARCH RESULTS FOR: '{query}'\n\n"
        for i, res in enumerate(results, 1):
            title = res.get('title', 'No title')
            snippet = res.get('body', 'No content')
            link = res.get('href', '#')
            # Форматуємо так, щоб GPT точно бачив посилання
            formatted += f"[{i}] {title}\nLINK: {link}\nDETAILS: {snippet}\n\n"
            
        return formatted

    except Exception as e:
        logger.error(f"Search error: {e}")
        return f"Search failed: {str(e)}"