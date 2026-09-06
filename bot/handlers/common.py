import logging
import re
import zoneinfo
from typing import Any, Optional
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from config import DEFAULT_SETTINGS, DEFAULT_GROUP_SETTINGS, BOT_TRIGGERS, ADMIN_IDS, BOT_TIMEZONE

logger = logging.getLogger(__name__)

MEDIA_GROUP_CACHE = {}

async def get_user_model_settings(user_id: int):
    """
    Отримує налаштування. user_id може бути ID користувача АБО ID групи.
    """
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)

        # Визначаємо дефолтні налаштування залежно від типу ID (група < 0)
        default = DEFAULT_GROUP_SETTINGS.copy() if user_id < 0 else DEFAULT_SETTINGS.copy()

        settings = user.settings if (user and user.settings) else default

        # Перевірка прав на пошук (для груп складніше, поки беремо з налаштувань)
        # Якщо це приват - перевіряємо адміна/ключі.
        # Якщо група - використовуємо налаштування групи (де може бути системний ключ)
        if user_id > 0:
            is_admin = user_id in ADMIN_IDS
            result = await session.execute(
                select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == 'openai', APIKey.is_active == True)
            )
            has_own_key = result.scalar_one_or_none() is not None
            settings['allow_search'] = is_admin or has_own_key or settings.get('allow_search', False)

        # Fallbacks
        if 'language' not in settings: settings['language'] = default['language']
        if 'trigger_mode' not in settings: settings['trigger_mode'] = default.get('trigger_mode', 'keywords')
        if 'video_repost' not in settings: settings['video_repost'] = default.get('video_repost', True)
        if 'timezone' not in settings: settings['timezone'] = default.get('timezone', BOT_TIMEZONE)
        if user_id > 0:
            if 'timezone_selected' not in settings:
                settings['timezone_selected'] = False
            else:
                settings['timezone_selected'] = bool(settings['timezone_selected'])

        return settings


def is_timezone_selected(settings: Optional[dict]) -> bool:
    """Перевіряє, чи було явно обрано часовий пояс користувачем у приватних налаштуваннях."""
    if not isinstance(settings, dict):
        return False
    return bool(settings.get("timezone_selected", False))


def _is_valid_iana_tz(tz_name: Any) -> bool:
    if not isinstance(tz_name, str) or not tz_name.strip():
        return False
    try:
        zoneinfo.ZoneInfo(tz_name.strip())
        return True
    except Exception:
        return False


async def get_effective_timezone(user_id: Optional[int], chat_id: Optional[int]) -> str:
    """
    Повертає дійсний IANA часовий пояс для контексту виконання дії:
    - Якщо chat_id < 0 (група), читає часовий пояс із налаштувань групи (chat_id).
    - Інакше читає часовий пояс із налаштувань користувача (user_id).
    - Повертає тільки валідну IANA timezone; при відсутності або невалідності — BOT_TIMEZONE (або UTC).
    - Не записує нічого в БД, не перезаписує явний UTC і не логує налаштування.
    """
    target_id = chat_id if (chat_id is not None and chat_id < 0) else (user_id if user_id is not None else chat_id)
    if target_id is None:
        return BOT_TIMEZONE.strip() if _is_valid_iana_tz(BOT_TIMEZONE) else "UTC"

    try:
        settings = await get_user_model_settings(target_id)
    except Exception:
        settings = None

    raw_tz = settings.get("timezone") if isinstance(settings, dict) else None
    if _is_valid_iana_tz(raw_tz):
        return str(raw_tz).strip()

    if _is_valid_iana_tz(BOT_TIMEZONE):
        return BOT_TIMEZONE.strip()
    return "UTC"


async def update_user_language(user_id: int, lang_code: str):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            settings = dict(user.settings)
            settings['language'] = lang_code
            user.settings = settings
            await session.commit()

def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Визначає, чи відповідати на повідомлення в групах."""
    chat = update.effective_chat
    message = update.message

    if not message: return False
    if chat.type == 'private': return True # В приваті відповідаємо завжди

    # --- ЛОГІКА ДЛЯ ГРУП ---

    # 1. Реплай на бота завжди тригерить (якщо це не авто-відповідь)
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        return True

    text = (message.text or message.caption or "").lower().strip()
    if not text: return False

    bot_username = context.bot.username.lower()

    # TODO: Тут можна отримати налаштування групи з БД, але це асинхронна операція,
    # а should_respond часто викликається синхронно або потребує кешу.
    # Для спрощення поки використовуємо стандартні тригери.
    # В ідеалі: settings = await get_user_model_settings(chat.id)
    # Але оскільки цей метод синхронний у багатьох хендлерах, ми покладаємось на базові правила.

    # Перевірка згадки (@botname)
    if f"@{bot_username}" in text:
        return True

    # Перевірка ключових слів (якщо не вимкнено в майбутньому)
    triggers = BOT_TRIGGERS + [bot_username]
    pattern = r'^(' + '|'.join(map(re.escape, triggers)) + r')\b'

    if re.search(pattern, text):
        return True

    return False