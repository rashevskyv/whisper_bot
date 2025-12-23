import logging
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, Application, ConversationHandler
from telegram.request import HTTPXRequest
from bot.database.session import init_db
from bot.handlers.commands import start
from bot.handlers.messages import (
    handle_text, handle_callback, handle_voice_video, handle_photo, 
    handle_internal_task # <-- Новий імпорт
)
from bot.handlers.settings import (
    settings_menu, keys_menu, ask_for_key, save_key, delete_key, 
    reset_context_handler, cancel_conversation, 
    ai_menu, model_menu, set_model, ask_custom_model, save_custom_model,
    persona_menu, set_persona, ask_custom_prompt, save_custom_prompt,
    language_menu, set_language_gui,
    WAITING_FOR_KEY, WAITING_FOR_CUSTOM_MODEL, WAITING_FOR_CUSTOM_PROMPT
)
from config import TOKEN
import warnings
from telegram.warnings import PTBUserWarning
warnings.filterwarnings("ignore", category=PTBUserWarning)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

async def post_init(application: Application):
    await init_db()
    print("📦 База даних перевірена/створена успішно.")

def main():
    if not TOKEN:
        print("❌ Помилка: Не задано BOT_TOKEN в .env!")
        return

    req = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0
    )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .request(req)
        .build()
    )

    # --- Conversations ---
    settings_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_for_key, pattern="^add_key_openai$")],
        states={WAITING_FOR_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_key)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation), CallbackQueryHandler(cancel_conversation, pattern="^cancel_conv$")],
        per_message=False
    )
    app.add_handler(settings_conv)

    custom_model_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_custom_model, pattern="^ask_custom_model$")],
        states={WAITING_FOR_CUSTOM_MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_custom_model)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation), CallbackQueryHandler(cancel_conversation, pattern="^cancel_conv$")],
        per_message=False
    )
    app.add_handler(custom_model_conv)

    custom_prompt_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_custom_prompt, pattern="^ask_custom_prompt$")],
        states={WAITING_FOR_CUSTOM_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_custom_prompt)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation), CallbackQueryHandler(cancel_conversation, pattern="^cancel_conv$")],
        per_message=False
    )
    app.add_handler(custom_prompt_conv)

    # --- Handlers ---
    app.add_handler(CommandHandler("start", start))
    
    app.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings_menu$"))
    app.add_handler(CallbackQueryHandler(keys_menu, pattern="^keys_menu$"))
    app.add_handler(CallbackQueryHandler(start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(delete_key, pattern="^del_key_"))
    app.add_handler(CallbackQueryHandler(reset_context_handler, pattern="^reset_context$"))
    
    app.add_handler(CallbackQueryHandler(ai_menu, pattern="^ai_menu$"))
    app.add_handler(CallbackQueryHandler(model_menu, pattern="^model_menu$"))
    app.add_handler(CallbackQueryHandler(set_model, pattern="^set_model_"))
    app.add_handler(CallbackQueryHandler(persona_menu, pattern="^persona_menu$"))
    app.add_handler(CallbackQueryHandler(set_persona, pattern="^set_persona_"))
    
    app.add_handler(CallbackQueryHandler(language_menu, pattern="^lang_menu$"))
    app.add_handler(CallbackQueryHandler(set_language_gui, pattern="^set_lang_"))

    # --- ПРІОРИТЕТНИЙ ХЕНДЛЕР: Внутрішня пересилка відео від Userbot ---
    # Ловимо відео з підписом task_id:
    app.add_handler(MessageHandler(filters.VIDEO & filters.CaptionRegex(r"^task_id:"), handle_internal_task))

    # Звичайні медіа
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE, handle_voice_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("✅ Бот запущено! Натисніть Ctrl+C для зупинки.")
    app.run_polling()

if __name__ == '__main__':
    os.makedirs("temp", exist_ok=True)
    main()