import logging
import os
import re
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.security import key_manager
from bot.ai.openai_provider import OpenAIProvider
from bot.ai.google_provider import GoogleProvider
from config import DEFAULT_SETTINGS
from telegram.constants import ParseMode

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

async def get_ai_provider(user_id: int, for_transcription: bool = False):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if for_transcription:
            model = user.settings.get('transcription_model', DEFAULT_SETTINGS['transcription_model']) if user and user.settings else 'whisper-1'
        else:
            model = user.settings.get('model', 'gpt-4o-mini') if user and user.settings else 'gpt-4o-mini'
        
        provider_type = 'google' if 'gemini' in model.lower() else 'openai'
        result = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == provider_type, APIKey.is_active == True))
        user_key_obj = result.scalar_one_or_none()
        
        api_key = key_manager.decrypt(user_key_obj.encrypted_key) if user_key_obj else (SYSTEM_GOOGLE_KEY if provider_type == 'google' else SYSTEM_OPENAI_KEY)
        if not api_key: return None

        return GoogleProvider(api_key=api_key, model_name=model) if provider_type == 'google' else OpenAIProvider(api_key=api_key)

def clean_html(text: str) -> str:
    """Очищає текст від заборонених тегів та мінімізує порожні рядки."""
    if not text: return ""
    
    # Конвертація базового Markdown, якщо AI його помилково використав
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'#{1,6}\s?(.*)', r'<b>\1</b>', text)
    
    # Видалення HTML структурних тегів
    text = re.sub(r'<(html|head|body|meta|doctype|style|script|link).*?>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'</(html|head|body|meta|style|script|link)>', '', text, flags=re.IGNORECASE)
    
    # Заміна блочних тегів на одинарний перенос рядка (зменшуємо 'повітря')
    text = text.replace("<ul>", "").replace("</ul>", "")
    text = text.replace("<ol>", "").replace("</ol>", "")
    text = text.replace("<li>", "• ").replace("</li>", "\n")
    text = text.replace("<div>", "").replace("</div>", "\n")
    text = text.replace("<p>", "").replace("</p>", "\n")
    text = text.replace("<br>", "\n").replace("<br/>", "\n")
    
    # Очищення заголовків
    text = re.sub(r'<h[1-6]>(.*?)</h[1-6]>', r'<b>\1</b>\n', text)
    
    # Видалення залишків Markdown коду
    text = text.replace("```html", "").replace("```", "")
    
    # ФІНАЛЬНИЙ ЕТАП: Видалення зайвих порожніх рядків (більше 2 підряд)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

async def send_long_message(target, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    text = clean_html(text)
    if hasattr(target, 'reply_text'): send_func = target.reply_text
    else: send_func = target.send_message
    
    LIMIT = 4000
    if len(text) <= LIMIT:
        try: await send_func(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except: await send_func(text, reply_markup=reply_markup, parse_mode=None)
        return

    parts = []
    inner_text = text
    while inner_text:
        if len(inner_text) <= LIMIT: parts.append(inner_text); break
        pos = inner_text.rfind('\n', 0, LIMIT)
        if pos == -1: pos = LIMIT
        parts.append(inner_text[:pos])
        inner_text = inner_text[pos:].strip()

    for part in parts:
        await send_func(part, reply_markup=reply_markup if part == parts[-1] else None, parse_mode=parse_mode)

async def beautify_text(user_id: int, text: str) -> str:
    provider = await get_ai_provider(user_id)
    if not provider: return text
    messages = [{"role": "system", "content": DEFAULT_SETTINGS['beautify_prompt']}, {"role": "user", "content": text}]
    result = ""
    try:
        async for chunk in provider.generate_stream(messages, {'model': 'gpt-4o-mini', 'temperature': 0.3, 'allow_search': False}):
            result += chunk
        return result.strip() if result else text
    except: return text