import logging
import os
import warnings
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, Application, ConversationHandler
from telegram.request import HTTPXRequest
from telegram.warnings import PTBUserWarning

from bot.database.session import init_db
from bot.handlers.commands import start
from bot.utils.scheduler import scheduler_service

# Handlers
from bot.handlers.text import handle_text, handle_internal_task
from bot.handlers.media import handle_voice_video, handle_photo
from bot.handlers.callbacks import handle_callback

# Settings Handlers
from bot.handlers.settings import (
    settings_menu, keys_menu, ask_for_key, save_key, delete_key, 
    reset_context_handler, cancel_conversation, close_menu,
    model_menu, set_model, ask_custom_model, save_custom_model,
    persona_menu, set_persona, ask_custom_prompt, save_custom_prompt,
    language_menu, set_language_gui, 
    transcription_menu, set_transcription_model,
    timezone_menu, set_timezone_btn, ask_custom_timezone, save_custom_timezone,
    WAITING_FOR_KEY, WAITING_FOR_CUSTOM_MODEL, WAITING_FOR_CUSTOM_PROMPT, WAITING_FOR_TIMEZONE
)
from config import TOKEN

warnings.filterwarnings("ignore", category=PTBUserWarning)

# Логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

async def post_init(application: Application):
    await init_db()
    print("📦 [MainBot] DB initialized (WAL mode).")
    scheduler_service.start(application)
    await scheduler_service.restore_reminders()
    print("⏰ [MainBot] Scheduler started.")

def main():
    if not TOKEN:
        print("❌ Помилка: Не задано BOT_TOKEN в .env!")
        return

    req = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=120.0, 
        read_timeout=120.0,
        write_timeout=120.0
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

    timezone_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_custom_timezone, pattern="^ask_custom_tz$")],
        states={WAITING_FOR_TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_custom_timezone)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation), CallbackQueryHandler(cancel_conversation, pattern="^cancel_conv$")],
        per_message=False
    )
    app.add_handler(timezone_conv)

    app.add_handler(CommandHandler("start", start))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings_menu$"))
    app.add_handler(CallbackQueryHandler(close_menu, pattern="^close_menu$"))
    app.add_handler(CallbackQueryHandler(keys_menu, pattern="^keys_menu$"))
    app.add_handler(CallbackQueryHandler(start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(delete_key, pattern="^del_key_"))
    app.add_handler(CallbackQueryHandler(reset_context_handler, pattern="^reset_context$"))
    app.add_handler(CallbackQueryHandler(model_menu, pattern="^model_menu$"))
    app.add_handler(CallbackQueryHandler(set_model, pattern="^set_model_"))
    app.add_handler(CallbackQueryHandler(persona_menu, pattern="^persona_menu$"))
    app.add_handler(CallbackQueryHandler(set_persona, pattern="^set_persona_"))
    app.add_handler(CallbackQueryHandler(language_menu, pattern="^lang_menu$"))
    app.add_handler(CallbackQueryHandler(set_language_gui, pattern="^set_lang_"))
    app.add_handler(CallbackQueryHandler(timezone_menu, pattern="^timezone_menu$"))
    app.add_handler(CallbackQueryHandler(set_timezone_btn, pattern="^set_tz_"))
    app.add_handler(CallbackQueryHandler(transcription_menu, pattern="^transcription_menu$"))
    app.add_handler(CallbackQueryHandler(set_transcription_model, pattern="^set_trans_"))

    # --- Handlers ---
    # Internal Task (Userbot) - Приймає будь-яке медіа з підписом task_id
    app.add_handler(MessageHandler(
        filters.CaptionRegex(r"^task_id:"), 
        handle_internal_task
    ))

    # Media
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE, handle_voice_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Generic Callback
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("✅ [MainBot] Started polling.")
    app.run_polling()
    
if __name__ == '__main__':
    os.makedirs("temp", exist_ok=True)
    main()