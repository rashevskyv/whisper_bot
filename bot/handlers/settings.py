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
from bot.utils.security import key_manager
from bot.utils.context import context_manager
from config import PERSONAS, DEFAULT_SETTINGS, DEFAULT_GROUP_SETTINGS, ADMIN_IDS, AVAILABLE_MODELS

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
async def update_setting(chat_id, key, value):
    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        if obj:
            settings = dict(obj.settings)
            settings[key] = value
            obj.settings = settings
            await session.commit()

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

    debug_icon = "✅" if show_debug else "❌"

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

    if update.effective_chat.type == 'private':
        keyboard.append([InlineKeyboardButton("🔑 Ключі API", callback_data="keys_menu")])

    if is_bot_admin:
        keyboard.append([
            InlineKeyboardButton(f"{debug_icon} Режим налагодження", callback_data="toggle_debug")
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
    for m in AVAILABLE_MODELS['openai']['common']:
        keyboard.append([InlineKeyboardButton(f"✅ {m}" if current == m else m, callback_data=f"set_model_{m}")])

    if bool(os.getenv("OPENAI_API_KEY")) or update.effective_user.id in ADMIN_IDS:
        for m in AVAILABLE_MODELS['openai']['advanced']:
            keyboard.append([InlineKeyboardButton(f"✅ {m}" if current == m else m, callback_data=f"set_model_{m}")])

    if bool(os.getenv("GOOGLE_API_KEY")) or update.effective_user.id in ADMIN_IDS:
        for m in AVAILABLE_MODELS['google']:
            keyboard.append([InlineKeyboardButton(f"✅ {m}" if current == m else m, callback_data=f"set_model_{m}")])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(f"🤖 Модель чату: {current}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
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
        current_tz = obj.settings.get('timezone', 'Europe/Kiev') if obj else 'Europe/Kiev'

    keyboard = [
        [InlineKeyboardButton("🇺🇦 Kyiv", callback_data="set_tz_Europe/Kiev")],
        [InlineKeyboardButton("🇬🇧 London", callback_data="set_tz_Europe/London")],
        [InlineKeyboardButton("🌐 UTC", callback_data="set_tz_UTC")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")]
    ]
    try: await query.edit_message_text(f"🌍 Часовий пояс: <code>{current_tz}</code>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except BadRequest: pass

async def set_timezone_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return
    query = update.callback_query
    new_tz = query.data.replace("set_tz_", "")
    await update_setting(update.effective_chat.id, 'timezone', new_tz)
    await query.answer(f"Встановлено: {new_tz}")
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
    has_o = any(k.provider=='openai' for k in keys)
    has_g = any(k.provider=='google' for k in keys)
    txt = "<b>🔑 Ключі API</b>\n\nТут ви можете додати свої ключі для зняття обмежень."
    kb = []
    if has_o: kb.append([InlineKeyboardButton("❌ Видалити OpenAI Key", callback_data="del_key_openai")])
    if has_g: kb.append([InlineKeyboardButton("❌ Видалити Google Key", callback_data="del_key_google")])
    kb.append([InlineKeyboardButton("➕ Додати ключ", callback_data="add_key_openai"), InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    try: await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    except BadRequest: pass

async def ask_for_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Надішліть ключ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_KEY

async def save_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    key_text = update.message.text.strip()
    try: await update.message.delete()
    except: pass
    provider = "openai" if key_text.startswith("sk-") else "google" if key_text.startswith("AIza") else None
    if not provider: return WAITING_FOR_KEY
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
    await update.message.reply_text(f"✅ Ключ збережено!")
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
    await query.edit_message_text("Введіть зону (напр. CET):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_TIMEZONE

async def save_custom_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_admin(update, context): return ConversationHandler.END
    tz = update.message.text.strip()
    try: zoneinfo.ZoneInfo(tz)
    except:
        await update.message.reply_text("❌ Невірна зона.")
        return WAITING_FOR_TIMEZONE
    await update_setting(update.effective_chat.id, 'timezone', tz)
    await update.message.reply_text(f"✅ Час: {tz}")
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