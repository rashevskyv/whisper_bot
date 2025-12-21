from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from config import DB_PATH
from .models import Base
import logging

logger = logging.getLogger(__name__)

# URL для підключення (sqlite+aiosqlite для асинхронності)
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)

# Фабрика сесій
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    """Ініціалізація БД (створення таблиць)"""
    async with engine.begin() as conn:
        # await conn.run_sync(Base.metadata.drop_all) # Розкоментувати для повного скидання
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База даних ініціалізована.")

async def get_db():
    """Генератор сесій для використання в хендлерах"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()