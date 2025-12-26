import logging
import zoneinfo
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import Reminder
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

class SchedulerService:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self.bot_app = None

    def start(self, app):
        """Initializes scheduler and restores tasks from DB"""
        self.bot_app = app
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("🕒 Scheduler started (UTC).")

    async def restore_reminders(self):
        """Loads pending reminders from DB on startup"""
        logger.info("🔄 Restoring reminders from DB...")
        async with AsyncSessionLocal() as session:
            stmt = select(Reminder)
            result = await session.execute(stmt)
            reminders = result.scalars().all()

            count = 0
            deleted_count = 0
            now = datetime.now(timezone.utc)
            
            for rem in reminders:
                trigger_time = rem.trigger_time
                if trigger_time.tzinfo is None:
                    trigger_time = trigger_time.replace(tzinfo=timezone.utc)
                
                # STRICT check: If time has passed, delete it immediately.
                if trigger_time > now:
                    self._schedule_job(rem.id, rem.chat_id, rem.text, trigger_time)
                    count += 1
                else:
                    await session.delete(rem)
                    deleted_count += 1
            
            await session.commit()
            if deleted_count > 0:
                logger.info(f"🗑 Deleted {deleted_count} expired reminders.")
            logger.info(f"✅ Restored {count} active reminders.")

    def _schedule_job(self, reminder_id: int, chat_id: int, text: str, run_date: datetime):
        """Internal method to add job to APScheduler"""
        if run_date.tzinfo is None:
            run_date = run_date.replace(tzinfo=timezone.utc)
            
        self.scheduler.add_job(
            self.send_reminder,
            trigger=DateTrigger(run_date=run_date, timezone=timezone.utc),
            args=[chat_id, text, reminder_id],
            id=str(reminder_id),
            replace_existing=True,
            misfire_grace_time=60 
        )
        
        # LOGGING CONVERSION FOR READABILITY
        try:
            local_tz = zoneinfo.ZoneInfo(BOT_TIMEZONE)
            local_time = run_date.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S")
            tz_label = BOT_TIMEZONE
        except:
            local_time = run_date.strftime("%Y-%m-%d %H:%M:%S")
            tz_label = "UTC"

        logger.info(f"📌 JOB SET: ID={reminder_id} | Chat={chat_id} | 🕒 Run At: {local_time} ({tz_label})")

    async def add_reminder(self, user_id: int, chat_id: int, text: str, trigger_time: datetime) -> int:
        """Saves to DB and schedules in memory"""
        if trigger_time.tzinfo is None:
            trigger_time = trigger_time.replace(tzinfo=timezone.utc)
        else:
            trigger_time = trigger_time.astimezone(timezone.utc)

        async with AsyncSessionLocal() as session:
            new_reminder = Reminder(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
                trigger_time=trigger_time
            )
            session.add(new_reminder)
            await session.commit()
            await session.refresh(new_reminder)
            reminder_id = new_reminder.id

        self._schedule_job(reminder_id, chat_id, text, trigger_time)
        return reminder_id

    async def send_reminder(self, chat_id: int, text: str, reminder_id: int):
        """Callback function triggered by scheduler"""
        logger.info(f"🔔 FIRING REMINDER #{reminder_id} for chat {chat_id}")
        
        if not self.bot_app:
            logger.error("❌ Bot App not initialized in Scheduler!")
            return

        try:
            await self.bot_app.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ <b>НАГАДУВАННЯ:</b>\n\n{text}",
                parse_mode="HTML"
            )
            
            async with AsyncSessionLocal() as session:
                rem = await session.get(Reminder, reminder_id)
                if rem:
                    await session.delete(rem)
                    await session.commit()
            logger.info(f"✅ Reminder #{reminder_id} sent and deleted from DB.")
                    
        except Exception as e:
            logger.error(f"❌ Failed to send reminder #{reminder_id}: {e}")

    async def get_active_reminders(self, chat_id: int):
        """Returns a list of active reminders for a user."""
        async with AsyncSessionLocal() as session:
            stmt = select(Reminder).where(Reminder.chat_id == chat_id).order_by(Reminder.trigger_time)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_reminders_count(self, chat_id: int) -> int:
        """Returns the count of active reminders."""
        async with AsyncSessionLocal() as session:
            # Efficient count
            from sqlalchemy import func
            stmt = select(func.count()).select_from(Reminder).where(Reminder.chat_id == chat_id)
            result = await session.execute(stmt)
            return result.scalar()

    async def delete_reminder_by_id(self, reminder_id: int):
        """Removes reminder from DB and Scheduler."""
        # 1. Remove from APScheduler
        try:
            self.scheduler.remove_job(str(reminder_id))
            logger.info(f"🗑 Job {reminder_id} removed from scheduler.")
        except Exception:
            pass # Job might be missing if server restarted or it just finished

        # 2. Remove from DB
        async with AsyncSessionLocal() as session:
            rem = await session.get(Reminder, reminder_id)
            if rem:
                await session.delete(rem)
                await session.commit()
                logger.info(f"🗑 Reminder {reminder_id} deleted from DB.")

# Global Instance
scheduler_service = SchedulerService()