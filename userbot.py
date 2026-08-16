import asyncio
import logging
import os
import sys
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
DONOR_BOTS = [BOT_SAVEAS] # ЛИШАЄМО ТІЛЬКИ ОДНОГО. Fallback буде через yt-dlp.

# Логування в stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Userbot")

if os.path.exists("userbot.py"):
    os.chdir(os.path.dirname(os.path.abspath("userbot.py")))

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

def get_target_bot(link: str) -> str:
    """Визначає, кому відправити: SaveAsBot чи Monkettbot"""
    link = link.lower()
    if any(d in link for d in ["twitter.com", "x.com", "9gag.com", "bsky.app"]):
        return BOT_MONKETT
    return BOT_SAVEAS 

async def process_queue():
    logger.info(f"🚀 [Userbot] Queue Processor STARTED.")
    logger.info(f"📬 [Userbot] Forwarding to: @{MAIN_BOT_USERNAME}")
    
    if not MAIN_BOT_USERNAME:
        logger.error("❌ [Userbot] MAIN_BOT_USERNAME not set in .env!")
        return

    tick = 0

    while True:
        try:
            task = None
            async with AsyncSessionLocal() as session:
                try:
                    result = await session.execute(
                        select(DownloadQueue).where(DownloadQueue.status == "pending").limit(1)
                    )
                    task = result.scalar_one_or_none()
                    
                    if task:
                        task.status = "processing"
                        await session.commit()
                        await session.refresh(task) 
                    else:
                        await session.commit()
                        
                except Exception as db_e:
                    logger.error(f"❌ [Userbot] DB Read Error: {db_e}")
                    await asyncio.sleep(1)
                    continue

            tick += 1
            if tick % 15 == 0 and not task:
                logger.info(f"💓 [Userbot] Alive. Checking queue... (No tasks)")

            if task:
                target_bot = get_target_bot(task.link)
                logger.info(f"📥 [Userbot] TAKING TASK #{task.id} -> {task.link}")
                
                final_status = "timeout"
                error_message = None
                
                try:
                    # 1. Unblock & Send
                    try: await app.unblock_user(target_bot)
                    except: pass
                    
                    sent_msg = await app.send_message(target_bot, task.link)
                    
                    response_found = False
                    found_messages = []
                    
                    # 2. Wait Loop
                    for i in range(15): # 30 sec max
                        await asyncio.sleep(2)
                        
                        history = []
                        async for msg in app.get_chat_history(target_bot, limit=8):
                            history.append(msg)
                        
                        new_messages = [m for m in history if m.id > sent_msg.id]
                        
                        # A. Check Media (Success)
                        media_msgs = [
                            m for m in new_messages 
                            if m.video or m.document or m.photo or m.animation or m.audio or m.voice or m.video_note
                        ]
                        
                        if media_msgs:
                            logger.info(f"   -> Media detected! Gathering batch...")
                            await asyncio.sleep(2)
                            
                            final_history = []
                            async for msg in app.get_chat_history(target_bot, limit=10):
                                if msg.id > sent_msg.id and (msg.video or msg.document or msg.photo or msg.animation or msg.audio or msg.voice or msg.video_note):
                                        final_history.append(msg)
                            
                            found_messages = final_history
                            final_status = "done"
                            break

                        # B. Check Error Text (Failure/Queue)
                        err_keywords = ["error", "помилка", "не вдалося", "subscribe", "не получилось", "queue"]
                        err_msgs = [m for m in new_messages if m.text and any(x in m.text.lower() for x in err_keywords)]
                        if err_msgs:
                            error_message = err_msgs[0].text
                            final_status = "failed_by_donor" # Новий статус для обробки main.py
                            logger.warning(f"❌ [Userbot] Helper bot error response: {error_message[:50]}...")
                            break
                    
                    # 3. Forwarding (only on media success)
                    if found_messages:
                        logger.info(f"✅ [Userbot] Success. Forwarding {len(found_messages)} files...")
                        
                        for msg in sorted(found_messages, key=lambda x: x.id):
                            try:
                                await msg.copy(
                                    MAIN_BOT_USERNAME, 
                                    caption=f"task_id:{task.id}"
                                )
                                response_found = True
                            except Exception as fwd_err:
                                logger.error(f"      -> ❌ Forward Failed: {fwd_err}")

                    # 4. Update Status (Finalizing)
                    async with AsyncSessionLocal() as session:
                        current_task = await session.get(DownloadQueue, task.id)
                        if current_task:
                            current_task.status = final_status
                            # Додаємо помилку до посилання, щоб Main Bot міг її показати
                            if error_message:
                                current_task.link = f"USERBOT_ERROR:{target_bot}:{error_message[:200]}###{task.link}"
                            await session.commit()
                            logger.info(f"💾 [Userbot] Task {task.id} FINAL status: {current_task.status}")

                except Exception as e:
                    logger.error(f"❌ [Userbot] CRITICAL TASK ERROR: {e}")
                    async with AsyncSessionLocal() as session:
                        t = await session.get(DownloadQueue, task.id)
                        if t: t.status = "error"; await session.commit()
            else:
                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ [Userbot] GLOBAL LOOP ERROR: {e}")
            await asyncio.sleep(5)

@app.on_message(filters.me & filters.command("ping"))
async def ping(client, message):
    await message.edit(f"Pong!")

async def main():
    async with app:
        logger.info("✅ [Userbot] Connected to Telegram.")
        await process_queue()

if __name__ == "__main__":
    app.run(main())