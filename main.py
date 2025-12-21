import logging
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, Application
from telegram.request import HTTPXRequest
from bot.database.session import init_db
from bot.handlers.commands import start, button_handler
# Імпортуємо новий хендлер
from bot.handlers.messages import handle_text, handle_callback, handle_voice_video
from config import TOKEN

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

    # --- ХЕНДЛЕРИ ---
    
    app.add_handler(CommandHandler("start", start))
    
    # 1. Обробка Медіа (Голосові, Відео, Кружечки)
    app.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE, 
        handle_voice_video
    ))
    
    # 2. Текстові повідомлення
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # 3. Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("✅ Бот запущено! Натисніть Ctrl+C для зупинки.")
    
    app.run_polling()

if __name__ == '__main__':
    os.makedirs("temp", exist_ok=True)
    main()