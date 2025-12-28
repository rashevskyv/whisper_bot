import asyncio
import logging
import os
from pyrogram import Client, filters
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import DownloadQueue
from dotenv import load_dotenv

load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = "my_userbot"
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME")

# Константи ботів-помічників
BOT_SAVEAS = "SaveAsBot"
BOT_MONKETT = "monkettbot"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Userbot")

# Встановлюємо робочу директорію явно, щоб файл сесії створювався там де треба
if os.path.exists("userbot.py"):
    os.chdir(os.path.dirname(os.path.abspath("userbot.py")))

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

def get_target_bot(link: str) -> str:
    """Визначає, якому боту відправити посилання"""
    link = link.lower()
    if any(d in link for d in ["twitter.com", "x.com", "9gag.com", "bsky.app"]):
        return BOT_MONKETT
    return BOT_SAVEAS # TikTok, Insta, Pinterest

async def process_queue():
    """Фонова задача обробки черги"""
    logger.info(f"=== Started Queue Processor ===")
    logger.info(f"Forwarding results to: @{MAIN_BOT_USERNAME}")
    
    if not MAIN_BOT_USERNAME:
        logger.error("❌ MAIN_BOT_USERNAME not set in .env!")
        return

    while True:
        try:
            task = None
            # Використовуємо окрему сесію для читання, щоб не тримати лок
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(DownloadQueue).where(DownloadQueue.status == "pending").limit(1)
                )
                task = result.scalar_one_or_none()
                
                if task:
                    # Одразу помічаємо як в обробці
                    task.status = "processing"
                    await session.commit()

            if task:
                target_bot = get_target_bot(task.link)
                logger.info(f"📌 Processing Task {task.id}: {task.link} via {target_bot}")
                
                try:
                    # 1. Відправляємо посилання боту-помічнику
                    # unblock на випадок, якщо бот був заблокований
                    try: await app.unblock_user(target_bot)
                    except: pass
                    
                    sent_msg = await app.send_message(target_bot, task.link)
                    response_found = False
                    
                    # 2. Чекаємо на відповідь (до 30 ітерацій по 2 сек = 60 сек)
                    for i in range(30):
                        await asyncio.sleep(2)
                        
                        # Перевіряємо останні 5 повідомлень від бота
                        found_media = False
                        async for msg in app.get_chat_history(target_bot, limit=5):
                            # Шукаємо повідомлення, яке прийшло ПІСЛЯ нашого запиту
                            if msg.id > sent_msg.id:
                                # Ігноруємо текстові "Зачекайте...", шукаємо медіа
                                if msg.video or msg.document or msg.photo or msg.animation or msg.audio:
                                    logger.info(f"✅ Media found inside history! Forwarding to main bot...")
                                    try:
                                        # Копіюємо медіа основному боту з ID задачі
                                        await msg.copy(
                                            MAIN_BOT_USERNAME, 
                                            caption=f"task_id:{task.id}"
                                        )
                                        response_found = True
                                        found_media = True
                                    except Exception as fwd_err:
                                        logger.error(f"Forward to main bot failed: {fwd_err}")
                                    break
                                elif msg.text and "error" in msg.text.lower():
                                    logger.warning(f"Bot returned error text: {msg.text}")
                                    response_found = True # Це теж відповідь, хоч і помилка
                                    found_media = True
                                    break
                        
                        if found_media:
                            break
                    
                    # Оновлюємо статус в БД
                    async with AsyncSessionLocal() as session:
                        current_task = await session.get(DownloadQueue, task.id)
                        if current_task:
                            current_task.status = "done" if response_found else "timeout"
                            await session.commit()
                            
                    if not response_found:
                        logger.warning(f"⚠️ Timeout waiting for {target_bot}")

                except Exception as e:
                    logger.error(f"Task Execution Error: {e}")
                    async with AsyncSessionLocal() as session:
                        current_task = await session.get(DownloadQueue, task.id)
                        if current_task:
                            current_task.status = "error"
                            await session.commit()

            else:
                # Якщо задач немає, спимо довше
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Global Loop Error: {e}")
            await asyncio.sleep(5)

@app.on_message(filters.me & filters.command("ping"))
async def ping(client, message):
    await message.edit(f"Pong! Connected to {BOT_SAVEAS} & {BOT_MONKETT}")

async def main():
    async with app:
        logger.info("Userbot connected and listening...")
        await process_queue()

if __name__ == "__main__":
    app.run(main())