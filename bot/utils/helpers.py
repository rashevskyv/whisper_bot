import logging
import os
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.security import key_manager
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.google_provider import GoogleProvider # Новий імпорт
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

SYSTEM_OPENAI_KEY = os.getenv("OPENAI_API_KEY")
SYSTEM_GOOGLE_KEY = os.getenv("GOOGLE_API_KEY")

async def get_or_create_user(telegram_user) -> User:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id == telegram_user.id))
        user = result.scalar_one_or_none()
        if not user:
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

async def get_ai_provider(user_id: int, force_whisper: bool = False):
    """
    Повертає провайдер AI залежно від налаштувань користувача.
    :param force_whisper: Якщо True, завжди повертає OpenAI (для транскрибації)
    """
    async with AsyncSessionLocal() as session:
        # 1. Дізнаємось яку модель хоче юзер
        user = await session.get(User, user_id)
        model = user.settings.get('model', 'gpt-4o-mini') if user and user.settings else 'gpt-4o-mini'
        
        # Якщо треба транскрибувати аудіо - завжди беремо OpenAI (Whisper)
        if force_whisper:
            provider_type = 'openai'
        elif 'gemini' in model.lower():
            provider_type = 'google'
        else:
            provider_type = 'openai'

        # 2. Шукаємо ключ
        result = await session.execute(
            select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == provider_type, APIKey.is_active == True)
        )
        user_key_obj = result.scalar_one_or_none()
        
        api_key = None
        if user_key_obj:
            api_key = key_manager.decrypt(user_key_obj.encrypted_key)
        else:
            # Системні ключі
            if provider_type == 'google':
                api_key = SYSTEM_GOOGLE_KEY
            else:
                api_key = SYSTEM_OPENAI_KEY
        
        if not api_key:
            return None

        # 3. Повертаємо інстанс
        if provider_type == 'google':
            return GoogleProvider(api_key=api_key)
        else:
            return OpenAIProvider(api_key=api_key)