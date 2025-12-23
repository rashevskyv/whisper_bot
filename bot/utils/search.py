import logging
import asyncio
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

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