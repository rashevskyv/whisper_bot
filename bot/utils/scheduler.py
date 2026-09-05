import html
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import zoneinfo
from zoneinfo import ZoneInfo
from typing import Optional
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.future import select
from bot.database.session import AsyncSessionLocal
from bot.database.models import Reminder, ScheduledTask, TaskOccurrence
from bot.utils.scheduled_tasks import (
    get_scheduled_task,
    list_active_scheduled_tasks,
    get_or_create_task_occurrence,
    claim_task_occurrence_for_delivery,
    complete_task_occurrence_delivery,
    revert_task_occurrence_delivery,
    mark_task_occurrence_missed,
    get_latest_task_occurrence_planned_at,
    list_pending_task_occurrences,
    CONTEXT_TYPE_MEDICATION,
    OCCURRENCE_STATUS_SCHEDULED,
    OCCURRENCE_STATUS_SNOOZED,
    _ensure_utc,
)
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)


def build_occurrence_inline_keyboard(occurrence_id: int, context_type: str) -> InlineKeyboardMarkup:
    """Builds a compact inline keyboard for delivered recurring-task occurrence."""
    if context_type == CONTEXT_TYPE_MEDICATION:
        done_text = "✅ Прийняв"
    else:
        done_text = "✅ Виконав"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(done_text, callback_data=f"occ:done:{occurrence_id}")],
        [
            InlineKeyboardButton("⏰ +15 хв", callback_data=f"occ:s15:{occurrence_id}"),
            InlineKeyboardButton("⏰ +30 хв", callback_data=f"occ:s30:{occurrence_id}"),
        ],
        [InlineKeyboardButton("⏭ Пропустив", callback_data=f"occ:skip:{occurrence_id}")],
    ])


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
            missed_count = 0
            now = datetime.now(timezone.utc)
            
            for rem in reminders:
                trigger_time = rem.trigger_time
                if trigger_time.tzinfo is None:
                    trigger_time = trigger_time.replace(tzinfo=timezone.utc)
                
                if trigger_time > now:
                    self._schedule_job(rem.id, rem.chat_id, rem.text, trigger_time)
                    count += 1
                else:
                    missed_count += 1
                    try:
                        if self.bot_app:
                            await self.bot_app.bot.send_message(
                                chat_id=rem.chat_id,
                                text=f"⚠️ <b>Пропущене нагадування!</b>\n⏰ <i>{trigger_time.strftime('%d.%m %H:%M UTC')}</i>\n📝 {rem.text}",
                                parse_mode="HTML"
                            )
                    except Exception:
                        logger.error(f"❌ Could not send missed alert for ID={rem.id}")
                    
                    await session.delete(rem)
            
            await session.commit()
            if missed_count > 0: logger.info(f"🔔 Processed {missed_count} missed reminders.")
            logger.info(f"✅ Restored {count} active reminders.")

    def _schedule_job(self, reminder_id: int, chat_id: int, text: str, run_date: datetime):
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
        
        try:
            local_tz = zoneinfo.ZoneInfo(BOT_TIMEZONE)
            local_time = run_date.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S")
        except:
            local_time = run_date.strftime("%Y-%m-%d %H:%M:%S UTC")

        logger.info(f"📌 JOB SET: ID={reminder_id} | Chat={chat_id} | 🕒 Run At: {local_time}")

    async def add_reminder(self, user_id: int, chat_id: int, text: str, trigger_time: datetime) -> int:
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
            logger.error(f"❌ Failed to send reminder #{reminder_id}: bot_app is None")
            return
            
        if not chat_id:
             logger.error(f"❌ Failed to send reminder #{reminder_id}: Chat_id is empty/None. Data: text='{text}'")
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
            logger.info(f"✅ Reminder #{reminder_id} delivered and DB cleared.")
                    
        except Exception:
            logger.error(f"❌ Failed to send reminder #{reminder_id}")

    async def get_active_reminders(self, chat_id: int):
        async with AsyncSessionLocal() as session:
            stmt = select(Reminder).where(Reminder.chat_id == chat_id).order_by(Reminder.trigger_time)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_reminders_count(self, chat_id: int) -> int:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func
            stmt = select(func.count()).select_from(Reminder).where(Reminder.chat_id == chat_id)
            result = await session.execute(stmt)
            return result.scalar()

    async def delete_reminder_by_id(self, reminder_id: int, chat_id: Optional[int] = None) -> bool:
        async with AsyncSessionLocal() as session:
            rem = await session.get(Reminder, reminder_id)
            if not rem:
                return False
            if chat_id is not None and rem.chat_id != chat_id:
                return False
            await session.delete(rem)
            await session.commit()
            logger.info(f"🗑 Reminder {reminder_id} deleted from DB.")

        try:
            self.scheduler.remove_job(str(reminder_id))
            logger.info(f"🗑 Job {reminder_id} removed from scheduler.")
        except Exception:
            pass

        return True

    async def get_active_reminders_string(self, chat_id: int, timezone_str: str) -> str:
        rems = await self.get_active_reminders(chat_id)
        if not rems: return "No active reminders."
        try: local_tz = zoneinfo.ZoneInfo(timezone_str)
        except: local_tz = zoneinfo.ZoneInfo("UTC")
        result = ""
        for r in rems:
            t = r.trigger_time.replace(tzinfo=zoneinfo.ZoneInfo("UTC")) if r.trigger_time.tzinfo is None else r.trigger_time
            local_t = t.astimezone(local_tz).strftime("%Y-%m-%d %H:%M")
            result += f"- ID: {r.id} | Time: {local_t} | Text: '{r.text}'\n"
        return result

    def schedule_recurring_task(self, task: ScheduledTask):
        """Registers or replaces a CronTrigger job for an active ScheduledTask."""
        if not task.active:
            self.unschedule_recurring_task(task.id)
            return

        hour, minute = map(int, task.local_time.split(":"))
        days_str = ",".join(str(d) for d in task.days_of_week)
        tz = ZoneInfo(task.timezone)
        job_id = f"scheduled_task:{task.id}"

        trigger = CronTrigger(
            hour=hour,
            minute=minute,
            day_of_week=days_str,
            timezone=tz,
        )

        self.scheduler.add_job(
            self._fire_scheduled_task,
            trigger=trigger,
            args=[task.id, task.user_id, task.chat_id],
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        logger.info(f"Registered recurring scheduled task job {job_id}")

    def unschedule_recurring_task(self, task_id: int):
        """Removes the recurring cron job for a task if present."""
        job_id = f"scheduled_task:{task_id}"
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed recurring scheduled task job {job_id}")
        except Exception:
            pass

    def schedule_task_occurrence(self, occurrence: TaskOccurrence):
        """Registers or replaces a DateTrigger delivery job for a concrete occurrence."""
        due_at = _ensure_utc(occurrence.due_at)
        job_id = f"task_occurrence:{occurrence.id}"
        self.scheduler.add_job(
            self._deliver_task_occurrence,
            trigger=DateTrigger(run_date=due_at, timezone=timezone.utc),
            args=[occurrence.id],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        logger.info(f"Registered task occurrence delivery job {job_id}")

    def unschedule_task_occurrence(self, occurrence_id: int):
        """Removes the DateTrigger job for an occurrence if present."""
        job_id = f"task_occurrence:{occurrence_id}"
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed task occurrence job {job_id}")
        except Exception:
            pass

    def _derive_planned_at(self, task: ScheduledTask, fire_time: Optional[datetime] = None) -> datetime:
        """Derives the scheduled local minute represented by the fire and normalizes to aware UTC."""
        if fire_time is None:
            fire_time = datetime.now(timezone.utc)
        else:
            fire_time = _ensure_utc(fire_time)

        tz = ZoneInfo(task.timezone)
        local_fire = fire_time.astimezone(tz)
        hour, minute = map(int, task.local_time.split(":"))

        base_date = local_fire.date()
        candidates = []
        for offset in (-1, 0, 1):
            cand_date = base_date + timedelta(days=offset)
            if cand_date.weekday() in task.days_of_week:
                cand_fold = local_fire.fold if offset == 0 else 0
                cand_dt = datetime(
                    cand_date.year, cand_date.month, cand_date.day,
                    hour, minute, tzinfo=tz, fold=cand_fold
                )
                diff = abs((cand_dt - local_fire).total_seconds())
                candidates.append((diff, cand_dt))

        if not candidates:
            cand_dt = datetime(
                base_date.year, base_date.month, base_date.day,
                hour, minute, tzinfo=tz, fold=local_fire.fold
            )
            return cand_dt.astimezone(timezone.utc).replace(second=0, microsecond=0)

        candidates.sort(key=lambda x: x[0])
        chosen = candidates[0][1]
        return chosen.astimezone(timezone.utc).replace(second=0, microsecond=0)

    async def _fire_scheduled_task(self, task_id: int, user_id: int, chat_id: int, fire_time: Optional[datetime] = None):
        """Recurring cron job callback. Generates or reuses occurrence and schedules DateTrigger delivery."""
        task = await get_scheduled_task(task_id, user_id, chat_id)
        if not task or not task.active:
            return

        planned_at_utc = self._derive_planned_at(task, fire_time)
        occ, _ = await get_or_create_task_occurrence(
            task_id=task.id,
            user_id=task.user_id,
            chat_id=task.chat_id,
            planned_at=planned_at_utc,
        )
        if occ is not None and occ.status in (OCCURRENCE_STATUS_SCHEDULED, OCCURRENCE_STATUS_SNOOZED):
            self.schedule_task_occurrence(occ)

    async def _deliver_task_occurrence(self, occurrence_id: int):
        """Occurrence DateTrigger callback. Atomically claims, sends Telegram message, and updates DB."""
        if not self.bot_app:
            logger.error(f"Cannot deliver occurrence_id={occurrence_id}: bot_app is None")
            return

        claim_res = await claim_task_occurrence_for_delivery(occurrence_id)
        if not claim_res or claim_res[0] is None:
            return

        occ, task, previous_status = claim_res

        escaped_name = html.escape(task.name)
        local_tz = ZoneInfo(task.timezone)
        local_planned = occ.planned_at.astimezone(local_tz)
        hh_mm = local_planned.strftime("%H:%M")

        if task.context_type == CONTEXT_TYPE_MEDICATION:
            escaped_dosage = html.escape(task.dosage) if task.dosage else ""
            text = (
                f"💊 <b>{escaped_name}</b>\n"
                f"Дозування: {escaped_dosage}\n"
                f"Час: {hh_mm}"
            )
        else:
            escaped_details = html.escape(task.details) if task.details else ""
            lines = [f"⏰ <b>{escaped_name}</b>"]
            if escaped_details:
                lines.append(escaped_details)
            lines.append(f"Час: {hh_mm}")
            text = "\n".join(lines)

        delivery_success = False
        msg_id = None
        markup = build_occurrence_inline_keyboard(occurrence_id, task.context_type)
        try:
            sent = await self.bot_app.bot.send_message(
                chat_id=task.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            if (
                sent is not None
                and hasattr(sent, "message_id")
                and isinstance(sent.message_id, int)
                and not isinstance(sent.message_id, bool)
                and sent.message_id > 0
            ):
                delivery_success = True
                msg_id = sent.message_id
        except Exception:
            delivery_success = False

        if delivery_success:
            await complete_task_occurrence_delivery(occurrence_id, msg_id)
            logger.info(
                f"Delivered occurrence_id={occurrence_id}, task_id={task.id}, "
                f"user_id={task.user_id}, chat_id={task.chat_id}, context_type={task.context_type}"
            )
        else:
            await revert_task_occurrence_delivery(occurrence_id, previous_status)
            logger.error(
                f"Failed delivery for occurrence_id={occurrence_id}, task_id={task.id}, "
                f"user_id={task.user_id}, chat_id={task.chat_id}, reverted_to={previous_status}"
            )

    async def restore_scheduled_tasks(self, now: Optional[datetime] = None):
        """Restores active recurring tasks, reconciles missed occurrences, and schedules future ones."""
        logger.info("Restoring scheduled tasks...")
        if now is None:
            now = datetime.now(timezone.utc)
        else:
            now = _ensure_utc(now)

        active_tasks = await list_active_scheduled_tasks()

        for task in active_tasks:
            # 1. Register exactly one CronTrigger job for each active task
            self.schedule_recurring_task(task)

            # 2. Reconcile offline scheduled occurrences
            latest_planned = await get_latest_task_occurrence_planned_at(task.id)
            boundary = latest_planned if latest_planned is not None else task.created_at
            boundary_utc = _ensure_utc(boundary)

            try:
                hour, minute = map(int, task.local_time.split(":"))
                days_str = ",".join(str(d) for d in task.days_of_week)
                tz = ZoneInfo(task.timezone)
                trigger = CronTrigger(hour=hour, minute=minute, day_of_week=days_str, timezone=tz)

                curr = boundary_utc.astimezone(tz)
                while True:
                    nxt = trigger.get_next_fire_time(curr, curr)
                    if nxt is None:
                        break
                    nxt_utc = nxt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                    if nxt_utc > now:
                        break
                    await get_or_create_task_occurrence(task.id, task.user_id, task.chat_id, nxt_utc)
                    curr = nxt
            except Exception:
                logger.error(f"Error reconciling occurrences for task_id={task.id}")

            # 3. Handle pending occurrences for this active task
            pending_occurrences = await list_pending_task_occurrences(task.id)
            newly_missed_count = 0

            for occ in pending_occurrences:
                occ_due_at = _ensure_utc(occ.due_at)
                if occ_due_at > now:
                    self.schedule_task_occurrence(occ)
                else:
                    newly_missed = await mark_task_occurrence_missed(occ.id, now)
                    if newly_missed:
                        newly_missed_count += 1

            # 4. Send missed summary if any occurrences were newly marked missed
            if newly_missed_count > 0:
                escaped_name = html.escape(task.name)
                summary_text = (
                    f"⚠️ <b>Пропущені заплановані події</b>\n"
                    f"Розклад: <b>{escaped_name}</b>\n"
                    f"Кількість: {newly_missed_count}\n"
                    f"Події було пропущено, поки бот був недоступний."
                )
                if self.bot_app:
                    try:
                        await self.bot_app.bot.send_message(
                            chat_id=task.chat_id,
                            text=summary_text,
                            parse_mode="HTML",
                        )
                        logger.info(
                            f"Sent missed summary for task_id={task.id}, count={newly_missed_count}"
                        )
                    except Exception:
                        logger.error(
                            f"Failed to send missed summary for task_id={task.id}, count={newly_missed_count}"
                        )
		  
scheduler_service = SchedulerService()