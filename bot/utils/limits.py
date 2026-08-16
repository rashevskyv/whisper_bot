import logging
from datetime import datetime, timezone
from typing import Tuple
from sqlalchemy.future import select
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from bot.database.session import AsyncSessionLocal
from bot.database.models import DailyTranscriptionUsage
from config import DAILY_TRANSCRIPTION_LIMIT_SECONDS

logger = logging.getLogger(__name__)

# ponytail: Concurrent uploads can slightly overshoot; atomic reservation is the upgrade path if abuse becomes a real issue.

async def get_daily_transcription_used_seconds(user_id: int) -> int:
    """
    Повертає кількість секунд медіа, успішно транскрибованих користувачем за поточний календарний день (UTC).
    """
    today_utc = datetime.now(timezone.utc).date()
    async with AsyncSessionLocal() as session:
        stmt = select(DailyTranscriptionUsage).where(
            and_(
                DailyTranscriptionUsage.user_id == user_id,
                DailyTranscriptionUsage.usage_date == today_utc
            )
        )
        res = await session.execute(stmt)
        usage = res.scalar_one_or_none()
        return usage.seconds_used if usage else 0

async def check_transcription_limit(user_id: int, duration_seconds: int = 0) -> Tuple[bool, str]:
    """
    Перевіряє, чи не вичерпано денний ліміт транскрибації для користувача (60 хв/добу UTC).
    Повертає (True, "") якщо ліміт дозволяє транскрибацію, або (False, error_msg) якщо ліміт вичерпано.
    """
    used = await get_daily_transcription_used_seconds(user_id)
    if used >= DAILY_TRANSCRIPTION_LIMIT_SECONDS:
        return False, "⚠️ Ліміт транскрибації на сьогодні вичерпано (60 хв)."

    if duration_seconds > 0 and (used + duration_seconds > DAILY_TRANSCRIPTION_LIMIT_SECONDS):
        remaining_seconds = max(0, DAILY_TRANSCRIPTION_LIMIT_SECONDS - used)
        remaining_mins = max(1, remaining_seconds // 60)
        return False, f"⚠️ Перевищено денний ліміт транскрибації (60 хв). Залишилось: ~{remaining_mins} хв."

    return True, ""

async def record_transcription_usage(user_id: int, duration_seconds: int):
    """
    Фіксує використані секунди транскрибації в базі даних після успішного розпізнавання.
    """
    if duration_seconds <= 0:
        return

    today_utc = datetime.now(timezone.utc).date()
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(DailyTranscriptionUsage).where(
                and_(
                    DailyTranscriptionUsage.user_id == user_id,
                    DailyTranscriptionUsage.usage_date == today_utc
                )
            )
            res = await session.execute(stmt)
            usage = res.scalar_one_or_none()
            if usage:
                usage.seconds_used += duration_seconds
            else:
                usage = DailyTranscriptionUsage(
                    user_id=user_id,
                    usage_date=today_utc,
                    seconds_used=duration_seconds
                )
                session.add(usage)
            await session.commit()
        except IntegrityError:
            # Обробка конфлікту паралельних вставок для одного користувача та дати UTC
            try:
                await session.rollback()
                stmt = select(DailyTranscriptionUsage).where(
                    and_(
                        DailyTranscriptionUsage.user_id == user_id,
                        DailyTranscriptionUsage.usage_date == today_utc
                    )
                )
                res = await session.execute(stmt)
                usage = res.scalar_one_or_none()
                if usage:
                    usage.seconds_used += duration_seconds
                    await session.commit()
                else:
                    logger.error(f"Failed to recover concurrent transcription usage for user {user_id}: row not found after rollback.")
            except Exception as e:
                logger.error(f"Failed to recover concurrent transcription usage for user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to record transcription usage for user {user_id}: {e}")
