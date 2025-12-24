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

# Константи ботів
BOT_SAVEAS = "SaveAsBot"
BOT_MONKETT = "monkettbot"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Userbot")

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

def get_target_bot(link: str) -> str:
    """Визначає, якому боту відправити посилання"""
    link = link.lower()
    if any(d in link for d in ["twitter.com", "x.com", "9gag.com", "bsky.app"]):
        return BOT_MONKETT
    return BOT_SAVEAS # За замовчуванням (TikTok, Insta, Pinterest)

async def process_queue():
    """Фонова задача"""
    logger.info(f"=== Started Queue Processor ===")
    logger.info(f"Target Bot: @{MAIN_BOT_USERNAME}")
    
    if not MAIN_BOT_USERNAME:
        logger.error("❌ MAIN_BOT_USERNAME not set in .env!")
        return

    while True:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(DownloadQueue).where(DownloadQueue.status == "pending").limit(1)
                )
                task = result.scalar_one_or_none()

                if task:
                    target_bot = get_target_bot(task.link)
                    logger.info(f"📌 Task {task.id}: {task.link} -> {target_bot}")
                    
                    task.status = "processing"
                    await session.commit()
                    
                    try:
                        # 1. Send link
                        sent_msg = await app.send_message(target_bot, task.link)
                        response_received = False
                        
                        # 2. Wait
                        for i in range(15):
                            await asyncio.sleep(2)
                            history = []
                            async for msg in app.get_chat_history(target_bot, limit=3):
                                history.append(msg)
                            
                            for msg in history:
                                if msg.id > sent_msg.id:
                                    if msg.video or msg.document or msg.photo:
                                        logger.info(f"✅ Media found! Forwarding...")
                                        try:
                                            await msg.copy(
                                                MAIN_BOT_USERNAME, 
                                                caption=f"task_id:{task.id}"
                                            )
                                            response_received = True
                                        except Exception as fwd_err:
                                            logger.error(f"Forward error: {fwd_err}")
                                        break
                                    elif "error" in (msg.text or "").lower():
                                        logger.warning(f"Bot returned error: {msg.text}")
                                        response_received = True 
                                        break
                            if response_received: break
                        
                        if not response_received:
                            task.status = "timeout"
                        else:
                            task.status = "done"

                    except Exception as e:
                        logger.error(f"Task error: {e}")
                        task.status = "error"
                    
                    await session.commit()

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Loop error: {e}")
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