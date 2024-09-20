import logging
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from bot.handlers import start, handle_message, button_handler
from bot.settings import error
from bot.settings_handler import (
    settings_menu, toggle_postprocessing, toggle_summarization, toggle_rewriting,
    change_language, toggle_video_processing, toggle_video_note_processing,
    toggle_ai, toggle_context, change_context_duration,
    text_processing_menu, media_processing_menu, ai_settings_menu,
    context_settings_menu, reset_context
)
from tokens import TOKEN
from telegram import Update

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger('httpcore').setLevel(logging.CRITICAL)
logging.getLogger('httpx').setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)

def setup_application():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.Regex('^Меню$'), settings_menu))

    # Обробники для головного меню налаштувань
    application.add_handler(CallbackQueryHandler(text_processing_menu, pattern='text_processing'))
    application.add_handler(CallbackQueryHandler(media_processing_menu, pattern='media_processing'))
    application.add_handler(CallbackQueryHandler(ai_settings_menu, pattern='ai_settings'))
    application.add_handler(CallbackQueryHandler(context_settings_menu, pattern='context_settings'))
    
    # Обробники для підменю
    application.add_handler(CallbackQueryHandler(toggle_postprocessing, pattern='toggle_postprocessing'))
    application.add_handler(CallbackQueryHandler(toggle_summarization, pattern='toggle_summarization'))
    application.add_handler(CallbackQueryHandler(toggle_rewriting, pattern='toggle_rewriting'))
    application.add_handler(CallbackQueryHandler(toggle_video_processing, pattern='toggle_video_processing'))
    application.add_handler(CallbackQueryHandler(toggle_video_note_processing, pattern='toggle_video_note_processing'))
    application.add_handler(CallbackQueryHandler(change_language, pattern='change_language'))
    application.add_handler(CallbackQueryHandler(toggle_ai, pattern='toggle_ai'))
    application.add_handler(CallbackQueryHandler(toggle_context, pattern='toggle_context'))
    application.add_handler(CallbackQueryHandler(change_context_duration, pattern='change_context_duration'))
    application.add_handler(CallbackQueryHandler(reset_context, pattern='reset_context'))
    
    # Обробник для повернення до головного меню
    application.add_handler(CallbackQueryHandler(settings_menu, pattern='back_to_main'))
    application.add_handler(CallbackQueryHandler(button_handler, pattern='^send_to_bot:'))
    
    application.add_handler(MessageHandler(filters.ALL, handle_message))

    application.add_error_handler(error)

    return application

def main() -> None:
    application = setup_application()
    logger.info("Бот запущено")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Бот зупинено")

if __name__ == '__main__':
    main()