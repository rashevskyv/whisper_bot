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

if os.path.exists("userbot.py"):
    os.chdir(os.path.dirname(os.path.abspath("userbot.py")))

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

def get_target_bot(link: str) -> str:
    """Визначає, якому боту відправити посилання"""
    link = link.lower()
    if any(d in link for d in ["twitter.com", "x.com", "9gag.com", "bsky.app"]):
        return BOT_MONKETT
    return BOT_SAVEAS 

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
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(DownloadQueue).where(DownloadQueue.status == "pending").limit(1)
                )
                task = result.scalar_one_or_none()
                
                if task:
                    task.status = "processing"
                    await session.commit()

            if task:
                target_bot = get_target_bot(task.link)
                logger.info(f"📌 Processing Task {task.id}: {task.link} via {target_bot}")
                
                try:
                    # 1. Відправляємо посилання
                    try: await app.unblock_user(target_bot)
                    except: pass
                    
                    sent_msg = await app.send_message(target_bot, task.link)
                    response_found = False
                    
                    # 2. Чекаємо на відповідь (цикл очікування)
                    # Чекаємо довше, щоб бот встиг вислати ВСІ файли (відео + аудіо)
                    found_messages = []
                    
                    for i in range(20): # 40 секунд макс
                        await asyncio.sleep(2)
                        
                        # Отримуємо історію
                        history = []
                        async for msg in app.get_chat_history(target_bot, limit=5):
                            history.append(msg)
                        
                        # Фільтруємо повідомлення, що прийшли ПІСЛЯ нашого запиту
                        new_messages = [m for m in history if m.id > sent_msg.id]
                        
                        # Шукаємо серед них медіа
                        media_messages = [
                            m for m in new_messages 
                            if m.video or m.document or m.photo or m.animation or m.audio
                        ]
                        
                        if media_messages:
                            # Якщо знайшли медіа, чекаємо ще трохи (2 сек), щоб переконатися, що це все
                            # SaveAsBot іноді шле Відео, а через секунду Аудіо.
                            await asyncio.sleep(2)
                            
                            # Робимо повторний запит історії, щоб забрати догружене
                            final_history = []
                            async for msg in app.get_chat_history(target_bot, limit=6):
                                if msg.id > sent_msg.id and (msg.video or msg.document or msg.photo or msg.animation or msg.audio):
                                    final_history.append(msg)
                            
                            found_messages = final_history
                            break
                        
                        # Перевірка на помилку текстом
                        error_msgs = [m for m in new_messages if m.text and "error" in m.text.lower()]
                        if error_msgs:
                            logger.warning(f"Bot returned error: {error_msgs[0].text}")
                            response_found = True # Вважаємо це відповіддю, щоб закрити задачу
                            break

                    # 3. Обробка знайдених повідомлень
                    if found_messages:
                        logger.info(f"✅ Found {len(found_messages)} media files. Forwarding all...")
                        # Сортуємо від старого до нового (щоб відео йшло перед аудіо, зазвичай)
                        for msg in sorted(found_messages, key=lambda x: x.id):
                            try:
                                await msg.copy(
                                    MAIN_BOT_USERNAME, 
                                    caption=f"task_id:{task.id}"
                                )
                                response_found = True
                            except Exception as fwd_err:
                                logger.error(f"Forward failed: {fwd_err}")
                    
                    # 4. Оновлення статусу
                    async with AsyncSessionLocal() as session:
                        current_task = await session.get(DownloadQueue, task.id)
                        if current_task:
                            current_task.status = "done" if response_found else "timeout"
                            await session.commit()
                            
                    if not response_found:
                        logger.warning(f"⚠️ Timeout: No media received from {target_bot}")

                except Exception as e:
                    logger.error(f"Task Execution Error: {e}")
                    async with AsyncSessionLocal() as session:
                        current_task = await session.get(DownloadQueue, task.id)
                        if current_task:
                            current_task.status = "error"
                            await session.commit()

            else:
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Global Loop Error: {e}")
            await asyncio.sleep(5)

@app.on_message(filters.me & filters.command("ping"))
async def ping(client, message):
    await message.edit(f"Pong! Helper bots: {BOT_SAVEAS}, {BOT_MONKETT}")

async def main():
    async with app:
        logger.info("Userbot connected.")
        await process_queue()

if __name__ == "__main__":
    app.run(main())