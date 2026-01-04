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
        async with AsyncSessionLocal() as session:
            try:
                msg = MessageCache(user_id=user_id, chat_id=chat_id, role=role, content=content, media_file_id=media_id)
                session.add(msg)
                await session.commit()
            except Exception as e: logger.error(f"Save context error: {e}")

    async def get_context(self, user_id: int, chat_id: int, limit: int = 20, time_window_hours: int = 24):
        messages = []
        async with AsyncSessionLocal() as session:
            # System Prompt
            u_res = await session.execute(select(User).where(User.id == user_id))
            u = u_res.scalar_one_or_none()
            sys = u.system_prompt if u and u.system_prompt else DEFAULT_SETTINGS['system_prompt']
            messages.append({"role": "system", "content": sys})

            # History: ONLY user and assistant. Ignore 'transcription'.
            since = datetime.utcnow() - timedelta(hours=time_window_hours)
            stmt = select(MessageCache).where(
                and_(
                    MessageCache.user_id == user_id,
                    MessageCache.chat_id == chat_id,
                    MessageCache.timestamp >= since,
                    MessageCache.role.in_(['user', 'assistant']) # <--- ОСЬ ЦЕ ГОЛОВНЕ
                )
            ).order_by(desc(MessageCache.timestamp)).limit(limit)
            
            res = await session.execute(stmt)
            for m in reversed(res.scalars().all()):
                messages.append({"role": m.role, "content": m.content})
        return messages

    async def get_last_transcription(self, user_id: int, chat_id: int) -> str:
        async with AsyncSessionLocal() as session:
            # Find specific 'transcription' role
            stmt = select(MessageCache).where(
                and_(
                    MessageCache.user_id == user_id,
                    MessageCache.chat_id == chat_id,
                    MessageCache.role == 'transcription'
                )
            ).order_by(desc(MessageCache.timestamp)).limit(1)
            
            res = await session.execute(stmt)
            msg = res.scalar_one_or_none()
            return msg.content if msg else None

context_manager = ContextManager()