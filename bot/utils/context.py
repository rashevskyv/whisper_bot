import logging
from datetime import datetime, timedelta
from sqlalchemy.future import select
from sqlalchemy import desc, and_
from bot.database.session import AsyncSessionLocal
from bot.database.models import MessageCache, User
from config import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

class ContextManager:
    async def save_message(self, user_id: int, chat_id: int, role: str, content: str, media_id: str = None):
        """Зберігає повідомлення в історію конкретного чату"""
        async with AsyncSessionLocal() as session:
            try:
                msg = MessageCache(
                    user_id=user_id,
                    chat_id=chat_id, # Обов'язкова прив'язка до чату
                    role=role,
                    content=content,
                    media_file_id=media_id
                )
                session.add(msg)
                await session.commit()
            except Exception as e:
                logger.error(f"Failed to save message context: {e}")

    async def get_context(self, user_id: int, chat_id: int, limit: int = 20, time_window_hours: int = 24):
        """
        Отримує контекст ТІЛЬКИ для поточного чату (chat_id).
        Запобігає витоку приватної переписки в групи.
        """
        messages = []
        async with AsyncSessionLocal() as session:
            # 1. System Prompt
            user_result = await session.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            sys_prompt = user.system_prompt if user and user.system_prompt else DEFAULT_SETTINGS['system_prompt']
            messages.append({"role": "system", "content": sys_prompt})

            # 2. Історія (фільтр за user_id ТА chat_id)
            since_time = datetime.utcnow() - timedelta(hours=time_window_hours)
            stmt = (
                select(MessageCache)
                .where(
                    and_(
                        MessageCache.user_id == user_id,
                        MessageCache.chat_id == chat_id, # Глобальна ізоляція контексту
                        MessageCache.timestamp >= since_time
                    )
                )
                .order_by(desc(MessageCache.timestamp))
                .limit(limit)
            )
            
            result = await session.execute(stmt)
            history_objs = result.scalars().all()
            for msg in reversed(history_objs):
                messages.append({"role": msg.role, "content": msg.content})

        return messages

    async def get_last_transcription(self, user_id: int, chat_id: int) -> str:
        """Останній текст транскрипції в конкретному чаті"""
        async with AsyncSessionLocal() as session:
            stmt = (
                select(MessageCache)
                .where(
                    and_(
                        MessageCache.user_id == user_id,
                        MessageCache.chat_id == chat_id,
                        MessageCache.role == 'user',
                        MessageCache.content.like("[Транскрипція]:%")
                    )
                )
                .order_by(desc(MessageCache.timestamp))
                .limit(1)
            )
            result = await session.execute(stmt)
            msg = result.scalar_one_or_none()
            return msg.content.replace("[Транскрипція]: ", "", 1) if msg else None

context_manager = ContextManager()