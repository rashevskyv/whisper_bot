import logging
import os
import zoneinfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.helpers import get_or_create_user
from bot.handlers.common import get_user_model_settings, get_effective_timezone, normalize_timezone
from bot.utils.security import key_manager
from bot.utils.context import context_manager
from bot.utils.queue_manager import get_queue_stats, clear_pending_tasks, clear_all_tasks
from config import PERSONAS, DEFAULT_SETTINGS, DEFAULT_GROUP_SETTINGS, ADMIN_IDS, AVAILABLE_MODELS, BOT_TIMEZONE

logger = logging.getLogger(__name__)

WAITING_FOR_KEY = 1
WAITING_FOR_CUSTOM_MODEL = 2
WAITING_FOR_CUSTOM_PROMPT = 3
WAITING_FOR_TIMEZONE = 4
WAITING_FOR_PHOTO_PROMPT = 5

async def check_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перевіряє, чи є користувач адміном групи. Якщо ні - кидає алерт."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # У приватних чатах перевірка не потрібна (сам собі адмін)
    if update.effective_chat.type == 'private':
        return True

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in ['administrator', 'creator']:
            return True
    except Exception as e:
        logger.error(f"Error checking admin: {e}")

    # Якщо дійшло сюди - користувач не адмін
    if update.callback_query:
        await update.callback_query.answer("🔒 Налаштування доступні лише адміністраторам групи!", show_alert=True)
    else:
        try: await update.message.reply_text("🔒 Тільки для адміністраторів.")
        except: pass

    return False

# --- УНІВЕРСАЛЬНА ФУНКЦІЯ ОНОВЛЕННЯ ---
async def update_settings(chat_id, **kwargs):
    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        if not obj:
            is_group = chat_id < 0
            initial_settings = DEFAULT_GROUP_SETTINGS.copy() if is_group else DEFAULT_SETTINGS.copy()
            obj = User(
                id=chat_id,
                settings=initial_settings,
                system_prompt=initial_settings.get('system_prompt', '')
            )
            session.add(obj)
        settings = dict(obj.settings or {})
        settings.update(kwargs)
        obj.settings = settings
        await session.commit()

async def update_setting(chat_id, key, value):
    await update_settings(chat_id, **{key: value})

TIMEZONE_ONBOARDING_TEXT = "🌍 Щоб правильно показувати час нагадувань, оберіть ваш часовий пояс."

def get_timezone_keyboard(include_back: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🇺🇦 Kyiv", callback_data="set_tz_Europe/Kyiv")],
        [InlineKeyboardButton("🇬🇧 London", callback_data="set_tz_Europe/London")],
        [InlineKeyboardButton("🌐 UTC", callback_data="set_tz_UTC")],
        [InlineKeyboardButton("✍️ Інший timezone", callback_data="ask_custom_tz")],
    ]
    if include_back:
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🧠 Чат Модель", callback_data="model_menu"),
            InlineKeyboardButton("🎭 Персона", callback_data="persona_menu")
        ],
        [
            InlineKeyboardButton("🌐 Мова", callback_data="lang_menu"),
            InlineKeyboardButton("🌍 Часовий пояс", callback_data="timezone_menu")
        ],
        [
            InlineKeyboardButton("🔑 Ключі API", callback_data="keys_menu")
        ],
        [
            InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # 1. ПЕРЕВІРКА ПРАВ
    if not await check_group_admin(update, context): return

    target_id = update.effective_chat.id
    user_id = update.effective_user.id
    is_bot_admin = user_id in ADMIN_IDS
    is_group = target_id < 0

    async with AsyncSessionLocal() as session:
        db_obj = await session.get(User, target_id)
        if not db_obj:
            await get_or_create_user(update.effective_chat)
            db_obj = await session.get(User, target_id)

        settings = db_obj.settings if db_obj else (DEFAULT_GROUP_SETTINGS if is_group else DEFAULT_SETTINGS)
        show_debug = settings.get('show_model_name', False)
        context_mode = settings.get('context_mode', 'shared' if is_group else 'personal')
        default_video = DEFAULT_GROUP_SETTINGS.get('video_repost', True) if is_group else DEFAULT_SETTINGS.get('video_repost', True)
        video_repost = settings.get('video_repost', default_video)

    debug_icon = "✅" if show_debug else "❌"
    video_icon = "✅" if video_repost else "❌"

    keyboard = [
        [
            InlineKeyboardButton("🧠 Чат Модель", callback_data="model_menu"),
            InlineKeyboardButton("🎭 Персона", callback_data="persona_menu")
        ],
        [
            InlineKeyboardButton("🌐 Мова", callback_data="lang_menu"),
            InlineKeyboardButton("🌍 Часовий пояс", callback_data="timezone_menu")
        ]
    ]

    if is_group:
        mode_btn_text = "👥 Контекст: Спільний" if context_mode == 'shared' else "👤 Контекст: Особистий"
        keyboard.append([InlineKeyboardButton(mode_btn_text, callback_data="toggle_context_mode")])

    keyboard.append([InlineKeyboardButton(f"🎥 Репост відео: {video_icon}", callback_data="toggle_video_repost")])

    if update.effective_chat.type == 'private':
        keyboard.append([InlineKeyboardButton("🔑 Ключі API", callback_data="keys_menu")])

    if is_bot_admin:
        keyboard.append([
            InlineKeyboardButton(f"{debug_icon} Режим налагодження", callback_data="toggle_debug"),
            InlineKeyboardButton("📥 Черга завдань", callback_data="queue_menu")
        ])
    elif update.effective_chat.type == 'private':
        keyboard.append([
            InlineKeyboardButton("📥 Черга завдань", callback_data="queue_menu")
        ])

    keyboard.append([InlineKeyboardButton("🗑 Очистити контекст", callback_data="reset_context")])
    keyboard.append([InlineKeyboardButton("🔙 Закрити", callback_data="close_menu")])

    try:
        await query.edit_message_text(
            f"⚙️ <b>Налаштування:</b> {update.effective_chat.title or 'Приват'}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except BadRequest: pass

async def queue_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_group_admin(update, context): return
    if query:
        await query.answer()

    stats = await get_queue_stats()
    pending = stats["pending"]
    processing = stats["processing"]
    done = stats["done"]
    error = stats["error"]
    total = stats["total"]

    msg_text = (
        f"📥 <b>Черга завантажень (Userbot):</b>\n\n"
        f"• ⏳ Очікують (pending): <b>{pending}</b>\n"
        f"• ⚙️ В обробці (processing): <b>{processing}</b>\n"
        f"• ✅ Виконано (done): <b>{done}</b>\n"
        f"• ❌ Помилки / Timeout: <b>{error}</b>\n"
        f"• 📊 Всього записів: <b>{total}</b>\n"
    )

    keyboard = [
        [InlineKeyboardButton(f"🗑 Очистити очікуючі ({pending})", callback_data="queue_clear_pending")],
        [InlineKeyboardButton("💥 Очистити ВСІ завдання", callback_data="queue_clear_all")],
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="queue_menu"),
            InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        try:
            await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')
        except BadRequest:
            pass
    elif update.effective_message:
        await update.effective_message.reply_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')

async def queue_clear_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_group_admin(update, context): return

    deleted_count = await clear_pending_tasks()
    if query:
        await query.answer(f"Очищено {deleted_count} очікуючих завдань!", show_alert=True)
    await queue_menu(update, context)

async def queue_clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_group_admin(update, context): return

    deleted_count = await clear_all_tasks()
    if query:
        await query.answer(f"Повністю очищено чергу ({deleted_count} записів)!", show_alert=True)
    await queue_menu(update, context)

async def toggle_context_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_group_admin(update, context): return

    target_id = update.effective_chat.id
    new_mode = 'shared'
    async with AsyncSessionLocal() as session:
        user = await session.get(User, target_id)
        if user:
            settings = dict(user.settings)
            current_mode = settings.get('context_mode', 'shared')
            new_mode = 'personal' if current_mode == 'shared' else 'shared'
            settings['context_mode'] = new_mode
            user.settings = settings
            await session.commit()

    await query.answer(f"Режим контексту: {'Спільний' if new_mode == 'shared' else 'Особистий'}")
    await settings_menu(update, context)

async def toggle_video_repost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_group_admin(update, context): return

    target_id = update.effective_chat.id
    is_group = target_id < 0
    default_val = DEFAULT_GROUP_SETTINGS.get('video_repost', True) if is_group else DEFAULT_SETTINGS.get('video_repost', True)
    new_state = not default_val

    async with AsyncSessionLocal() as session:
        user = await session.get(User, target_id)
        if user:
            settings = dict(user.settings or {})
            current_val = settings.get('video_repost', default_val)
            new_state = not current_val
            settings['video_repost'] = new_state
            user.settings = settings
            await session.commit()

    await query.answer(f"Репост відео: {'Увімкнено' if new_state else 'Вимкнено'}")
    await settings_menu(update, context)

async def toggle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Тільки власник бота може вмикати дебаг, навіть в групах
    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("🔒 Тільки для власника бота.")
        return

    target_id = update.effective_chat.id
    new_state = False
    async with AsyncSessionLocal() as session:
        user = await session.get(User, target_id)
        if user:
            settings = dict(user.settings)
            new_state = not settings.get('show_model_name', False)
            settings['show_model_name'] = new_state
            user.settings = settings
            await session.commit()

    await query.answer(f"Дебаг: {'Ввімкнено' if new_state else 'Вимкнено'}")
    await settings_menu(update, context)

async def close_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Закрити може кожен? Ні, краще теж адмін, щоб не заважали
    if not await check_group_admin(update, context): return
    await query.answer()
    try: await query.message.delete()
    except: pass

async def reset_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_group_admin(update, context): return

    deleted_count = await context_manager.clear_context(update.effective_chat.id)
    await query.answer(f"Контекст діалогу очищено! Видалено повідомлень: {deleted_count}", show_alert=True)
    await settings_menu(update, context)

# --- MENU HANDLERS ---

async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        current = obj.settings.get('language', 'uk') if obj else 'uk'

    langs = [('🇺🇦 Українська', 'uk'), ('🇬🇧 English', 'en'), ('🇷🇺 Русский', 'ru')]
    keyboard = []
    for label, code in langs:
        btn_text = f"✅ {label}" if current == code else label
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"set_lang_{code}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(f"🌐 Мова чату: {current}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_language_gui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    new_lang = query.data.replace("set_lang_", "")
    await update_setting(update.effective_chat.id, 'language', new_lang)
    await query.answer(f"Мова змінена: {new_lang}")
    await language_menu(update, context)

async def model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        current = obj.settings.get('model', DEFAULT_SETTINGS['model']) if obj else DEFAULT_SETTINGS['model']

    keyboard = []

    # 1. OpenRouter Сучасні Моделі
    for m in AVAILABLE_MODELS.get('openrouter', []):
        m_id = m['id']
        m_name = m['name']
        btn_label = f"✅ {m_name}" if current == m_id else m_name
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"set_model_{m_id}")])

    # 2. Прямий OpenAI (якщо є ключ або адмін)
    if bool(os.getenv("OPENAI_API_KEY")) or update.effective_user.id in ADMIN_IDS:
        for m in AVAILABLE_MODELS.get('openai', {}).get('common', []):
            keyboard.append([InlineKeyboardButton(f"✅ {m}" if current == m else m, callback_data=f"set_model_{m}")])
        for m in AVAILABLE_MODELS.get('openai', {}).get('advanced', []):
            keyboard.append([InlineKeyboardButton(f"✅ {m}" if current == m else m, callback_data=f"set_model_{m}")])

    # 3. Прямий Google (якщо є ключ або адмін)
    if bool(os.getenv("GOOGLE_API_KEY")) or update.effective_user.id in ADMIN_IDS:
        for m in AVAILABLE_MODELS.get('google', []):
            keyboard.append([InlineKeyboardButton(f"✅ {m}" if current == m else m, callback_data=f"set_model_{m}")])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    
    current_label = current
    for m in AVAILABLE_MODELS.get('openrouter', []):
        if m['id'] == current:
            current_label = m['name']
            break

    try:
        await query.edit_message_text(
            f"🤖 <b>Модель чату:</b> <code>{current_label}</code>\n\nОберіть модель для генерації відповідей:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except BadRequest: pass

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    new_model = query.data.replace("set_model_", "")
    await update_setting(update.effective_chat.id, 'model', new_model)
    await query.answer(f"Модель: {new_model}")
    await model_menu(update, context)

async def persona_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        current_prompt = obj.system_prompt if obj else PERSONAS['assistant']['prompt']

    current_key = "custom"
    for key, data in PERSONAS.items():
        if data['prompt'] == current_prompt: current_key = key; break

    keyboard = []
    row = []
    for key, data in PERSONAS.items():
        label = f"✅ {data['name']}" if current_key == key else data['name']
        row.append(InlineKeyboardButton(label, callback_data=f"set_persona_{key}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text("🎭 Оберіть характер бота:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    key = query.data.replace("set_persona_", "")

    if key in PERSONAS:
        async with AsyncSessionLocal() as session:
            obj = await session.get(User, update.effective_chat.id)
            if obj:
                obj.system_prompt = PERSONAS[key]['prompt']
                await session.commit()
        await query.answer(f"Режим: {PERSONAS[key]['name']}")
    await persona_menu(update, context)

async def timezone_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    chat_id = update.effective_chat.id

    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        settings = obj.settings if (obj and obj.settings) else {}

    is_private = update.effective_chat.type == 'private'
    if is_private and not settings.get('timezone_selected'):
        display_tz = "&lt;не обрано&gt;"
    else:
        raw_tz = settings.get('timezone', BOT_TIMEZONE)
        display_tz = str(normalize_timezone(raw_tz) or raw_tz)

    keyboard = get_timezone_keyboard(include_back=True)
    try: await query.edit_message_text(f"🌍 Часовий пояс: <code>{display_tz}</code>", reply_markup=keyboard, parse_mode='HTML')
    except BadRequest: pass

async def set_timezone_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    new_tz = query.data.replace("set_tz_", "")
    norm_tz = normalize_timezone(new_tz) or new_tz.strip()
    try:
        zoneinfo.ZoneInfo(norm_tz)
    except Exception:
        await query.answer("❌ Невірна зона.", show_alert=True)
        return
    await update_settings(update.effective_chat.id, timezone=norm_tz, timezone_selected=True)
    await query.answer(f"Встановлено: {norm_tz}")
    await timezone_menu(update, context)

# --- PRIVATE ONLY ---

async def keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        await update.callback_query.answer("❌ Ключі тільки в приваті.", show_alert=True)
        return

    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        keys = (await session.execute(select(APIKey).where(APIKey.user_id==user_id, APIKey.is_active==True))).scalars().all()
    has_or = any(k.provider=='openrouter' for k in keys)
    has_o = any(k.provider=='openai' for k in keys)
    has_g = any(k.provider=='google' for k in keys)
    
    txt = (
        "<b>🔑 Ключі API</b>\n\n"
        "Тут ви можете додати свої персональні ключі:\n"
        "• <b>OpenRouter</b> (універсальний ключ для GPT-5.6 Luna, DeepSeek V4, Gemini 3.7, Qwen, Mistral)\n"
        "• <b>OpenAI</b> (для прямого API та транскрибації)\n"
        "• <b>Google GenAI</b> (для прямого Gemini)"
    )
    kb = []
    if has_or: kb.append([InlineKeyboardButton("❌ Видалити OpenRouter Key", callback_data="del_key_openrouter")])
    if has_o: kb.append([InlineKeyboardButton("❌ Видалити OpenAI Key", callback_data="del_key_openai")])
    if has_g: kb.append([InlineKeyboardButton("❌ Видалити Google Key", callback_data="del_key_google")])
    kb.append([InlineKeyboardButton("➕ Додати ключ", callback_data="add_key_openai"), InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    except BadRequest: pass

async def ask_for_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text(
        "Надішліть ваш API ключ:\n\n"
        "• <code>sk-or-v1-...</code> — OpenRouter\n"
        "• <code>sk-...</code> — OpenAI\n"
        "• <code>AIza...</code> — Google Gemini",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]),
        parse_mode='HTML'
    )
    return WAITING_FOR_KEY

async def save_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    key_text = update.message.text.strip()
    try: await update.message.delete()
    except: pass
    
    if key_text.startswith(("sk-or-v1-", "sk-or-")):
        provider = "openrouter"
    elif key_text.startswith("sk-"):
        provider = "openai"
    elif key_text.startswith("AIza"):
        provider = "google"
    else:
        provider = "openrouter" if len(key_text) > 25 else None
        
    if not provider:
        await update.message.reply_text("❌ Нерозпізнаний формат ключа. Спробуйте ще раз або скасуйте.")
        return WAITING_FOR_KEY
        
    encrypted = key_manager.encrypt(key_text)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
             from config import DEFAULT_SETTINGS
             user = User(id=user_id, settings=DEFAULT_SETTINGS, system_prompt=DEFAULT_SETTINGS['system_prompt'])
             session.add(user); await session.flush()
        old_keys = await session.execute(select(APIKey).where(APIKey.user_id==user_id, APIKey.provider==provider))
        for k in old_keys.scalars().all(): await session.delete(k)
        session.add(APIKey(user_id=user_id, provider=provider, encrypted_key=encrypted, is_active=True))
        await session.commit()
    await update.message.reply_text(f"✅ Ключ <b>{provider.upper()}</b> успішно збережено!", parse_mode='HTML')
    return ConversationHandler.END

async def delete_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    provider = query.data.replace("del_key_", "")
    async with AsyncSessionLocal() as session:
        old_keys = await session.execute(select(APIKey).where(APIKey.user_id==user_id, APIKey.provider==provider))
        for k in old_keys.scalars().all(): await session.delete(k)
        await session.commit()
    await query.answer("Ключ видалено!")
    await keys_menu(update, context)

# Конверсейшени для налаштувань (вхід тільки через кнопки, які вже захищені)
# Але для надійності додамо перевірку при старті конв.

async def ask_custom_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return ConversationHandler.END
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Введіть назву моделі:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_CUSTOM_MODEL

async def save_custom_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Тут складніше, бо це MessageHandler. Перевірка адміна потрібна.
    if not await check_group_admin(update, context): return ConversationHandler.END
    model = update.message.text.strip()
    await update_setting(update.effective_chat.id, 'model', model)
    await update.message.reply_text(f"✅ Модель: {model}")
    return ConversationHandler.END

async def ask_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return ConversationHandler.END
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Надішліть промпт:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_CUSTOM_PROMPT

async def save_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return ConversationHandler.END
    prompt = update.message.text.strip()
    async with AsyncSessionLocal() as session:
        obj = await session.get(User, update.effective_chat.id)
        if obj:
            obj.system_prompt = prompt
            await session.commit()
    await update.message.reply_text("✅ Промпт оновлено!")
    return ConversationHandler.END

async def ask_custom_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return ConversationHandler.END
    query = update.callback_query; await query.answer()
    text = (
        "Введіть назву часового поясу за стандартом IANA.\n\n"
        "Приклади:\n"
        "• <code>Europe/Warsaw</code>\n"
        "• <code>America/Toronto</code>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]])
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode='HTML')
    except BadRequest:
        if update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=markup, parse_mode='HTML')
    return WAITING_FOR_TIMEZONE

async def save_custom_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return ConversationHandler.END
    tz = update.message.text.strip()
    norm_tz = normalize_timezone(tz) or tz
    try:
        zoneinfo.ZoneInfo(norm_tz)
    except Exception:
        await update.message.reply_text(
            "❌ Невірна назва часового поясу IANA. Спробуйте ще раз (наприклад, <code>Europe/Warsaw</code> або <code>America/Toronto</code>):",
            parse_mode='HTML'
        )
        return WAITING_FOR_TIMEZONE
    await update_settings(update.effective_chat.id, timezone=norm_tz, timezone_selected=True)
    await update.message.reply_text(f"✅ Встановлено часовий пояс: <code>{norm_tz}</code>", parse_mode='HTML')
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        try: await update.callback_query.edit_message_text("Дію скасовано.")
        except BadRequest: pass
        await settings_menu(update, context)
    else:
        await update.message.reply_text("Дію скасовано.")
    return ConversationHandler.END

# --- PHOTO PROMPT ---
async def ask_photo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    photo_msg_id = query.message.reply_to_message.message_id
    context.user_data['photo_msg_id'] = photo_msg_id
    await query.answer()
    try:
        await query.edit_message_text("💬 Надішліть запит:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    except BadRequest:
        await query.message.reply_text("💬 Надішліть запит:", reply_to_message_id=photo_msg_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_PHOTO_PROMPT

async def process_photo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_msg_id = context.user_data.get('photo_msg_id')
    chat_id = update.effective_chat.id
    if not photo_msg_id:
        await update.message.reply_text("❌ Помилка ID.")
        return ConversationHandler.END
    try:
        temp = await update.message.reply_text("...")
        await context.bot.edit_message_text("👀 Дивлюсь...", chat_id=chat_id, message_id=temp.message_id)
        await update.message.reply_text("❌ Не реалізовано (використовуйте реплай).", reply_to_message_id=update.message.message_id)
    except Exception as e: await update.message.reply_text(f"❌ {e}")
    return ConversationHandler.END