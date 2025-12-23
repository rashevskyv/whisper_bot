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
DOWNLOADER_BOT = "SaveAsBot"
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME")

# Більш детальний формат логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Userbot")

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

async def process_queue():
    """Фонова задача"""
    logger.info(f"=== Started Queue Processor ===")
    logger.info(f"Target Bot: @{MAIN_BOT_USERNAME}")
    
    if not MAIN_BOT_USERNAME:
        logger.error("❌ MAIN_BOT_USERNAME not set in .env! Userbot cannot forward videos.")
        return

    while True:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(DownloadQueue).where(DownloadQueue.status == "pending").limit(1)
                )
                task = result.scalar_one_or_none()

                if task:
                    logger.info(f"📌 Found task {task.id} for user {task.user_id}: {task.link}")
                    task.status = "processing"
                    await session.commit()
                    
                    try:
                        # 1. Send link to SaveAsBot
                        logger.info(f"sending link to {DOWNLOADER_BOT}...")
                        sent_msg = await app.send_message(DOWNLOADER_BOT, task.link)
                        
                        response_received = False
                        
                        # 2. Wait for response
                        logger.info("Waiting for response...")
                        for i in range(15): # 30 seconds wait
                            await asyncio.sleep(2)
                            
                            history = []
                            async for msg in app.get_chat_history(DOWNLOADER_BOT, limit=3):
                                history.append(msg)
                            
                            for msg in history:
                                # Check if message is newer than our request
                                if msg.id > sent_msg.id:
                                    logger.info(f"Received msg ID {msg.id}. Type: {msg.media}")
                                    
                                    if msg.video or msg.document or msg.photo:
                                        logger.info(f"✅ Media found! Forwarding to @{MAIN_BOT_USERNAME}")
                                        
                                        try:
                                            await msg.copy(
                                                MAIN_BOT_USERNAME, 
                                                caption=f"task_id:{task.id}"
                                            )
                                            logger.info("Forwarded successfully.")
                                            response_received = True
                                        except Exception as fwd_err:
                                            logger.error(f"❌ Forward error: {fwd_err}")
                                        
                                        break
                                        
                                    elif "error" in (msg.text or "").lower():
                                        logger.warning(f"Bot returned error: {msg.text}")
                                        response_received = True 
                                        break
                            
                            if response_received:
                                break
                        
                        if not response_received:
                            logger.warning("⏱ Timeout: SaveAsBot did not reply in time.")
                            task.status = "timeout"
                        else:
                            task.status = "done"

                    except Exception as e:
                        logger.error(f"Task processing error: {e}")
                        task.status = "error"
                    
                    await session.commit()

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Global loop error: {e}")
            await asyncio.sleep(5)

@app.on_message(filters.me & filters.command("ping"))
async def ping(client, message):
    await message.edit(f"Pong! Forwarding to: @{MAIN_BOT_USERNAME}")

async def main():
    async with app:
        logger.info("Userbot client connected.")
        await process_queue()

if __name__ == "__main__":
    app.run(main())