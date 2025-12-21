import logging
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.security import key_manager
from bot.ai import OpenAIProvider
from config import DEFAULT_SETTINGS, TOKEN
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Системний ключ (резервний)
SYSTEM_OPENAI_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("TEST_OPENAI_KEY")

async def get_or_create_user(telegram_user) -> User:
    """Отримує користувача з БД або створює нового"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id == telegram_user.id))
        user = result.scalar_one_or_none()

        if not user:
            logger.info(f"Новий користувач: {telegram_user.id}")
            user = User(
                id=telegram_user.id,
                username=telegram_user.username,
                full_name=telegram_user.full_name,
                settings=DEFAULT_SETTINGS,
                system_prompt=DEFAULT_SETTINGS['system_prompt']
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        
        return user

async def get_ai_provider(user_id: int):
    """
    Визначає, який API ключ використовувати:
    1. Особистий ключ користувача (якщо є).
    2. Системний ключ (якщо немає особистого).
    Повертає екземпляр провайдера (OpenAIProvider) або None.
    """
    async with AsyncSessionLocal() as session:
        # Шукаємо ключ користувача для OpenAI
        result = await session.execute(
            select(APIKey).where(
                APIKey.user_id == user_id,
                APIKey.provider == 'openai',
                APIKey.is_active == True
            )
        )
        user_key_obj = result.scalar_one_or_none()

        api_key = None
        
        if user_key_obj:
            # Розшифровуємо ключ користувача
            api_key = key_manager.decrypt(user_key_obj.encrypted_key)
            logger.info(f"Використання особистого ключа для {user_id}")
        elif SYSTEM_OPENAI_KEY:
            # Фоллбек на системний ключ
            api_key = SYSTEM_OPENAI_KEY
            logger.info(f"Використання системного ключа для {user_id}")
        
        if api_key:
            # Тут в майбутньому можна додати switch/case для інших провайдерів (Claude, etc)
            return OpenAIProvider(api_key=api_key)
        
        return None