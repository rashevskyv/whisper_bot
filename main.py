import logging
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from bot.handlers import start, handle_message, settings_menu
from bot.settings import error
from bot.settings_handler import toggle_postprocessing, toggle_summarization, toggle_rewriting, change_language, toggle_video_processing, toggle_video_note_processing, toggle_ai
from tokens import TOKEN

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

def main() -> None:
    updater = Updater(TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CallbackQueryHandler(settings_menu, pattern='settings'))
    dispatcher.add_handler(CallbackQueryHandler(toggle_postprocessing, pattern='toggle_postprocessing'))
    dispatcher.add_handler(CallbackQueryHandler(toggle_summarization, pattern='toggle_summarization'))
    dispatcher.add_handler(CallbackQueryHandler(toggle_rewriting, pattern='toggle_rewriting'))
    dispatcher.add_handler(CallbackQueryHandler(toggle_video_processing, pattern='toggle_video_processing'))
    dispatcher.add_handler(CallbackQueryHandler(toggle_video_note_processing, pattern='toggle_video_note_processing'))
    dispatcher.add_handler(CallbackQueryHandler(change_language, pattern='change_language'))
    dispatcher.add_handler(CallbackQueryHandler(toggle_ai, pattern='toggle_ai'))
    dispatcher.add_handler(MessageHandler(Filters.regex('^Меню налаштувань$'), settings_menu))
    dispatcher.add_handler(MessageHandler(Filters.all, handle_message))

    dispatcher.add_error_handler(error)

    logger.info("Бот запущено")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
