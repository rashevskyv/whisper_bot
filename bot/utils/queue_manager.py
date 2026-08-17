import logging
from sqlalchemy.future import select
from sqlalchemy import func, delete
from bot.database.session import AsyncSessionLocal
from bot.database.models import DownloadQueue

logger = logging.getLogger(__name__)

async def get_queue_stats() -> dict[str, int]:
    """
    Повертає статистику черги завантажень:
    {
        'pending': int,
        'processing': int,
        'done': int,
        'error': int,
        'total': int
    }
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DownloadQueue.status, func.count(DownloadQueue.id)).group_by(DownloadQueue.status)
        )
        counts = dict(result.all())
        pending = counts.get("pending", 0)
        processing = counts.get("processing", 0)
        done = counts.get("done", 0)
        error = counts.get("error", 0) + counts.get("timeout", 0) + counts.get("failed_by_donor", 0)
        total = sum(counts.values())
        return {
            "pending": pending,
            "processing": processing,
            "done": done,
            "error": error,
            "total": total
        }

async def clear_pending_tasks() -> int:
    """
    Видаляє з черги всі завдання у статусах 'pending' та 'processing'.
    Повертає кількість видалених завдань.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(DownloadQueue).where(DownloadQueue.status.in_(["pending", "processing"]))
        )
        await session.commit()
        deleted_count = result.rowcount or 0
        logger.info(f"🗑 Очищено {deleted_count} очікуючих завдань з черги.")
        return deleted_count

async def clear_all_tasks() -> int:
    """
    Повністю очищає таблицю черги завантажень download_queue.
    Повертає кількість видалених завдань.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(delete(DownloadQueue))
        await session.commit()
        deleted_count = result.rowcount or 0
        logger.info(f"💥 Повністю очищено таблицю черги ({deleted_count} записів).")
        return deleted_count
