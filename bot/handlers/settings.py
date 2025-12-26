import logging
import os
import zoneinfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.security import key_manager
from config import PERSONAS, DEFAULT_SETTINGS, ADMIN_IDS, AVAILABLE_MODELS, TRANSCRIPTION_MODELS

logger = logging.getLogger(__name__)

WAITING_FOR_KEY = 1
WAITING_FOR_CUSTOM_MODEL = 2
WAITING_FOR_CUSTOM_PROMPT = 3
WAITING_FOR_TIMEZONE = 4

async def get_or_create_user_internal(session, user_id):
    user = await session.get(User, user_id)
    if not user:
        user = User(
            id=user_id, 
            settings=DEFAULT_SETTINGS, 
            system_prompt=DEFAULT_SETTINGS['system_prompt']
        )
        session.add(user)
        await session.flush()
    return user

def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🧠 Чат Модель", callback_data="model_menu"), 
            InlineKeyboardButton("🎙 Транскрибація", callback_data="transcription_menu")
        ],
        [
            InlineKeyboardButton("🌐 Мова", callback_data="lang_menu"), 
            InlineKeyboardButton("🎭 Персона", callback_data="persona_menu")
        ],
        [
            InlineKeyboardButton("🌍 Часовий пояс", callback_data="timezone_menu"),
            InlineKeyboardButton("🔑 Ключі API", callback_data="keys_menu")
        ],
        [InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text(
            "⚙️ <b>Головні налаштування:</b>", 
            reply_markup=get_main_menu_keyboard(), 
            parse_mode='HTML'
        )
    except BadRequest:
        pass

async def close_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except:
        pass

# ... (transcription_menu, set_transcription_model, language_menu, set_language_gui БЕЗ ЗМІН) ...
# Для економії місця не дублюю ці функції, вони лишаються як в попередньому варіанті, але якщо треба - напиши.
# Я покажу лише ті де додав ЛОГИ.

async def transcription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Без змін, крім імпортів)
    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        current_model = user.settings.get('transcription_model', 'whisper-1')
        keys_res = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.is_active == True))
        user_keys = keys_res.scalars().all()
        has_openai_key = any(k.provider == 'openai' for k in user_keys)
        has_google_key = any(k.provider == 'google' for k in user_keys)
        is_admin = user_id in ADMIN_IDS

    can_access_settings = is_admin or has_openai_key or has_google_key
    
    if not can_access_settings:
        text = ("🔒 <b>Доступ обмежено</b>\n\nЗміна моделі транскрибації доступна лише користувачам із власними API ключами.\n"f"Наразі використовується стандартна модель: <code>{current_model}</code>")
        keyboard = [[InlineKeyboardButton("🔑 Додати ключ", callback_data="keys_menu")],[InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")]]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        except BadRequest: pass
        return

    text = f"🎙 <b>Модель транскрибації:</b> <code>{current_model}</code>\n"
    if 'whisper' in current_model: text += "ℹ️ Whisper - спеціалізована модель для аудіо."
    elif 'transcribe' in current_model: text += "ℹ️ GPT Audio - мультимодальна транскрибація."
    else: text += "ℹ️ Gemini - мультимодальна (розуміє контекст)."

    keyboard = []
    if is_admin or has_openai_key:
        for m in TRANSCRIPTION_MODELS['openai']: keyboard.append([InlineKeyboardButton(f"✅ {m}" if current_model == m else m, callback_data=f"set_trans_{m}")])
    if is_admin or has_google_key:
        for m in TRANSCRIPTION_MODELS['google']: keyboard.append([InlineKeyboardButton(f"✅ {m}" if current_model == m else m, callback_data=f"set_trans_{m}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_transcription_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_model = query.data.replace("set_trans_", "")
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        settings = dict(user.settings)
        settings['transcription_model'] = new_model
        user.settings = settings
        await session.commit()
    
    logger.info(f"SETTING CHANGED: User {user_id} set transcription model to {new_model}")
    await query.answer(f"Транскрибація: {new_model}")
    await transcription_menu(update, context)

async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        current_lang = user.settings.get('language', 'uk')
    
    text = f"🌐 <b>Current Language:</b> {current_lang.upper()}"
    langs = [('🇺🇦 Українська', 'uk'), ('🇬🇧 English', 'en'), ('🇷🇺 Русский', 'ru')]
    keyboard = []
    for label, code in langs:
        btn_text = f"✅ {label}" if current_lang == code else label
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"set_lang_{code}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_language_gui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_lang = query.data.replace("set_lang_", "")
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        settings = dict(user.settings)
        settings['language'] = new_lang
        user.settings = settings
        await session.commit()
    
    logger.info(f"SETTING CHANGED: User {user_id} set language to {new_lang}")
    await query.answer(f"Language changed to {new_lang}")
    await language_menu(update, context)

async def model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Код без змін, тільки логи можна не додавати тут, бо це перегляд)
    query = update.callback_query
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        current_model = user.settings.get('model', DEFAULT_SETTINGS['model'])
        res_openai = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == 'openai', APIKey.is_active == True))
        has_openai = res_openai.scalar_one_or_none() is not None
        res_google = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == 'google', APIKey.is_active == True))
        has_google_personal = res_google.scalar_one_or_none() is not None
        is_admin = user_id in ADMIN_IDS
        has_google_system = bool(os.getenv("GOOGLE_API_KEY"))
        gemini_available = has_google_system or has_google_personal or is_admin

    text = f"🤖 <b>Поточна модель:</b> <code>{current_model}</code>\n"
    keyboard = []
    for m in AVAILABLE_MODELS['openai']['common']: keyboard.append([InlineKeyboardButton(f"✅ {m}" if current_model == m else m, callback_data=f"set_model_{m}")])
    if has_openai or is_admin:
        for m in AVAILABLE_MODELS['openai']['advanced']: keyboard.append([InlineKeyboardButton(f"✅ {m}" if current_model == m else m, callback_data=f"set_model_{m}")])
    if gemini_available:
        for m in AVAILABLE_MODELS['google']: keyboard.append([InlineKeyboardButton(f"✅ {m}" if current_model == m else m, callback_data=f"set_model_{m}")])
    if has_openai or is_admin: keyboard.append([InlineKeyboardButton("✍️ Вписати свою...", callback_data="ask_custom_model")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_model = query.data.replace("set_model_", "")
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        settings = dict(user.settings)
        settings['model'] = new_model
        user.settings = settings
        await session.commit()
    
    logger.info(f"SETTING CHANGED: User {user_id} set chat model to {new_model}")
    await query.answer(f"Модель: {new_model}")
    await model_menu(update, context)

# ... (persona_menu, set_persona, timezone_menu, set_timezone_btn etc - аналогічно додаємо логи)

async def persona_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Без змін)
    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        current_prompt = user.system_prompt
    current_persona_key = "custom"
    for key, data in PERSONAS.items():
        if data['prompt'] == current_prompt: current_persona_key = key; break
    text = f"🎭 <b>Оберіть режим:</b>"
    keyboard = []
    row = []
    for key, data in PERSONAS.items():
        label = f"✅ {data['name']}" if current_persona_key == key else data['name']
        row.append(InlineKeyboardButton(label, callback_data=f"set_persona_{key}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✍️ Свій промпт...", callback_data="ask_custom_prompt"), InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.replace("set_persona_", "")
    user_id = update.effective_user.id
    if key in PERSONAS:
        async with AsyncSessionLocal() as session:
            user = await get_or_create_user_internal(session, user_id)
            user.system_prompt = PERSONAS[key]['prompt']
            await session.commit()
        logger.info(f"SETTING CHANGED: User {user_id} set persona to {key}")
        await query.answer(f"Режим: {PERSONAS[key]['name']}")
    await persona_menu(update, context)

async def timezone_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Без змін)
    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        current_tz = user.settings.get('timezone', 'Europe/Kiev')
    text = (f"🌍 <b>Часовий пояс</b>\n\nПоточний: <code>{current_tz}</code>\nЦе впливає на час нагадувань.")
    keyboard = [
        [InlineKeyboardButton("🇺🇦 Kyiv", callback_data="set_tz_Europe/Kiev")],
        [InlineKeyboardButton("🇬🇧 London", callback_data="set_tz_Europe/London")],
        [InlineKeyboardButton("🌐 UTC", callback_data="set_tz_UTC")],
        [InlineKeyboardButton("✍️ Вписати своє місто", callback_data="ask_custom_tz")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")]
    ]
    try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_timezone_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_tz = query.data.replace("set_tz_", "")
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        settings = dict(user.settings)
        settings['timezone'] = new_tz
        user.settings = settings
        await session.commit()
    
    logger.info(f"SETTING CHANGED: User {user_id} set timezone to {new_tz}")
    await query.answer(f"Встановлено: {new_tz}")
    await timezone_menu(update, context)

async def ask_custom_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Напишіть назву часового поясу (наприклад `Europe/Warsaw`, `America/New_York` або просто `CET`).", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]),
        parse_mode="Markdown"
    )
    return WAITING_FOR_TIMEZONE

async def save_custom_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz_input = update.message.text.strip()
    user_id = update.effective_user.id
    try:
        zoneinfo.ZoneInfo(tz_input)
    except Exception:
        await update.message.reply_text(
            "❌ Некоректна назва поясу. Спробуйте `Europe/London` або оберіть зі списку.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 До меню", callback_data="cancel_conv")]])
        )
        return WAITING_FOR_TIMEZONE

    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        settings = dict(user.settings)
        settings['timezone'] = tz_input
        user.settings = settings
        await session.commit()
    
    logger.info(f"SETTING CHANGED: User {user_id} set custom timezone to {tz_input}")
    await update.message.reply_text(f"✅ Часовий пояс змінено на {tz_input}")
    return ConversationHandler.END

# ... (ask_custom_model, save_custom_model, ask_custom_prompt, save_custom_prompt, keys_menu, ask_for_key, save_key, delete_key, reset_context_handler, cancel_conversation БЕЗ ЗМІН, ТАМ ЛОГИ НЕ КРИТИЧНІ)
async def ask_custom_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введіть назву моделі (наприклад, gpt-4-32k).", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]])
    )
    return WAITING_FOR_CUSTOM_MODEL

async def save_custom_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    model = update.message.text.strip()
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        settings = dict(user.settings)
        settings['model'] = model
        user.settings = settings
        await session.commit()
    logger.info(f"SETTING CHANGED: User {user_id} set custom model to {model}")
    await update.message.reply_text(f"✅ Модель встановлена: {model}")
    return ConversationHandler.END

async def ask_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Надішліть новий системний промпт.", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]])
    )
    return WAITING_FOR_CUSTOM_PROMPT

async def save_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text.strip()
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user_internal(session, user_id)
        user.system_prompt = prompt
        await session.commit()
    logger.info(f"SETTING CHANGED: User {user_id} updated system prompt")
    await update.message.reply_text("✅ Системний промпт оновлено!")
    return ConversationHandler.END

async def keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        keys = (await session.execute(select(APIKey).where(APIKey.user_id==user_id, APIKey.is_active==True))).scalars().all()
    has_o = any(k.provider=='openai' for k in keys)
    has_g = any(k.provider=='google' for k in keys)
    txt = "<b>🔑 Ключі API</b>\n\nТут ви можете додати свої ключі для зняття обмежень."
    kb = []
    if has_o:
        kb.append([InlineKeyboardButton("❌ Видалити OpenAI Key", callback_data="del_key_openai")])
        txt += "\n✅ OpenAI: Власний ключ."
    else: txt += "\n⚠️ OpenAI: Використовується системний (лімітований)."
    if has_g:
        kb.append([InlineKeyboardButton("❌ Видалити Google Key", callback_data="del_key_google")])
        txt += "\n✅ Google: Власний ключ."
    else: txt += "\n⚠️ Google: Використовується системний."
    kb.append([InlineKeyboardButton("➕ Додати ключ", callback_data="add_key_openai"), InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    except BadRequest: pass

async def ask_for_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Надішліть ваш API ключ (sk-... або AIza...).\nСистема автоматично визначить провайдера.", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]])
    )
    return WAITING_FOR_KEY

async def save_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    key_text = update.message.text.strip()
    try: await update.message.delete()
    except: pass
    provider = None
    if key_text.startswith("sk-"): provider = "openai"
    elif key_text.startswith("AIza"): provider = "google"
    if not provider:
        await update.message.reply_text("❌ Невірний формат ключа. Спробуйте ще раз або скасуйте.")
        return WAITING_FOR_KEY
    encrypted = key_manager.encrypt(key_text)
    async with AsyncSessionLocal() as session:
        await get_or_create_user_internal(session, user_id)
        old_keys = await session.execute(select(APIKey).where(APIKey.user_id==user_id, APIKey.provider==provider))
        for k in old_keys.scalars().all(): await session.delete(k)
        session.add(APIKey(user_id=user_id, provider=provider, encrypted_key=encrypted, is_active=True))
        await session.commit()
    logger.info(f"KEY ADDED: User {user_id} added {provider} key")
    await update.message.reply_text(f"✅ Ключ {provider} успішно збережено!")
    return ConversationHandler.END

async def delete_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    provider = query.data.replace("del_key_", "")
    async with AsyncSessionLocal() as session:
        old_keys = await session.execute(select(APIKey).where(APIKey.user_id==user_id, APIKey.provider==provider))
        for k in old_keys.scalars().all(): await session.delete(k)
        await session.commit()
    logger.info(f"KEY DELETED: User {user_id} deleted {provider} key")
    await query.answer("Ключ видалено!")
    await keys_menu(update, context)

async def reset_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Контекст очищено!", show_alert=True)
    await settings_menu(update, context)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        try: await update.callback_query.edit_message_text("Дію скасовано.")
        except BadRequest: pass
        await settings_menu(update, context)
    else:
        await update.message.reply_text("Дію скасовано.")
    return ConversationHandler.END