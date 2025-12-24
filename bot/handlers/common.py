import logging
import re
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from config import DEFAULT_SETTINGS, BOT_TRIGGERS, ADMIN_IDS

logger = logging.getLogger(__name__)

# Кеш для медіа-груп (альбомів)
MEDIA_GROUP_CACHE = {}

def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Визначає, чи відповідати на повідомлення в групах."""
    chat_type = update.effective_chat.type
    
    if chat_type == 'private':
        return True
    
    message = update.message
    if not message:
        return False
        
    # Реплай на бота
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        return True
        
    text = (message.text or message.caption or "").lower().strip()
    if not text:
        return False

    bot_username = context.bot.username.lower()
    triggers = BOT_TRIGGERS + [f"@{bot_username}", bot_username]
    
    # Пошук тригера на початку
    pattern = r'^(' + '|'.join(map(re.escape, triggers)) + r')\b'
    
    if re.search(pattern, text):
        return True
        
    return False

async def get_user_model_settings(user_id: int):
    """Отримує налаштування користувача з БД"""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        settings = user.settings if (user and user.settings) else DEFAULT_SETTINGS.copy()
        
        is_admin = user_id in ADMIN_IDS
        result = await session.execute(
            select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == 'openai', APIKey.is_active == True)
        )
        has_own_key = result.scalar_one_or_none() is not None
        
        settings['allow_search'] = is_admin or has_own_key
        
        if 'language' not in settings:
            settings['language'] = DEFAULT_SETTINGS['language']
            
        return settings

async def update_user_language(user_id: int, lang_code: str):
    """Оновлює мову користувача в БД"""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            settings = dict(user.settings)
            settings['language'] = lang_code
            user.settings = settings
            await session.commit()
    logger.info(f"User {user_id} language updated to {lang_code}")