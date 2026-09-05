import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Tuple, List, Set
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.database.models import ScheduledTask, TaskOccurrence
from bot.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Context types
CONTEXT_TYPE_MEDICATION = "medication"
CONTEXT_TYPE_GENERIC = "generic"
SUPPORTED_CONTEXT_TYPES: Set[str] = {
    CONTEXT_TYPE_MEDICATION,
    CONTEXT_TYPE_GENERIC,
}

# TaskOccurrence statuses
OCCURRENCE_STATUS_SCHEDULED = "scheduled"
OCCURRENCE_STATUS_DELIVERED = "delivered"
OCCURRENCE_STATUS_DONE = "done"
OCCURRENCE_STATUS_SNOOZED = "snoozed"
OCCURRENCE_STATUS_SKIPPED = "skipped"
OCCURRENCE_STATUS_MISSED = "missed"
SUPPORTED_OCCURRENCE_STATUSES: Set[str] = {
    OCCURRENCE_STATUS_SCHEDULED,
    OCCURRENCE_STATUS_DELIVERED,
    OCCURRENCE_STATUS_DONE,
    OCCURRENCE_STATUS_SNOOZED,
    OCCURRENCE_STATUS_SKIPPED,
    OCCURRENCE_STATUS_MISSED,
}

OCCURRENCE_INITIAL_STATUS = OCCURRENCE_STATUS_SCHEDULED


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize potentially naive SQLite datetimes to aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_task_datetimes(task: Optional[ScheduledTask]) -> Optional[ScheduledTask]:
    if task is not None:
        if task.created_at is not None and task.created_at.tzinfo is None:
            task.created_at = task.created_at.replace(tzinfo=timezone.utc)
        elif task.created_at is not None:
            task.created_at = task.created_at.astimezone(timezone.utc)
        if task.updated_at is not None and task.updated_at.tzinfo is None:
            task.updated_at = task.updated_at.replace(tzinfo=timezone.utc)
        elif task.updated_at is not None:
            task.updated_at = task.updated_at.astimezone(timezone.utc)
    return task


def _normalize_occurrence_datetimes(occ: Optional[TaskOccurrence]) -> Optional[TaskOccurrence]:
    if occ is not None:
        if occ.planned_at is not None and occ.planned_at.tzinfo is None:
            occ.planned_at = occ.planned_at.replace(tzinfo=timezone.utc)
        elif occ.planned_at is not None:
            occ.planned_at = occ.planned_at.astimezone(timezone.utc)
        if occ.due_at is not None and occ.due_at.tzinfo is None:
            occ.due_at = occ.due_at.replace(tzinfo=timezone.utc)
        elif occ.due_at is not None:
            occ.due_at = occ.due_at.astimezone(timezone.utc)
        if occ.created_at is not None and occ.created_at.tzinfo is None:
            occ.created_at = occ.created_at.replace(tzinfo=timezone.utc)
        elif occ.created_at is not None:
            occ.created_at = occ.created_at.astimezone(timezone.utc)
        if occ.updated_at is not None and occ.updated_at.tzinfo is None:
            occ.updated_at = occ.updated_at.replace(tzinfo=timezone.utc)
        elif occ.updated_at is not None:
            occ.updated_at = occ.updated_at.astimezone(timezone.utc)
    return occ


def _validate_user_id(user_id: Any) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be a positive integer and not bool")


def _validate_chat_id(chat_id: Any) -> None:
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        raise ValueError("chat_id must be a non-zero integer and not bool")


def _validate_task_id(task_id: Any) -> None:
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ValueError("task_id must be a positive integer and not bool")


def _validate_context_type(context_type: Any) -> str:
    if not isinstance(context_type, str) or context_type not in SUPPORTED_CONTEXT_TYPES:
        raise ValueError(f"context_type must be one of {sorted(SUPPORTED_CONTEXT_TYPES)}, got: {context_type!r}")
    return context_type


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    return name.strip()


def _canonicalize_local_time(local_time: Any) -> str:
    if not isinstance(local_time, str):
        raise ValueError("local_time must be a string")
    s = local_time.strip()
    match = re.fullmatch(r"^(\d{1,2}):(\d{2})$", s)
    if not match:
        raise ValueError(f"local_time must be in 'HH:MM' format, got: {local_time!r}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"local_time out of range (00:00-23:59), got: {local_time!r}")
    return f"{hour:02d}:{minute:02d}"


def _validate_timezone(timezone_name: Any) -> str:
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("timezone_name must be a non-empty string")
    clean_tz = timezone_name.strip()
    try:
        ZoneInfo(clean_tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Invalid IANA timezone: {timezone_name!r}") from e
    return clean_tz


def _validate_days_of_week(days_of_week: Any) -> List[int]:
    if not isinstance(days_of_week, (list, tuple)):
        raise ValueError("days_of_week must be a list or tuple")
    if not days_of_week:
        raise ValueError("days_of_week cannot be empty")
    cleaned_days = set()
    for d in days_of_week:
        if not isinstance(d, int) or isinstance(d, bool):
            raise ValueError("days_of_week must contain integers (0..6) and not bool")
        if not (0 <= d <= 6):
            raise ValueError(f"Each day in days_of_week must be between 0 and 6, got: {d}")
        cleaned_days.add(d)
    return sorted(cleaned_days)


def _validate_details(details: Any) -> Optional[str]:
    if details is None:
        return None
    if not isinstance(details, str):
        raise ValueError("details must be a string or None")
    return details.strip()


def _validate_dosage(dosage: Any, context_type: str) -> Optional[str]:
    if context_type == CONTEXT_TYPE_MEDICATION:
        if not isinstance(dosage, str) or not dosage.strip():
            raise ValueError("dosage is required and must be a non-empty string for medication tasks")
        return dosage.strip()
    # generic
    if dosage is not None:
        if not isinstance(dosage, str):
            raise ValueError("dosage must be a string or None for generic tasks")
        clean = dosage.strip()
        return clean if clean else None
    return None


async def create_scheduled_task(
    user_id: int,
    chat_id: int,
    context_type: str,
    name: str,
    local_time: str,
    timezone_name: str,
    days_of_week: list[int],
    *,
    details: str | None = None,
    dosage: str | None = None,
) -> ScheduledTask:
    _validate_user_id(user_id)
    _validate_chat_id(chat_id)
    clean_context = _validate_context_type(context_type)
    clean_name = _validate_name(name)
    clean_time = _canonicalize_local_time(local_time)
    clean_tz = _validate_timezone(timezone_name)
    clean_days = _validate_days_of_week(days_of_week)
    clean_details = _validate_details(details)
    clean_dosage = _validate_dosage(dosage, clean_context)

    async with AsyncSessionLocal() as session:
        task = ScheduledTask(
            user_id=user_id,
            chat_id=chat_id,
            context_type=clean_context,
            name=clean_name,
            details=clean_details,
            dosage=clean_dosage,
            local_time=clean_time,
            timezone=clean_tz,
            days_of_week=clean_days,
            active=True,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        _normalize_task_datetimes(task)
        logger.info(
            f"Created scheduled task {task.id} (context_type={clean_context}, "
            f"user_id={user_id}, chat_id={chat_id})"
        )
        return task


async def get_scheduled_task(
    task_id: int,
    user_id: int,
    chat_id: int,
) -> ScheduledTask | None:
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None

    async with AsyncSessionLocal() as session:
        task = await session.get(ScheduledTask, task_id)
        if not task or task.user_id != user_id or task.chat_id != chat_id:
            return None
        _normalize_task_datetimes(task)
        return task


async def list_active_scheduled_tasks() -> list[ScheduledTask]:
    async with AsyncSessionLocal() as session:
        stmt = (
            select(ScheduledTask)
            .where(ScheduledTask.active.is_(True))
            .order_by(ScheduledTask.id.asc())
        )
        res = await session.execute(stmt)
        tasks = list(res.scalars().all())
        for task in tasks:
            _normalize_task_datetimes(task)
        return tasks


async def deactivate_scheduled_task(
    task_id: int,
    user_id: int,
    chat_id: int,
) -> ScheduledTask | None:
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None

    async with AsyncSessionLocal() as session:
        stmt = (
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.chat_id == chat_id,
                ScheduledTask.active.is_(True),
            )
            .values(active=False)
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        if res.rowcount > 0:
            await session.commit()
            task = await session.get(ScheduledTask, task_id)
            _normalize_task_datetimes(task)
            logger.info(f"Deactivated scheduled task {task_id} for user={user_id}, chat={chat_id}")
            return task

        # Non-transition path: verify task belongs to exact owner
        task = await session.get(ScheduledTask, task_id)
        if not task or task.user_id != user_id or task.chat_id != chat_id:
            return None

        _normalize_task_datetimes(task)
        return task


async def get_or_create_task_occurrence(
    task_id: int,
    user_id: int,
    chat_id: int,
    planned_at: datetime,
) -> tuple[TaskOccurrence | None, bool]:
    _validate_task_id(task_id)
    _validate_user_id(user_id)
    _validate_chat_id(chat_id)

    if not isinstance(planned_at, datetime) or isinstance(planned_at, bool):
        raise ValueError("planned_at must be a timezone-aware datetime")
    if planned_at.tzinfo is None or planned_at.tzinfo.utcoffset(planned_at) is None:
        raise ValueError("planned_at must be a timezone-aware datetime (tzinfo required)")

    planned_at_utc = planned_at.astimezone(timezone.utc)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                task = await session.get(ScheduledTask, task_id)
                if not task or task.user_id != user_id or task.chat_id != chat_id or not task.active:
                    return None, False

                stmt = (
                    sqlite_insert(TaskOccurrence)
                    .values(
                        task_id=task_id,
                        planned_at=planned_at_utc,
                        due_at=planned_at_utc,
                        status=OCCURRENCE_INITIAL_STATUS,
                        telegram_message_id=None,
                    )
                    .on_conflict_do_nothing(index_elements=["task_id", "planned_at"])
                )
                res = await session.execute(stmt)
                created = (res.rowcount > 0)
                await session.commit()

                occ_stmt = (
                    select(TaskOccurrence)
                    .where(
                        TaskOccurrence.task_id == task_id,
                        TaskOccurrence.planned_at == planned_at_utc,
                    )
                )
                occ_res = await session.execute(occ_stmt)
                occ = occ_res.scalars().first()
                _normalize_occurrence_datetimes(occ)
                return occ, created
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))
        except IntegrityError:
            async with AsyncSessionLocal() as session:
                occ_stmt = (
                    select(TaskOccurrence)
                    .where(
                        TaskOccurrence.task_id == task_id,
                        TaskOccurrence.planned_at == planned_at_utc,
                    )
                )
                occ_res = await session.execute(occ_stmt)
                occ = occ_res.scalars().first()
                if occ is not None:
                    _normalize_occurrence_datetimes(occ)
                    return occ, False
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None, False


async def get_task_occurrence(
    occurrence_id: int,
    user_id: int,
    chat_id: int,
) -> TaskOccurrence | None:
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None

    async with AsyncSessionLocal() as session:
        stmt = (
            select(TaskOccurrence)
            .join(ScheduledTask, TaskOccurrence.task_id == ScheduledTask.id)
            .where(
                TaskOccurrence.id == occurrence_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.chat_id == chat_id,
            )
        )
        res = await session.execute(stmt)
        occ = res.scalars().first()
        if not occ:
            return None
        _normalize_occurrence_datetimes(occ)
        return occ


async def claim_task_occurrence_for_delivery(
    occurrence_id: int,
) -> tuple[TaskOccurrence | None, ScheduledTask | None, str | None]:
    """Atomically claims an occurrence for delivery.

    Validates that:
    - occurrence exists
    - parent task exists and is active
    - occurrence status is deliverable ('scheduled' or 'snoozed')

    Conditionally transitions status to 'delivered' in the database.
    Returns (occurrence, parent_task, previous_status) on success, or (None, None, None).
    """
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return None, None, None

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(TaskOccurrence, ScheduledTask)
                    .join(ScheduledTask, TaskOccurrence.task_id == ScheduledTask.id)
                    .where(TaskOccurrence.id == occurrence_id)
                )
                res = await session.execute(stmt)
                row = res.first()
                if not row:
                    return None, None, None

                occ, task = row
                if not task.active:
                    return None, None, None

                if occ.status not in (OCCURRENCE_STATUS_SCHEDULED, OCCURRENCE_STATUS_SNOOZED):
                    return None, None, None

                previous_status = occ.status

                update_stmt = (
                    update(TaskOccurrence)
                    .where(
                        TaskOccurrence.id == occurrence_id,
                        TaskOccurrence.status == previous_status,
                    )
                    .values(status=OCCURRENCE_STATUS_DELIVERED)
                    .execution_options(synchronize_session=False)
                )
                update_res = await session.execute(update_stmt)
                if update_res.rowcount != 1:
                    return None, None, None

                await session.commit()
                occ.status = OCCURRENCE_STATUS_DELIVERED
                _normalize_occurrence_datetimes(occ)
                _normalize_task_datetimes(task)
                return occ, task, previous_status
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None, None, None


async def complete_task_occurrence_delivery(
    occurrence_id: int,
    telegram_message_id: int,
) -> bool:
    """Stores the Telegram message ID after successful delivery."""
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return False
    if not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool) or telegram_message_id <= 0:
        return False

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(TaskOccurrence)
                    .where(
                        TaskOccurrence.id == occurrence_id,
                        TaskOccurrence.status == OCCURRENCE_STATUS_DELIVERED,
                    )
                    .values(telegram_message_id=telegram_message_id)
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount > 0:
                    await session.commit()
                    return True
                return False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return False


async def revert_task_occurrence_delivery(
    occurrence_id: int,
    previous_status: str,
) -> bool:
    """Restores occurrence to its previous deliverable status on send failure."""
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return False
    if previous_status not in (OCCURRENCE_STATUS_SCHEDULED, OCCURRENCE_STATUS_SNOOZED):
        return False

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(TaskOccurrence)
                    .where(
                        TaskOccurrence.id == occurrence_id,
                        TaskOccurrence.status == OCCURRENCE_STATUS_DELIVERED,
                    )
                    .values(status=previous_status, telegram_message_id=None)
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount > 0:
                    await session.commit()
                    return True
                return False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return False


async def mark_task_occurrence_missed(occurrence_id: int, now: datetime) -> bool:
    """Transitions a scheduled or snoozed occurrence to missed if due_at <= now."""
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return False
    if not isinstance(now, datetime) or isinstance(now, bool):
        raise ValueError("now must be a timezone-aware datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be a timezone-aware datetime (tzinfo required)")

    now_utc = now.astimezone(timezone.utc)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(TaskOccurrence)
                    .where(
                        TaskOccurrence.id == occurrence_id,
                        TaskOccurrence.status.in_([OCCURRENCE_STATUS_SCHEDULED, OCCURRENCE_STATUS_SNOOZED]),
                        TaskOccurrence.due_at <= now_utc,
                    )
                    .values(status=OCCURRENCE_STATUS_MISSED)
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount == 1:
                    await session.commit()
                    return True
                return False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return False


async def get_latest_task_occurrence_planned_at(task_id: int) -> datetime | None:
    """Returns the latest planned_at UTC datetime for a task, or None."""
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return None

    async with AsyncSessionLocal() as session:
        stmt = (
            select(TaskOccurrence.planned_at)
            .where(TaskOccurrence.task_id == task_id)
            .order_by(TaskOccurrence.planned_at.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        val = res.scalars().first()
        return _ensure_utc(val)


async def list_pending_task_occurrences(task_id: int) -> list[TaskOccurrence]:
    """Returns all scheduled or snoozed occurrences for a task, ordered by due_at asc."""
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return []

    async with AsyncSessionLocal() as session:
        stmt = (
            select(TaskOccurrence)
            .where(
                TaskOccurrence.task_id == task_id,
                TaskOccurrence.status.in_([OCCURRENCE_STATUS_SCHEDULED, OCCURRENCE_STATUS_SNOOZED]),
            )
            .order_by(TaskOccurrence.due_at.asc())
        )
        res = await session.execute(stmt)
        occurrences = list(res.scalars().all())
        for occ in occurrences:
            _normalize_occurrence_datetimes(occ)
        return occurrences


async def create_scheduled_tasks_batch(
    user_id: int,
    chat_id: int,
    context_type: str,
    timezone_name: str,
    items: list[dict[str, Any]],
) -> list[ScheduledTask]:
    """
    Creates multiple ScheduledTask rows in one atomic database transaction.
    Validates every item before inserting any row.
    """
    _validate_user_id(user_id)
    _validate_chat_id(chat_id)
    clean_context = _validate_context_type(context_type)
    clean_tz = _validate_timezone(timezone_name)

    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("items must be a non-empty list of schedule items")

    validated_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each item in items must be a dictionary")
        name = _validate_name(item.get("name"))
        time_to_use = item.get("resolved_local_time") or item.get("local_time")
        clean_time = _canonicalize_local_time(time_to_use)
        days_to_use = item.get("resolved_days_of_week") or item.get("days_of_week")
        clean_days = _validate_days_of_week(days_to_use)
        clean_details = _validate_details(item.get("details"))
        clean_dosage = _validate_dosage(item.get("dosage"), clean_context)

        validated_items.append({
            "name": name,
            "local_time": clean_time,
            "days_of_week": clean_days,
            "details": clean_details,
            "dosage": clean_dosage,
        })

    async with AsyncSessionLocal() as session:
        created_tasks: list[ScheduledTask] = []
        for v in validated_items:
            task = ScheduledTask(
                user_id=user_id,
                chat_id=chat_id,
                context_type=clean_context,
                name=v["name"],
                details=v["details"],
                dosage=v["dosage"],
                local_time=v["local_time"],
                timezone=clean_tz,
                days_of_week=v["days_of_week"],
                active=True,
            )
            session.add(task)
            created_tasks.append(task)
        await session.commit()
        for task in created_tasks:
            await session.refresh(task)
            _normalize_task_datetimes(task)

        task_ids = [t.id for t in created_tasks]
        logger.info(
            f"Created scheduled tasks batch: count={len(created_tasks)}, "
            f"task_ids={task_ids}, context_type={clean_context}, "
            f"user_id={user_id}, chat_id={chat_id}"
        )
        return created_tasks


async def transition_task_occurrence_terminal(
    occurrence_id: int,
    user_id: int,
    chat_id: int,
    telegram_message_id: int,
    target_status: str,
) -> Tuple[Optional[TaskOccurrence], bool]:
    """
    Atomically transitions a delivered occurrence to done or skipped.
    Enforces exact user_id/chat_id ownership and matching telegram_message_id.
    Returns (occurrence, transitioned).
    """
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return None, False
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None, False
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        return None, False
    if not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool) or telegram_message_id <= 0:
        return None, False
    if target_status not in (OCCURRENCE_STATUS_DONE, OCCURRENCE_STATUS_SKIPPED):
        return None, False

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(TaskOccurrence)
                    .where(
                        TaskOccurrence.id == occurrence_id,
                        TaskOccurrence.status == OCCURRENCE_STATUS_DELIVERED,
                        TaskOccurrence.telegram_message_id == telegram_message_id,
                        TaskOccurrence.task_id.in_(
                            select(ScheduledTask.id).where(
                                ScheduledTask.user_id == user_id,
                                ScheduledTask.chat_id == chat_id,
                            )
                        ),
                    )
                    .values(status=target_status)
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount == 1:
                    await session.commit()
                    logger.info(f"Transitioned occurrence {occurrence_id} to {target_status} for user {user_id}, chat {chat_id}")
                    occ = await get_task_occurrence(occurrence_id, user_id, chat_id)
                    return occ, True

                occ = await get_task_occurrence(occurrence_id, user_id, chat_id)
                return occ, False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None, False


async def snooze_task_occurrence(
    occurrence_id: int,
    user_id: int,
    chat_id: int,
    telegram_message_id: int,
    minutes: int,
    now: datetime,
) -> Tuple[Optional[TaskOccurrence], bool]:
    """
    Atomically transitions a delivered occurrence to snoozed.
    Calculates new due_at = now.astimezone(UTC) + timedelta(minutes=minutes).
    Clears telegram_message_id while preserving planned_at.
    Enforces exact user_id/chat_id ownership and matching telegram_message_id.
    Returns (occurrence, transitioned).
    """
    if not isinstance(occurrence_id, int) or isinstance(occurrence_id, bool) or occurrence_id <= 0:
        return None, False
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None, False
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        return None, False
    if not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool) or telegram_message_id <= 0:
        return None, False
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes not in (15, 30):
        return None, False
    if not isinstance(now, datetime) or isinstance(now, bool):
        raise ValueError("now must be a timezone-aware datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be a timezone-aware datetime (tzinfo required)")

    now_utc = now.astimezone(timezone.utc)
    new_due_at = now_utc + timedelta(minutes=minutes)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(TaskOccurrence)
                    .where(
                        TaskOccurrence.id == occurrence_id,
                        TaskOccurrence.status == OCCURRENCE_STATUS_DELIVERED,
                        TaskOccurrence.telegram_message_id == telegram_message_id,
                        TaskOccurrence.task_id.in_(
                            select(ScheduledTask.id).where(
                                ScheduledTask.user_id == user_id,
                                ScheduledTask.chat_id == chat_id,
                            )
                        ),
                    )
                    .values(
                        status=OCCURRENCE_STATUS_SNOOZED,
                        due_at=new_due_at,
                        telegram_message_id=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount == 1:
                    await session.commit()
                    logger.info(f"Snoozed occurrence {occurrence_id} for {minutes}m for user {user_id}, chat {chat_id}")
                    occ = await get_task_occurrence(occurrence_id, user_id, chat_id)
                    return occ, True

                occ = await get_task_occurrence(occurrence_id, user_id, chat_id)
                return occ, False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None, False
