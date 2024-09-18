import logging
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from bot.handlers import start, handle_audio, handle_video, handle_video_note, settings_menu, toggle_postprocessing, change_language, handle_text, toggle_rewriting, toggle_summarization, toggle_video_note_processing, toggle_video_processing
from bot.settings import error
from bot.settings_handler import toggle_ai
from tokens import TOKEN

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
    dispatcher.add_handler(CallbackQueryHandler(toggle_ai, pattern='toggle_ai'))  # Новий обробник
    dispatcher.add_handler(MessageHandler(Filters.text & Filters.regex('^Меню налаштувань$'), settings_menu))
    dispatcher.add_handler(MessageHandler(Filters.audio | Filters.voice, handle_audio))
    dispatcher.add_handler(MessageHandler(Filters.video, handle_video))
    dispatcher.add_handler(MessageHandler(Filters.video_note, handle_video_note))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    dispatcher.add_error_handler(error)

    logger.info("Бот запущено")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
