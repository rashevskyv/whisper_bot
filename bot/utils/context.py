import logging
from datetime import datetime, timedelta
from sqlalchemy.future import select
from sqlalchemy import desc, and_
from bot.database.session import AsyncSessionLocal
from bot.database.models import MessageCache, User
from config import DEFAULT_SETTINGS, DEFAULT_GROUP_SETTINGS

logger = logging.getLogger(__name__)

class ContextManager:
    async def save_message(self, user_id: int, chat_id: int, role: str, content: str, media_id: str = None):
        """Зберігає повідомлення в історію конкретного чату"""
        async with AsyncSessionLocal() as session:
            try:
                msg = MessageCache(
                    user_id=user_id,
                    chat_id=chat_id,
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
        Отримує контекст для діалогу.
        ВИПРАВЛЕНО: Системний промпт береться з налаштувань ЧАТУ (групи), а не юзера.
        """
        messages = []
        async with AsyncSessionLocal() as session:
            # 1. System Prompt (Беремо по chat_id!)
            chat_settings_obj = await session.get(User, chat_id)

            if chat_settings_obj:
                sys_prompt = chat_settings_obj.system_prompt
            else:
                # Якщо налаштувань для чату ще немає, беремо дефолт
                sys_prompt = DEFAULT_GROUP_SETTINGS['system_prompt'] if chat_id < 0 else DEFAULT_SETTINGS['system_prompt']

            messages.append({"role": "system", "content": sys_prompt})

            # 2. Історія (тільки активні ролі: user/assistant)
            since_time = datetime.utcnow() - timedelta(hours=time_window_hours)
            stmt = (
                select(MessageCache)
                .where(
                    and_(
                        # Ми не фільтруємо по user_id тут, бо в групі бот має знати контекст всієї розмови,
                        # а не тільки одного юзера (хіба що ви хочете повної ізоляції навіть всередині групи).
                        # Але зазвичай в групах контекст спільний.
                        # Якщо треба ізоляція по юзеру - розкоментуйте рядок нижче:
                        # MessageCache.user_id == user_id,

                        MessageCache.chat_id == chat_id,
                        MessageCache.timestamp >= since_time,
                        MessageCache.role.in_(['user', 'assistant'])
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
        """
        Шукає останню транскрипцію (ізольовану).
        """
        async with AsyncSessionLocal() as session:
            stmt = (
                select(MessageCache)
                .where(
                    and_(
                        MessageCache.user_id == user_id,
                        MessageCache.chat_id == chat_id,
                        MessageCache.role == 'transcription'
                    )
                )
                .order_by(desc(MessageCache.timestamp))
                .limit(1)
            )
            result = await session.execute(stmt)
            msg = result.scalar_one_or_none()
            return msg.content.replace("[Транскрипція]: ", "", 1) if msg else None

context_manager = ContextManager()