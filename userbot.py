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

BOT_SAVEAS = "SaveAsBot"
BOT_MONKETT = "monkettbot"

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

    # Лічильник для heartbeat логів
    tick = 0

    while True:
        try:
            task = None
            
            # ВІДКРИВАЄМО НОВУ СЕСІЮ ДЛЯ КОЖНОЇ ПЕРЕВІРКИ
            async with AsyncSessionLocal() as session:
                try:
                    result = await session.execute(
                        select(DownloadQueue).where(DownloadQueue.status == "pending").limit(1)
                    )
                    task = result.scalar_one_or_none()
                    
                    if task:
                        # Якщо знайшли задачу - блокуємо її
                        task.status = "processing"
                        await session.commit()
                        # Оновлюємо об'єкт, щоб мати доступ до полів поза сесією
                        await session.refresh(task) 
                    else:
                        # ВАЖЛИВО: Навіть якщо нічого не знайшли, робимо commit,
                        # щоб завершити транзакцію і оновити snapshot бази (для WAL режиму)
                        await session.commit()
                        
                except Exception as db_e:
                    logger.error(f"❌ [Userbot] DB Read Error: {db_e}")
                    await asyncio.sleep(1)
                    continue

            # Лог "пульсу" кожні ~30 секунд (15 циклів по 2 сек), щоб бачити що бот живий
            tick += 1
            if tick % 15 == 0 and not task:
                logger.info(f"💓 [Userbot] Alive. Checking queue... (No tasks)")

            if task:
                target_bot = get_target_bot(task.link)
                logger.info(f"📥 [Userbot] TAKING TASK #{task.id} -> {task.link}")
                
                try:
                    # 1. Unblock & Send
                    try: await app.unblock_user(target_bot)
                    except: pass
                    
                    logger.info(f"📤 [Userbot] Sending to @{target_bot}...")
                    sent_msg = await app.send_message(target_bot, task.link)
                    
                    response_found = False
                    found_messages = []
                    
                    # 2. Wait Loop
                    for i in range(25): # 50 sec max
                        await asyncio.sleep(2)
                        
                        history = []
                        async for msg in app.get_chat_history(target_bot, limit=5):
                            history.append(msg)
                        
                        new_messages = [m for m in history if m.id > sent_msg.id]
                        
                        media_msgs = [
                            m for m in new_messages 
                            if m.video or m.document or m.photo or m.animation or m.audio or m.voice or m.video_note
                        ]
                        
                        if media_msgs:
                            logger.info(f"   -> Detected media! Waiting 2s for batch...")
                            await asyncio.sleep(2)
                            
                            final_history = []
                            async for msg in app.get_chat_history(target_bot, limit=8):
                                if msg.id > sent_msg.id:
                                    if msg.video or msg.document or msg.photo or msg.animation or msg.audio or msg.voice or msg.video_note:
                                        final_history.append(msg)
                            
                            found_messages = final_history
                            break
                        
                        errs = [m for m in new_messages if m.text and "error" in m.text.lower()]
                        if errs:
                            logger.warning(f"❌ [Userbot] Bot error: {errs[0].text}")
                            response_found = True
                            break

                    # 3. Forwarding
                    if found_messages:
                        logger.info(f"✅ [Userbot] Found {len(found_messages)} files. Forwarding...")
                        
                        for msg in sorted(found_messages, key=lambda x: x.id):
                            try:
                                await msg.copy(
                                    MAIN_BOT_USERNAME, 
                                    caption=f"task_id:{task.id}"
                                )
                                response_found = True
                                logger.info(f"      -> Sent MsgID {msg.id}")
                            except Exception as fwd_err:
                                logger.error(f"      -> ❌ Forward Failed: {fwd_err}")
                    
                    # 4. Update Status
                    async with AsyncSessionLocal() as session:
                        current_task = await session.get(DownloadQueue, task.id)
                        if current_task:
                            current_task.status = "done" if response_found else "timeout"
                            await session.commit()
                            logger.info(f"💾 [Userbot] Task {task.id} finished as: {current_task.status}")

                except Exception as e:
                    logger.error(f"❌ [Userbot] Task Processing Error: {e}")
                    async with AsyncSessionLocal() as session:
                        t = await session.get(DownloadQueue, task.id)
                        if t:
                            t.status = "error"
                            await session.commit()
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