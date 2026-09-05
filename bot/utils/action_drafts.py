import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set

from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError
from bot.database.models import ActionDraft
from bot.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Status constants
DRAFT_STATUS_AWAITING_INFO = "awaiting_info"
DRAFT_STATUS_PENDING_CONFIRMATION = "pending_confirmation"
DRAFT_STATUS_CONFIRMED = "confirmed"
DRAFT_STATUS_CANCELLED = "cancelled"
DRAFT_STATUS_EXPIRED = "expired"

ACTIVE_DRAFT_STATUSES: Tuple[str, ...] = (
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
)

SUPPORTED_ACTION_TYPES: Set[str] = {
    "schedule_reminder",
    "delete_reminder",
    "create_scheduled_tasks",
    "add_shopping_items",
    "set_shopping_item_state",
    "delete_shopping_item",
    "clear_bought_items",
}

MAX_DRAFT_TTL_SECONDS: int = 86400  # 24 hours


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize potentially naive SQLite datetimes to aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _validate_user_id(user_id: Any) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be a positive integer and not bool")


def _validate_chat_id(chat_id: Any) -> None:
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        raise ValueError("chat_id must be a non-zero integer and not bool")


def _validate_source_message_id(source_message_id: Any) -> None:
    if source_message_id is not None:
        if not isinstance(source_message_id, int) or isinstance(source_message_id, bool) or source_message_id <= 0:
            raise ValueError("source_message_id must be a positive integer and not bool when supplied")


def _validate_action_type(action_type: Any) -> None:
    if not isinstance(action_type, str) or action_type not in SUPPORTED_ACTION_TYPES:
        raise ValueError(f"action_type must be one of {sorted(SUPPORTED_ACTION_TYPES)}, got: {action_type!r}")


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary/mapping")


def _validate_and_clean_missing_fields(missing_fields: Any) -> List[str]:
    if missing_fields is None:
        return []
    if not isinstance(missing_fields, (list, tuple)):
        raise ValueError("missing_fields must be a list or tuple")

    cleaned: List[str] = []
    for item in missing_fields:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("missing_fields must contain only non-empty strings")
        val = item.strip()
        if val not in cleaned:
            cleaned.append(val)
    return cleaned


def _validate_ttl(ttl_seconds: Any) -> None:
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0 or ttl_seconds > MAX_DRAFT_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be a positive integer <= {MAX_DRAFT_TTL_SECONDS}")


async def create_action_draft(
    user_id: int,
    chat_id: int,
    action_type: str,
    payload: Dict[str, Any],
    missing_fields: Optional[List[str]] = None,
    source_message_id: Optional[int] = None,
    ttl_seconds: int = 3600,
) -> ActionDraft:
    """
    Validates inputs and creates a persistent ActionDraft.
    Cancels/expires any existing active drafts for (user_id, chat_id) using BEGIN IMMEDIATE
    to prevent lost updates, and retries on unique constraint collision so concurrent callers
    deterministically replace rather than leak IntegrityError.
    """
    _validate_user_id(user_id)
    _validate_chat_id(chat_id)
    _validate_source_message_id(source_message_id)
    _validate_action_type(action_type)
    _validate_payload(payload)
    cleaned_missing = _validate_and_clean_missing_fields(missing_fields)
    _validate_ttl(ttl_seconds)

    status = DRAFT_STATUS_AWAITING_INFO if cleaned_missing else DRAFT_STATUS_PENDING_CONFIRMATION

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                now = datetime.now(timezone.utc)
                expires_at = now + timedelta(seconds=ttl_seconds)

                # Query active drafts for this user and chat
                stmt = (
                    select(ActionDraft)
                    .where(
                        ActionDraft.user_id == user_id,
                        ActionDraft.chat_id == chat_id,
                        ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                    )
                )
                res = await session.execute(stmt)
                active_drafts = res.scalars().all()

                for old in active_drafts:
                    if _ensure_utc(old.expires_at) <= now:
                        old.status = DRAFT_STATUS_EXPIRED
                    else:
                        old.status = DRAFT_STATUS_CANCELLED

                # Flush cancelled/expired updates before inserting the replacement to respect partial unique index
                if active_drafts:
                    await session.flush()

                draft = ActionDraft(
                    user_id=user_id,
                    chat_id=chat_id,
                    source_message_id=source_message_id,
                    action_type=action_type,
                    payload=dict(payload),
                    missing_fields=cleaned_missing,
                    status=status,
                    expires_at=expires_at,
                )
                session.add(draft)
                await session.commit()
                await session.refresh(draft)

                logger.info(f"Created action draft {draft.id} ({action_type}) for user={user_id}, chat={chat_id}, status={status}")
                return draft
        except IntegrityError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.02 * (attempt + 1))


async def get_action_draft(draft_id: int, user_id: int, chat_id: int) -> Optional[ActionDraft]:
    """
    Retrieves a draft by exact ID, user_id, and chat_id.
    If an active draft has expired, persists status=expired conditionally before returning.
    """
    async with AsyncSessionLocal() as session:
        draft = await session.get(ActionDraft, draft_id)
        if not draft or draft.user_id != user_id or draft.chat_id != chat_id:
            return None

        now = datetime.now(timezone.utc)
        if draft.status in ACTIVE_DRAFT_STATUSES and _ensure_utc(draft.expires_at) <= now:
            res = await session.execute(
                update(ActionDraft)
                .where(
                    ActionDraft.id == draft_id,
                    ActionDraft.user_id == user_id,
                    ActionDraft.chat_id == chat_id,
                    ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                )
                .values(status=DRAFT_STATUS_EXPIRED)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount > 0:
                await session.commit()
                await session.refresh(draft)

        return draft


async def get_active_action_draft(user_id: int, chat_id: int) -> Optional[ActionDraft]:
    """
    Returns only an unexpired draft in one of the active statuses for (user_id, chat_id).
    Persists expired status conditionally when a stale draft is encountered.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        stmt = (
            select(ActionDraft)
            .where(
                ActionDraft.user_id == user_id,
                ActionDraft.chat_id == chat_id,
                ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
            )
        )
        res = await session.execute(stmt)
        draft = res.scalars().first()
        if not draft:
            return None

        if _ensure_utc(draft.expires_at) <= now:
            await session.execute(
                update(ActionDraft)
                .where(
                    ActionDraft.id == draft.id,
                    ActionDraft.user_id == user_id,
                    ActionDraft.chat_id == chat_id,
                    ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                )
                .values(status=DRAFT_STATUS_EXPIRED)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return None

        return draft


async def update_action_draft_information(
    draft_id: int,
    user_id: int,
    chat_id: int,
    *,
    payload_updates: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[List[str]] = None,
) -> Optional[ActionDraft]:
    """
    Updates payload and/or missing_fields on an owned, active, unexpired draft.
    Uses BEGIN IMMEDIATE and conditional update matching active status so concurrent
    terminal transitions (confirm/cancel) are never overwritten or revived.
    """
    if payload_updates is not None:
        _validate_payload(payload_updates)

    cleaned_missing = None
    if missing_fields is not None:
        cleaned_missing = _validate_and_clean_missing_fields(missing_fields)

    async with AsyncSessionLocal() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        draft = await session.get(ActionDraft, draft_id)
        if not draft or draft.user_id != user_id or draft.chat_id != chat_id:
            return None

        if draft.status not in ACTIVE_DRAFT_STATUSES:
            return None

        now = datetime.now(timezone.utc)
        if _ensure_utc(draft.expires_at) <= now:
            await session.execute(
                update(ActionDraft)
                .where(
                    ActionDraft.id == draft_id,
                    ActionDraft.user_id == user_id,
                    ActionDraft.chat_id == chat_id,
                    ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                )
                .values(status=DRAFT_STATUS_EXPIRED)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return None

        new_payload = dict(draft.payload or {})
        if payload_updates is not None:
            new_payload.update(payload_updates)

        new_missing = cleaned_missing if cleaned_missing is not None else list(draft.missing_fields or [])
        new_status = DRAFT_STATUS_AWAITING_INFO if new_missing else DRAFT_STATUS_PENDING_CONFIRMATION

        stmt = (
            update(ActionDraft)
            .where(
                ActionDraft.id == draft_id,
                ActionDraft.user_id == user_id,
                ActionDraft.chat_id == chat_id,
                ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                ActionDraft.expires_at > now,
            )
            .values(
                payload=new_payload,
                missing_fields=new_missing,
                status=new_status,
            )
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        if res.rowcount == 0:
            await session.commit()
            return None

        await session.commit()
        await session.refresh(draft)
        logger.info(f"Updated action draft {draft_id} for user={user_id}, chat={chat_id}, status={draft.status}")
        return draft


async def confirm_action_draft(draft_id: int, user_id: int, chat_id: int) -> Tuple[Optional[ActionDraft], bool]:
    """
    Transitions a pending_confirmation unexpired draft to confirmed.
    Uses atomic conditional update to guarantee exactly one winner among concurrent calls.
    Returns (draft, transitioned). Repeated confirmation of an already-confirmed draft returns (draft, False).
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        stmt = (
            update(ActionDraft)
            .where(
                ActionDraft.id == draft_id,
                ActionDraft.user_id == user_id,
                ActionDraft.chat_id == chat_id,
                ActionDraft.status == DRAFT_STATUS_PENDING_CONFIRMATION,
                ActionDraft.expires_at > now,
            )
            .values(status=DRAFT_STATUS_CONFIRMED)
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        if res.rowcount > 0:
            await session.commit()
            draft = await session.get(ActionDraft, draft_id)
            logger.info(f"Confirmed action draft {draft_id} for user={user_id}, chat={chat_id}")
            return draft, True

        # Non-transition path: reload row from DB
        draft = await session.get(ActionDraft, draft_id)
        if not draft or draft.user_id != user_id or draft.chat_id != chat_id:
            return None, False

        # If it was active but expired, update status conditionally
        if draft.status in ACTIVE_DRAFT_STATUSES and _ensure_utc(draft.expires_at) <= now:
            await session.execute(
                update(ActionDraft)
                .where(
                    ActionDraft.id == draft_id,
                    ActionDraft.user_id == user_id,
                    ActionDraft.chat_id == chat_id,
                    ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                )
                .values(status=DRAFT_STATUS_EXPIRED)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            await session.refresh(draft)

        return draft, False


async def cancel_action_draft(draft_id: int, user_id: int, chat_id: int) -> Optional[ActionDraft]:
    """
    Cancels an active unexpired draft for an owner using an atomic conditional update.
    Idempotent for already cancelled drafts. Does not alter confirmed or expired drafts.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        stmt = (
            update(ActionDraft)
            .where(
                ActionDraft.id == draft_id,
                ActionDraft.user_id == user_id,
                ActionDraft.chat_id == chat_id,
                ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                ActionDraft.expires_at > now,
            )
            .values(status=DRAFT_STATUS_CANCELLED)
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        if res.rowcount > 0:
            await session.commit()
            draft = await session.get(ActionDraft, draft_id)
            logger.info(f"Cancelled action draft {draft_id} for user={user_id}, chat={chat_id}")
            return draft

        # Non-transition path: reload row from DB
        draft = await session.get(ActionDraft, draft_id)
        if not draft or draft.user_id != user_id or draft.chat_id != chat_id:
            return None

        # Already terminal: idempotent, preserve terminal state
        if draft.status in (DRAFT_STATUS_CANCELLED, DRAFT_STATUS_CONFIRMED, DRAFT_STATUS_EXPIRED):
            return draft

        # If active but expired, update status conditionally
        if draft.status in ACTIVE_DRAFT_STATUSES and _ensure_utc(draft.expires_at) <= now:
            await session.execute(
                update(ActionDraft)
                .where(
                    ActionDraft.id == draft_id,
                    ActionDraft.user_id == user_id,
                    ActionDraft.chat_id == chat_id,
                    ActionDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                )
                .values(status=DRAFT_STATUS_EXPIRED)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            await session.refresh(draft)

        return draft
