from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from config import DB_PATH
from .models import Base
import logging

logger = logging.getLogger(__name__)

# URL для підключення
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)

# Фабрика сесій
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    """Ініціалізація БД та увімкнення WAL режиму для швидкодії"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    # Вмикаємо Write-Ahead Logging (WAL) для роботи в кілька процесів
    async with engine.connect() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA synchronous=NORMAL;"))
        await conn.commit()
        
    logger.info("✅ База даних ініціалізована (WAL mode enabled).")

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()