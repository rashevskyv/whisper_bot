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
    """
    Повертає провайдер AI.
    :param for_transcription: Якщо True, вибирає провайдера на основі налаштування 'transcription_model'
    """
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        
        # Визначаємо яку модель використовувати
        if for_transcription:
            # Якщо юзер ще не має налаштування, беремо дефолт
            model = user.settings.get('transcription_model', DEFAULT_SETTINGS['transcription_model']) if user and user.settings else 'whisper-1'
        else:
            model = user.settings.get('model', 'gpt-4o-mini') if user and user.settings else 'gpt-4o-mini'
        
        # Визначаємо провайдера за назвою моделі
        if 'gemini' in model.lower():
            provider_type = 'google'
        else:
            provider_type = 'openai' # whisper, gpt

        # Шукаємо ключ
        result = await session.execute(
            select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == provider_type, APIKey.is_active == True)
        )
        user_key_obj = result.scalar_one_or_none()
        
        api_key = None
        if user_key_obj:
            api_key = key_manager.decrypt(user_key_obj.encrypted_key)
        else:
            if provider_type == 'google':
                api_key = SYSTEM_GOOGLE_KEY
            else:
                api_key = SYSTEM_OPENAI_KEY
        
        if not api_key:
            return None

        # Створюємо інстанс, передаючи модель (це важливо для Gemini)
        if provider_type == 'google':
            return GoogleProvider(api_key=api_key, model_name=model)
        else:
            return OpenAIProvider(api_key=api_key)

def clean_html(text: str) -> str:
    """Примусово замінює заборонені теги та конвертує Markdown."""
    if not text: return ""
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<i>\1</i>', text)
    text = re.sub(r'#{1,6}\s?(.*)', r'<b>\1</b>', text)
    text = re.sub(r'<!DOCTYPE.*?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<html.*?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</html>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<head>.*?</head>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<body.*?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</body>', '', text, flags=re.IGNORECASE)
    text = text.replace("<ul>", "").replace("</ul>", "")
    text = text.replace("<ol>", "").replace("</ol>", "")
    text = text.replace("<li>", "• ").replace("</li>", "\n")
    text = text.replace("<div>", "").replace("</div>", "\n")
    text = text.replace("<p>", "").replace("</p>", "\n\n")
    text = text.replace("<br>", "\n").replace("<br/>", "\n")
    text = re.sub(r'<h[1-6]>(.*?)</h[1-6]>', r'<b>\1</b>\n', text)
    text = text.replace("```html", "").replace("```", "")
    return text.strip()

async def send_long_message(target, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Розбиває довге повідомлення."""
    text = clean_html(text)
    if hasattr(target, 'reply_text'): send_func = target.reply_text
    else: send_func = target.send_message
    
    LIMIT = 4000
    is_code_wrapped = False
    inner_text = text
    if text.startswith("<code>") and text.endswith("</code>"):
        is_code_wrapped = True
        inner_text = text[6:-7]
    elif text.startswith("<pre>") and text.endswith("</pre>"):
        is_code_wrapped = True
        inner_text = re.sub(r'^<pre><code>|</code></pre>$|^<pre>|</pre>$', '', text)

    if len(text) <= LIMIT:
        try: await send_func(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"HTML Error: {e}")
            await send_func(text, reply_markup=reply_markup, parse_mode=None)
        return

    parts = []
    while inner_text:
        if len(inner_text) <= LIMIT: parts.append(inner_text); break
        split_at = -1
        for separator in ['\n\n', '\n', '. ', ' ']:
            pos = inner_text.rfind(separator, 0, LIMIT)
            if pos != -1: split_at = pos; break
        if split_at == -1: split_at = LIMIT
        chunk = inner_text[:split_at+1] if split_at < LIMIT else inner_text[:split_at]
        parts.append(chunk)
        inner_text = inner_text[len(chunk):]

    for i, part in enumerate(parts):
        part_to_send = f"<code>{part.strip()}</code>" if is_code_wrapped else part
        keyboard = reply_markup if i == len(parts) - 1 else None
        try: await send_func(part_to_send, reply_markup=keyboard, parse_mode=parse_mode)
        except: await send_func(part.replace('<','').replace('>',''), reply_markup=keyboard, parse_mode=None)