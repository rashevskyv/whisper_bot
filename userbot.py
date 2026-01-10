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

# Ланцюжки ботів (Пріоритет -> Резерв -> Резерв)
BOT_CHAINS = {
    "twitter": ["monkettbot", "SaveAsBot", "GoSeaverBot"],
    "tiktok": ["SaveAsBot", "ttsavebot", "GoSeaverBot"],
    "default": ["SaveAsBot", "GoSeaverBot"]
}

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

def get_target_bots(link: str) -> list[str]:
    """Повертає список ботів для спроби завантаження"""
    link = link.lower()
    if any(d in link for d in ["twitter.com", "x.com"]):
        return BOT_CHAINS["twitter"]
    if "tiktok.com" in link:
        return BOT_CHAINS["tiktok"]
    return BOT_CHAINS["default"]

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
                target_bots = get_target_bots(task.link)
                logger.info(f"📥 [Userbot] TAKING TASK #{task.id} -> {task.link}")
                logger.info(f"   -> Strategy: { ' -> '.join(['@'+b for b in target_bots]) }")
                
                final_status = "failed"
                
                # --- ЦИКЛ ПО БОТАХ (RETRY LOGIC) ---
                for bot_username in target_bots:
                    logger.info(f"🔄 [Userbot] Trying provider: @{bot_username}...")
                    
                    try:
                        # 1. Unblock & Send
                        try: await app.unblock_user(bot_username)
                        except: pass
                        
                        sent_msg = await app.send_message(bot_username, task.link)
                        
                        response_found = False
                        found_messages = []
                        
                        # 2. Wait Loop (30 sec per bot)
                        # Зменшив час очікування, щоб швидше переходити до резервного бота
                        for i in range(15): 
                            await asyncio.sleep(2)
                            
                            history = []
                            async for msg in app.get_chat_history(bot_username, limit=5):
                                history.append(msg)
                            
                            new_messages = [m for m in history if m.id > sent_msg.id]
                            
                            media_msgs = [
                                m for m in new_messages 
                                if m.video or m.document or m.photo or m.animation or m.audio or m.voice or m.video_note
                            ]
                            
                            if media_msgs:
                                logger.info(f"   -> Media detected from @{bot_username}! Gathering batch...")
                                await asyncio.sleep(2)
                                
                                final_history = []
                                async for msg in app.get_chat_history(bot_username, limit=8):
                                    if msg.id > sent_msg.id:
                                        if msg.video or msg.document or msg.photo or msg.animation or msg.audio or msg.voice or msg.video_note:
                                            final_history.append(msg)
                                
                                found_messages = final_history
                                break
                            
                            # Check generic errors
                            errs = [m for m in new_messages if m.text and any(x in m.text.lower() for x in ["error", "не знайшов", "not found"])]
                            if errs:
                                logger.warning(f"❌ [Userbot] @{bot_username} returned error: {errs[0].text[:50]}...")
                                # Якщо бот явно сказав "помилка", немає сенсу чекати далі - йдемо до наступного
                                break

                        # 3. Forwarding if found
                        if found_messages:
                            logger.info(f"✅ [Userbot] Success with @{bot_username}. Forwarding {len(found_messages)} files...")
                            
                            for msg in sorted(found_messages, key=lambda x: x.id):
                                try:
                                    await msg.copy(
                                        MAIN_BOT_USERNAME, 
                                        caption=f"task_id:{task.id}"
                                    )
                                    response_found = True
                                except Exception as fwd_err:
                                    logger.error(f"      -> ❌ Forward Failed: {fwd_err}")
                            
                            if response_found:
                                final_status = "done"
                                break # ВИХОДИМО З ЦИКЛУ БОТІВ, ЯКЩО ВСЕ ОК
                        else:
                            logger.warning(f"⚠️ [Userbot] Timeout or no media from @{bot_username}. Switching to next...")

                    except Exception as e:
                        logger.error(f"❌ [Userbot] Error with @{bot_username}: {e}")
                        # Continue to next bot
                
                # --- UPDATE DB ---
                async with AsyncSessionLocal() as session:
                    current_task = await session.get(DownloadQueue, task.id)
                    if current_task:
                        # Якщо жоден бот не впорався - timeout/failed
                        current_task.status = "done" if final_status == "done" else "timeout"
                        await session.commit()
                        logger.info(f"💾 [Userbot] Task {task.id} FINAL status: {current_task.status}")

            else:
                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ [Userbot] Loop Error: {e}")
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