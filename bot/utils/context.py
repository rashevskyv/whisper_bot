import logging
from datetime import datetime, timedelta
from sqlalchemy.future import select
from sqlalchemy import desc
from bot.database.session import AsyncSessionLocal
from bot.database.models import MessageCache, User
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

class ContextManager:
    async def save_message(self, user_id: int, role: str, content: str, media_id: str = None):
        """Зберігає повідомлення в історію"""
        async with AsyncSessionLocal() as session:
            try:
                msg = MessageCache(
                    user_id=user_id,
                    role=role,
                    content=content,
                    media_file_id=media_id
                )
                session.add(msg)
                await session.commit()
            except Exception as e:
                logger.error(f"Failed to save message context: {e}")

    async def get_context(self, user_id: int, limit: int = 20, time_window_hours: int = 24):
        """
        Отримує контекст для формування запиту до AI.
        Повертає список словників: [{'role': 'user', ...}, ...]
        """
        messages = []
        
        async with AsyncSessionLocal() as session:
            # 1. Отримуємо налаштування користувача (System Prompt)
            user_result = await session.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            
            sys_prompt = user.system_prompt if user and user.system_prompt else DEFAULT_SETTINGS['system_prompt']
            messages.append({"role": "system", "content": sys_prompt})

            # 2. Отримуємо історію повідомлень
            # Логіка: беремо останні N повідомлень за останні X годин
            # Важливо брати їх у зворотному порядку (від найновіших), а потім розвернути
            since_time = datetime.utcnow() - timedelta(hours=time_window_hours)
            
            stmt = (
                select(MessageCache)
                .where(
                    MessageCache.user_id == user_id,
                    MessageCache.timestamp >= since_time
                )
                .order_by(desc(MessageCache.timestamp))
                .limit(limit)
            )
            
            result = await session.execute(stmt)
            history_objs = result.scalars().all()

            # Розвертаємо список, щоб найстаріші були зверху (для GPT це важливо)
            for msg in reversed(history_objs):
                messages.append({"role": msg.role, "content": msg.content})

        return messages

    async def clear_context(self, user_id: int):
        """Очищає історію повідомлень користувача (команда /reset)"""
        async with AsyncSessionLocal() as session:
            # Тут ми можемо або видаляти фізично, або ставити флаг is_archived
            # Для простоти поки що видаляємо старі записи кешу, залишаючи юзера
            # Але реалізацію зробимо пізніше, якщо треба
            pass

context_manager = ContextManager()