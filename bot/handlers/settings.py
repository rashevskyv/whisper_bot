import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, APIKey
from bot.utils.security import key_manager
from config import PERSONAS, DEFAULT_SETTINGS, ADMIN_IDS

logger = logging.getLogger(__name__)
WAITING_FOR_KEY = 1
WAITING_FOR_CUSTOM_MODEL = 2
WAITING_FOR_CUSTOM_PROMPT = 3

# --- ЕКСПОРТОВАНА ФУНКЦІЯ ДЛЯ ЄДИНОГО МЕНЮ ---
def get_main_menu_keyboard():
    """Повертає стандартну клавіатуру налаштувань"""
    keyboard = [
        [InlineKeyboardButton("🧠 AI (Модель/Персона)", callback_data="ai_menu")],
        [InlineKeyboardButton("🌐 Мова / Language", callback_data="lang_menu")],
        [InlineKeyboardButton("🔑 Мої ключі API", callback_data="keys_menu")],
        [InlineKeyboardButton("🧹 Очистити пам'ять", callback_data="reset_context")],
        [InlineKeyboardButton("🔙 Закрити меню", callback_data="close_menu")] # Змінено на закриття/видалення
    ]
    return InlineKeyboardMarkup(keyboard)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник callback для головного меню"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚙️ <b>Головні налаштування:</b>\n\nТут ви можете змінити мову, модель інтелекту та керувати ключами.", 
        reply_markup=get_main_menu_keyboard(), 
        parse_mode='HTML'
    )

async def close_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закриває меню (видаляє повідомлення)"""
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except:
        await query.message.edit_text("Меню закрито.")

# --- МЕНЮ МОВИ ---
async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        current_lang = user.settings.get('language', 'uk')

    text = f"🌐 <b>Current Language / Поточна мова:</b> {current_lang.upper()}\n\nЦя мова використовується для:\n• Відповідей бота\n• Розпізнавання голосових (Whisper)"
    
    langs = [('🇺🇦 Українська', 'uk'), ('🇬🇧 English', 'en'), ('🇷🇺 Русский', 'ru')]
    keyboard = []
    for label, code in langs:
        # Додаємо маркер обраної мови
        btn_text = f"✅ {label}" if current_lang == code else label
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"set_lang_{code}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def set_language_gui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_lang = query.data.replace("set_lang_", "")
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        settings = dict(user.settings)
        settings['language'] = new_lang
        user.settings = settings
        await session.commit()
    
    await query.answer(f"Language changed to {new_lang}")
    await language_menu(update, context)

# --- AI МЕНЮ ---
async def ai_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🤖 Обрати модель", callback_data="model_menu")],
        [InlineKeyboardButton("🎭 Обрати персону (Режим)", callback_data="persona_menu")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("🧠 <b>Налаштування інтелекту:</b>", reply_markup=reply_markup, parse_mode='HTML')

async def model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        current_model = user.settings.get('model', DEFAULT_SETTINGS['model'])
        
        # Перевірка наявності ключів
        keys_res = await session.execute(
            select(APIKey).where(APIKey.user_id == user_id, APIKey.is_active == True)
        )
        user_keys = keys_res.scalars().all()
        has_openai = any(k.provider == 'openai' for k in user_keys)
        has_google = any(k.provider == 'google' for k in user_keys)
        is_admin = user_id in ADMIN_IDS

    text = f"🤖 <b>Поточна модель:</b> <code>{current_model}</code>\n\n"
    keyboard = []
    
    # 1. OpenAI Models
    models = ["gpt-4o-mini"]
    if has_openai or is_admin:
        models.extend(["gpt-4o", "gpt-4-turbo"])
    
    # 2. Google Models
    if has_google or is_admin:
        models.extend([
            "gemini-2.0-flash-exp",
            "gemini-1.5-pro",
            "gemini-1.5-flash"
        ])
        text += "✅ <i>Gemini доступні.</i>\n"
    else:
        text += "🔒 <i>Gemini приховані.</i>\n"

    for m in models:
        label = f"✅ {m}" if current_model == m else m
        keyboard.append([InlineKeyboardButton(label, callback_data=f"set_model_{m}")])
            
    if has_openai or is_admin:
        keyboard.append([InlineKeyboardButton("✍️ Вписати свою...", callback_data="ask_custom_model")])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="ai_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_model = query.data.replace("set_model_", "")
    user_id = update.effective_user.id
    
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        settings = dict(user.settings)
        settings['model'] = new_model
        user.settings = settings
        await session.commit()
    
    await query.answer(f"Модель змінено на {new_model}")
    await model_menu(update, context)

# ... (інші функції persona_menu, set_persona, convs залишаються без змін, але важливо імпортувати їх коректно) ...
# Я додаю їх сюди для цілісності файлу

async def persona_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id); current_prompt = user.system_prompt
    current_persona_key = "custom"
    for key, data in PERSONAS.items():
        if data['prompt'] == current_prompt: current_persona_key = key; break
    text = f"🎭 <b>Оберіть режим:</b>"; keyboard = []; row = []
    for key, data in PERSONAS.items():
        label = f"✅ {data['name']}" if current_persona_key == key else data['name']
        row.append(InlineKeyboardButton(label, callback_data=f"set_persona_{key}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✍️ Свій промпт...", callback_data="ask_custom_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="ai_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def set_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; key = query.data.replace("set_persona_", ""); user_id = update.effective_user.id
    if key in PERSONAS:
        async with AsyncSessionLocal() as session: user = await session.get(User, user_id); user.system_prompt = PERSONAS[key]['prompt']; await session.commit()
        await query.answer(f"Режим: {PERSONAS[key]['name']}")
    await persona_menu(update, context)

async def ask_custom_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Введіть назву моделі.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_CUSTOM_MODEL

async def save_custom_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    model = update.message.text.strip(); user_id = update.effective_user.id
    async with AsyncSessionLocal() as session: user = await session.get(User, user_id); s = dict(user.settings); s['model'] = model; user.settings = s; await session.commit()
    await update.message.reply_text(f"✅ Модель: {model}"); return ConversationHandler.END

async def ask_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Введіть промпт.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_CUSTOM_PROMPT

async def save_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text.strip(); user_id = update.effective_user.id
    async with AsyncSessionLocal() as session: user = await session.get(User, user_id); user.system_prompt = prompt; await session.commit()
    await update.message.reply_text("✅ Промпт збережено!"); return ConversationHandler.END

async def keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.is_active == True))
        keys = result.scalars().all()
    has_openai = any(k.provider == 'openai' for k in keys)
    has_google = any(k.provider == 'google' for k in keys)
    text = "<b>🔑 Керування ключами</b>"
    keyboard = []
    if has_openai: keyboard.append([InlineKeyboardButton("❌ Видалити OpenAI", callback_data="del_key_openai")]); text += "\n✅ OpenAI: Власний ключ."
    else: text += "\n⚠️ OpenAI: Системний ключ."
    if has_google: keyboard.append([InlineKeyboardButton("❌ Видалити Google", callback_data="del_key_google")]); text += "\n✅ Google: Власний ключ."
    else: text += "\n⚠️ Google: Не встановлено."
    keyboard.append([InlineKeyboardButton("➕ Додати ключ", callback_data="add_key_openai")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="settings_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def ask_for_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Надішліть ключ (sk-... або AIza...).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="cancel_conv")]]))
    return WAITING_FOR_KEY

async def save_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; key_text = update.message.text.strip()
    try: await update.message.delete()
    except: pass
    provider = None
    if key_text.startswith("sk-"): provider = "openai"
    elif key_text.startswith("AIza"): provider = "google"
    if not provider: await update.message.reply_text("❌ Невідомий формат."); return WAITING_FOR_KEY
    enc = key_manager.encrypt(key_text)
    async with AsyncSessionLocal() as session:
        old = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == provider))
        for k in old.scalars().all(): await session.delete(k)
        session.add(APIKey(user_id=user_id, provider=provider, encrypted_key=enc, is_active=True)); await session.commit()
    await update.message.reply_text(f"✅ Ключ {provider} збережено!"); return ConversationHandler.END

async def delete_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user_id = update.effective_user.id
    provider = query.data.replace("del_key_", "")
    async with AsyncSessionLocal() as session:
        old = await session.execute(select(APIKey).where(APIKey.user_id == user_id, APIKey.provider == provider))
        for k in old.scalars().all(): await session.delete(k); await session.commit()
    await query.answer("Видалено!"); await keys_menu(update, context)

async def reset_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Очищено!", show_alert=True); await settings_menu(update, context)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query: await update.callback_query.answer(); await update.callback_query.edit_message_text("Скасовано."); await settings_menu(update, context)
    else: await update.message.reply_text("Скасовано."); return ConversationHandler.END