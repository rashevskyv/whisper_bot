import html
import logging
from typing import Optional, List, Tuple
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from sqlalchemy.future import select
from sqlalchemy import desc, and_
from bot.database.session import AsyncSessionLocal
from bot.database.models import User, UserMemory
from bot.utils.helpers import get_or_create_user
from bot.handlers.settings import get_main_menu_keyboard
from bot.utils.scheduler import scheduler_service

logger = logging.getLogger(__name__)

def validate_glossary_terms(raw_input: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Парсить та валідує список термінів через кому для словника транскрибації.
    Зберігає порядок, обрізає пробіли, дедуплікує точні збіги.
    Відхиляє:
      - порожній ввід;
      - більше 30 термінів;
      - терміни довші за 100 символів;
      - терміни із забороненими символами OpenAI (<, >, \r, \n).
    Повертає (terms, None) у разі успіху або (None, error_message) у разі помилки.
    """
    if not raw_input or not raw_input.strip():
        return None, "❌ Вкажіть хоча б один непорожній термін."

    raw_terms = [t.strip() for t in raw_input.split(",") if t.strip()]
    terms = list(dict.fromkeys(raw_terms))

    if not terms:
        return None, "❌ Вкажіть хоча б один непорожній термін."

    if len(terms) > 30:
        return None, f"❌ Забагато термінів: {len(terms)}. Максимально дозволено 30."

    for t in terms:
        if len(t) > 100:
            return None, f"❌ Термін «{html.escape(t[:30])}...» перевищує 100 символів."
        if any(c in t for c in ('<', '>', '\r', '\n')):
            return None, f"❌ Термін «{html.escape(t[:30])}» містить заборонені символи (&lt;, &gt;, новий рядок)."

    return terms, None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    # Якщо це callback (кнопка "Назад" в меню)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"Вітаю, {user.first_name}! Ви в головному меню.",
            reply_markup=get_main_menu_keyboard()
        )
        return

    # Реєструємо користувача або групу в БД
    await get_or_create_user(chat)

    text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        f"Я — мульти-модельний AI бот (GPT-4o + Gemini).\n"
    )

    # ЛОГІКА КНОПОК
    if chat.type == 'private':
        # В особистих - показуємо кнопки
        has_reminders = await scheduler_service.get_reminders_count(chat.id) > 0
        buttons_row = [KeyboardButton("⚙️ Налаштування")]
        if has_reminders:
            buttons_row.insert(0, KeyboardButton("⏰ Нагадування"))

        reply_markup = ReplyKeyboardMarkup(
            [buttons_row],
            resize_keyboard=True,
            is_persistent=True
        )

        await update.message.reply_text(
            text + "Я вмію бачити, чути, шукати в інтернеті та аналізувати.",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        await update.message.reply_text("Швидкий доступ:", reply_markup=get_main_menu_keyboard())

    else:
        # В ГРУПАХ - ПРИБИРАЄМО КНОПКИ
        text += (
            f"<b>Команди для адміністраторів:</b>\n"
            f"• <code>налаштування</code> або <code>меню</code> — конфігурація бота.\n\n"
            f"Я відповідаю на реплаї або згадки."
        )

        # ReplyKeyboardRemove прибере клавіатуру у користувача, що викликав команду
        await update.message.reply_text(
            text,
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )

async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Зберігає факт у пам'ять користувача: /remember <fact>"""
    if not update.message: return
    user_id = update.effective_user.id

    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "ℹ️ Вкажіть факт для збереження.\nПриклад: <code>/remember Мене звати Олексій</code>",
            parse_mode="HTML"
        )
        return

    fact = parts[1].strip()
    if len(fact) > 500:
        await update.message.reply_text("❌ Факт занадто довгий (максимум 500 символів).")
        return

    await get_or_create_user(update.effective_user)

    async with AsyncSessionLocal() as session:
        memory = UserMemory(user_id=user_id, fact=fact)
        session.add(memory)
        await session.commit()
        await session.refresh(memory)
        memory_id = memory.id

    safe_fact = html.escape(fact)
    await update.message.reply_text(
        f"🧠 <b>Збережено в пам'ять (ID: {memory_id}):</b>\n<i>{safe_fact}</i>",
        parse_mode="HTML"
    )

async def memories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує список збережених фактів користувача (до 10 останніх)"""
    if not update.message: return
    user_id = update.effective_user.id

    async with AsyncSessionLocal() as session:
        stmt = (
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(desc(UserMemory.created_at), desc(UserMemory.id))
            .limit(10)
        )
        res = await session.execute(stmt)
        memories = res.scalars().all()

    if not memories:
        await update.message.reply_text("🧠 У вас немає збережених фактів. Додайте командою <code>/remember &lt;факт&gt;</code>.", parse_mode="HTML")
        return

    lines = ["🧠 <b>Ваші збережені факти:</b>\n"]
    for mem in reversed(memories):
        safe_fact = html.escape(mem.fact)
        lines.append(f"• <b>[ID: {mem.id}]</b> {safe_fact}")

    lines.append("\nЩоб видалити факт: <code>/forget &lt;ID&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видаляє факт з пам'яті користувача: /forget <id>"""
    if not update.message: return
    user_id = update.effective_user.id

    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "ℹ️ Вкажіть ID факту для видалення.\nПриклад: <code>/forget 12</code>",
            parse_mode="HTML"
        )
        return

    try:
        memory_id = int(parts[1].strip())
    except ValueError:
        await update.message.reply_text("❌ Некоректний ID. Вкажіть число.")
        return

    async with AsyncSessionLocal() as session:
        stmt = select(UserMemory).where(
            and_(
                UserMemory.id == memory_id,
                UserMemory.user_id == user_id
            )
        )
        res = await session.execute(stmt)
        memory = res.scalar_one_or_none()

        if not memory:
            await update.message.reply_text(f"❌ Факт з ID {memory_id} не знайдено серед ваших збережених фактів.")
            return

        await session.delete(memory)
        await session.commit()

    await update.message.reply_text(f"🗑 Факт [ID: {memory_id}] видалено з пам'яті.")

async def check_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перевіряє права адміна для команди в групі."""
    if update.effective_chat.type == 'private':
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        if member.status in ['administrator', 'creator']:
            return True
    except Exception as e:
        logger.error(f"Admin check error: {e}")
    await update.message.reply_text("🔒 Ця команда доступна лише адміністраторам групи.")
    return False

async def terms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Керування словником термінів транскрибації: /terms, /terms clear, /terms термін1, термін2"""
    if not update.message: return
    if not await check_chat_admin(update, context): return

    chat_id = update.effective_chat.id
    raw_text = update.message.text.strip()

    # Ensure chat row exists before session usage
    await get_or_create_user(update.effective_chat)

    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        async with AsyncSessionLocal() as session:
            obj = await session.get(User, chat_id)
            settings = obj.settings if obj else {}
            terms = settings.get('transcription_keywords', [])

        if not terms:
            await update.message.reply_text(
                "📖 Словник термінів для транскрибації порожній.\n"
                "Встановіть: <code>/terms термін1, термін2</code>",
                parse_mode='HTML'
            )
        else:
            terms_str = ", ".join(f"<code>{html.escape(t)}</code>" for t in terms)
            await update.message.reply_text(
                f"📖 <b>Словник термінів транскрибації ({len(terms)}):</b>\n{terms_str}\n\n"
                "Очистити: <code>/terms clear</code>\n"
                "Оновити: <code>/terms термін1, термін2</code>",
                parse_mode='HTML'
            )
        return

    arg = parts[1].strip()
    if arg.lower() == 'clear':
        async with AsyncSessionLocal() as session:
            obj = await session.get(User, chat_id)
            if obj:
                settings = dict(obj.settings or {})
                settings['transcription_keywords'] = []
                obj.settings = settings
                await session.commit()
        await update.message.reply_text("🗑 Словник термінів транскрибації очищено.")
        return

    terms, error_msg = validate_glossary_terms(arg)
    if error_msg:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return

    async with AsyncSessionLocal() as session:
        obj = await session.get(User, chat_id)
        if obj:
            settings = dict(obj.settings or {})
            settings['transcription_keywords'] = terms
            obj.settings = settings
            await session.commit()

    terms_str = ", ".join(f"<code>{html.escape(t)}</code>" for t in terms)
    await update.message.reply_text(
        f"✅ <b>Словник термінів оновлено ({len(terms)}):</b>\n{terms_str}",
        parse_mode='HTML'
    )