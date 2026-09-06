import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError, OperationalError

from bot.database.models import UserList, ListItem
from bot.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

LIST_TYPE_SHOPPING = "shopping"
SUPPORTED_LIST_TYPES = {LIST_TYPE_SHOPPING}
DEFAULT_SHOPPING_LIST_NAME = "Покупки"


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize potentially naive SQLite datetimes to aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_list_datetimes(user_list: Optional[UserList]) -> Optional[UserList]:
    if user_list is not None:
        if user_list.created_at is not None and user_list.created_at.tzinfo is None:
            user_list.created_at = user_list.created_at.replace(tzinfo=timezone.utc)
        elif user_list.created_at is not None:
            user_list.created_at = user_list.created_at.astimezone(timezone.utc)
        if user_list.updated_at is not None and user_list.updated_at.tzinfo is None:
            user_list.updated_at = user_list.updated_at.replace(tzinfo=timezone.utc)
        elif user_list.updated_at is not None:
            user_list.updated_at = user_list.updated_at.astimezone(timezone.utc)
    return user_list


def _normalize_item_datetimes(item: Optional[ListItem]) -> Optional[ListItem]:
    if item is not None:
        if item.created_at is not None and item.created_at.tzinfo is None:
            item.created_at = item.created_at.replace(tzinfo=timezone.utc)
        elif item.created_at is not None:
            item.created_at = item.created_at.astimezone(timezone.utc)
        if item.updated_at is not None and item.updated_at.tzinfo is None:
            item.updated_at = item.updated_at.replace(tzinfo=timezone.utc)
        elif item.updated_at is not None:
            item.updated_at = item.updated_at.astimezone(timezone.utc)
    return item


def _validate_user_id(user_id: Any) -> int:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be a positive integer and not bool")
    return user_id


def _validate_chat_id(chat_id: Any) -> int:
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        raise ValueError("chat_id must be a non-zero integer and not bool")
    return chat_id


def _validate_list_id(list_id: Any) -> int:
    if not isinstance(list_id, int) or isinstance(list_id, bool) or list_id <= 0:
        raise ValueError("list_id must be a positive integer and not bool")
    return list_id


def _validate_item_id(item_id: Any) -> int:
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
        raise ValueError("item_id must be a positive integer and not bool")
    return item_id


def _validate_list_type(list_type: Any) -> str:
    if not isinstance(list_type, str) or list_type not in SUPPORTED_LIST_TYPES:
        raise ValueError(f"list_type must be one of {sorted(SUPPORTED_LIST_TYPES)}, got: {list_type!r}")
    return list_type


def _clean_text(val: Any, field_name: str = "text") -> str:
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = re.sub(r"\s+", " ", val.strip())
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty after cleaning")
    return cleaned


def _normalize_name(cleaned_name: str) -> str:
    return cleaned_name.casefold()


async def create_or_get_user_list(
    user_id: int,
    chat_id: int,
    list_type: str,
    name: str,
) -> tuple[UserList, bool]:
    _validate_user_id(user_id)
    _validate_chat_id(chat_id)
    clean_type = _validate_list_type(list_type)
    clean_name = _clean_text(name, "list name")
    norm_name = _normalize_name(clean_name)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(UserList)
                    .where(
                        UserList.chat_id == chat_id,
                        UserList.list_type == clean_type,
                        UserList.normalized_name == norm_name,
                    )
                )
                res = await session.execute(stmt)
                existing = res.scalars().first()
                if existing:
                    _normalize_list_datetimes(existing)
                    return existing, False

                user_list = UserList(
                    chat_id=chat_id,
                    list_type=clean_type,
                    name=clean_name,
                    normalized_name=norm_name,
                    created_by_user_id=user_id,
                )
                session.add(user_list)
                await session.flush()
                await session.refresh(user_list)
                await session.commit()
                _normalize_list_datetimes(user_list)
                logger.info(
                    "Created user list %d (type=%s, chat_id=%d, actor=%d)",
                    user_list.id, clean_type, chat_id, user_id,
                )
                return user_list, True
        except IntegrityError:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(UserList)
                    .where(
                        UserList.chat_id == chat_id,
                        UserList.list_type == clean_type,
                        UserList.normalized_name == norm_name,
                    )
                )
                res = await session.execute(stmt)
                winner = res.scalars().first()
                if winner:
                    _normalize_list_datetimes(winner)
                    return winner, False
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    # Fallback to query existing winner
    async with AsyncSessionLocal() as session:
        stmt = (
            select(UserList)
            .where(
                UserList.chat_id == chat_id,
                UserList.list_type == clean_type,
                UserList.normalized_name == norm_name,
            )
        )
        res = await session.execute(stmt)
        winner = res.scalars().first()
        if winner:
            _normalize_list_datetimes(winner)
            return winner, False

    raise RuntimeError("Failed to create or get user list after retries")


async def get_user_list(
    list_id: int,
    chat_id: int,
) -> UserList | None:
    _validate_list_id(list_id)
    _validate_chat_id(chat_id)

    async with AsyncSessionLocal() as session:
        user_list = await session.get(UserList, list_id)
        if not user_list or user_list.chat_id != chat_id:
            return None
        _normalize_list_datetimes(user_list)
        return user_list


async def list_user_lists(
    chat_id: int,
    list_type: str = LIST_TYPE_SHOPPING,
) -> list[UserList]:
    _validate_chat_id(chat_id)
    clean_type = _validate_list_type(list_type)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(UserList)
            .where(
                UserList.chat_id == chat_id,
                UserList.list_type == clean_type,
            )
            .order_by(UserList.id.asc())
        )
        res = await session.execute(stmt)
        lists = list(res.scalars().all())
        for ul in lists:
            _normalize_list_datetimes(ul)
        return lists


async def resolve_user_list(
    user_id: int,
    chat_id: int,
    list_type: str = LIST_TYPE_SHOPPING,
    explicit_name: str | None = None,
    current_list_id: int | None = None,
) -> tuple[UserList, bool]:
    _validate_user_id(user_id)
    _validate_chat_id(chat_id)
    clean_type = _validate_list_type(list_type)

    # 1. If explicit_name is passed:
    if explicit_name is not None:
        clean_explicit = _clean_text(explicit_name, "explicit_name")
        return await create_or_get_user_list(
            user_id=user_id,
            chat_id=chat_id,
            list_type=clean_type,
            name=clean_explicit,
        )

    # 2. Otherwise, if valid current_list_id belongs to exact chat_id and list_type:
    if (
        current_list_id is not None
        and isinstance(current_list_id, int)
        and not isinstance(current_list_id, bool)
        and current_list_id > 0
    ):
        current = await get_user_list(current_list_id, chat_id)
        if current is not None and current.list_type == clean_type:
            return current, False

    # 3. Otherwise get lists of exact chat_id/list_type:
    existing_lists = await list_user_lists(chat_id, clean_type)
    if len(existing_lists) == 1:
        return existing_lists[0], False

    # 4. Otherwise find or concurrency-safe create default list "Покупки"
    return await create_or_get_user_list(
        user_id=user_id,
        chat_id=chat_id,
        list_type=clean_type,
        name=DEFAULT_SHOPPING_LIST_NAME,
    )


async def list_list_items(
    list_id: int,
    chat_id: int,
) -> list[ListItem] | None:
    _validate_list_id(list_id)
    _validate_chat_id(chat_id)

    async with AsyncSessionLocal() as session:
        user_list = await session.get(UserList, list_id)
        if not user_list or user_list.chat_id != chat_id:
            return None

        stmt = (
            select(ListItem)
            .where(ListItem.list_id == list_id)
            .order_by(ListItem.is_done.asc(), ListItem.id.asc())
        )
        res = await session.execute(stmt)
        items = list(res.scalars().all())
        for it in items:
            _normalize_item_datetimes(it)
        return items


async def find_existing_user_list(
    chat_id: int,
    list_name: Optional[str] = None,
    list_type: str = LIST_TYPE_SHOPPING,
) -> Optional[UserList]:
    _validate_chat_id(chat_id)
    clean_type = _validate_list_type(list_type)

    if list_name is not None and isinstance(list_name, str) and list_name.strip():
        clean_name = re.sub(r"\s+", " ", list_name.strip())
        norm_name = _normalize_name(clean_name)
        async with AsyncSessionLocal() as session:
            stmt = (
                select(UserList)
                .where(
                    UserList.chat_id == chat_id,
                    UserList.list_type == clean_type,
                    UserList.normalized_name == norm_name,
                )
            )
            res = await session.execute(stmt)
            user_list = res.scalars().first()
            _normalize_list_datetimes(user_list)
            return user_list

    existing_lists = await list_user_lists(chat_id, clean_type)
    if len(existing_lists) == 1:
        return existing_lists[0]
    elif len(existing_lists) > 1:
        default_norm = _normalize_name(DEFAULT_SHOPPING_LIST_NAME)
        for ul in existing_lists:
            if ul.normalized_name == default_norm:
                return ul
        return None

    return None


async def get_list_item(
    item_id: int,
    chat_id: int,
) -> Optional[ListItem]:
    _validate_item_id(item_id)
    _validate_chat_id(chat_id)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(ListItem)
            .join(UserList, ListItem.list_id == UserList.id)
            .where(
                ListItem.id == item_id,
                UserList.chat_id == chat_id,
            )
        )
        res = await session.execute(stmt)
        item = res.scalars().first()
        _normalize_item_datetimes(item)
        return item


async def add_list_items(
    list_id: int,
    chat_id: int,
    actor_user_id: int,
    items: list[str] | tuple[str, ...],
) -> list[ListItem] | None:
    _validate_list_id(list_id)
    _validate_chat_id(chat_id)
    _validate_user_id(actor_user_id)

    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("items must be a non-empty list or tuple of strings")

    cleaned_texts: list[str] = []
    for it in items:
        cleaned_text = _clean_text(it, "item text")
        cleaned_texts.append(cleaned_text)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                user_list = await session.get(UserList, list_id)
                if not user_list or user_list.chat_id != chat_id:
                    return None

                created_items: list[ListItem] = []
                for text_val in cleaned_texts:
                    item = ListItem(
                        list_id=list_id,
                        text=text_val,
                        is_done=False,
                        created_by_user_id=actor_user_id,
                        updated_by_user_id=actor_user_id,
                    )
                    session.add(item)
                    created_items.append(item)

                await session.flush()
                for item in created_items:
                    await session.refresh(item)
                await session.commit()

                for item in created_items:
                    _normalize_item_datetimes(item)

                logger.info(
                    "Added %d items to list %d in chat %d by actor %d",
                    len(created_items), list_id, chat_id, actor_user_id,
                )
                return created_items
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None


async def set_list_item_done(
    item_id: int,
    chat_id: int,
    actor_user_id: int,
    is_done: bool,
) -> tuple[ListItem | None, bool]:
    _validate_item_id(item_id)
    _validate_chat_id(chat_id)
    _validate_user_id(actor_user_id)
    if not isinstance(is_done, bool):
        raise ValueError("is_done must be a boolean")

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(ListItem)
                    .where(
                        ListItem.id == item_id,
                        ListItem.is_done.is_(not is_done),
                        ListItem.list_id.in_(
                            select(UserList.id).where(UserList.chat_id == chat_id)
                        ),
                    )
                    .values(
                        is_done=is_done,
                        updated_by_user_id=actor_user_id,
                    )
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount == 1:
                    fetch_stmt = (
                        select(ListItem)
                        .join(UserList, ListItem.list_id == UserList.id)
                        .where(ListItem.id == item_id, UserList.chat_id == chat_id)
                    )
                    fetch_res = await session.execute(fetch_stmt)
                    item = fetch_res.scalars().first()
                    await session.commit()
                    _normalize_item_datetimes(item)
                    logger.info(
                        "Transitioned list item %d (is_done=%s) in chat %d by actor %d",
                        item_id, is_done, chat_id, actor_user_id,
                    )
                    return item, True

                check_stmt = (
                    select(ListItem)
                    .join(UserList, ListItem.list_id == UserList.id)
                    .where(ListItem.id == item_id, UserList.chat_id == chat_id)
                )
                check_res = await session.execute(check_stmt)
                existing = check_res.scalars().first()
                if existing is None:
                    return None, False

                _normalize_item_datetimes(existing)
                return existing, False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None, False


async def delete_list_item(
    item_id: int,
    chat_id: int,
    actor_user_id: int,
) -> bool:
    _validate_item_id(item_id)
    _validate_chat_id(chat_id)
    _validate_user_id(actor_user_id)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    delete(ListItem)
                    .where(
                        ListItem.id == item_id,
                        ListItem.list_id.in_(
                            select(UserList.id).where(UserList.chat_id == chat_id)
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                if res.rowcount == 1:
                    await session.commit()
                    logger.info(
                        "Deleted list item %d in chat %d by actor %d",
                        item_id, chat_id, actor_user_id,
                    )
                    return True
                return False
        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return False


async def clear_done_list_items(
    list_id: int,
    chat_id: int,
    actor_user_id: int,
) -> int | None:
    _validate_list_id(list_id)
    _validate_chat_id(chat_id)
    _validate_user_id(actor_user_id)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                user_list = await session.get(UserList, list_id)
                if not user_list or user_list.chat_id != chat_id:
                    return None

                stmt = (
                    delete(ListItem)
                    .where(
                        ListItem.list_id == list_id,
                        ListItem.is_done.is_(True),
                        ListItem.list_id.in_(
                            select(UserList.id).where(
                                UserList.id == list_id,
                                UserList.chat_id == chat_id,
                            )
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt)
                deleted_count = res.rowcount
                await session.commit()
                logger.info(
                    "Cleared %d done items from list %d in chat %d by actor %d",
                    deleted_count, list_id, chat_id, actor_user_id,
                )
                return deleted_count

        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return None


async def delete_user_list(
    list_id: int,
    chat_id: int,
    actor_user_id: int,
) -> bool:
    _validate_list_id(list_id)
    _validate_chat_id(chat_id)
    _validate_user_id(actor_user_id)

    for attempt in range(5):
        try:
            async with AsyncSessionLocal() as session:
                user_list = await session.get(UserList, list_id)
                if not user_list or user_list.chat_id != chat_id:
                    return False

                stmt_items = (
                    delete(ListItem)
                    .where(
                        ListItem.list_id == list_id,
                        ListItem.list_id.in_(
                            select(UserList.id).where(
                                UserList.id == list_id,
                                UserList.chat_id == chat_id,
                            )
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.execute(stmt_items)

                stmt_list = (
                    delete(UserList)
                    .where(
                        UserList.id == list_id,
                        UserList.chat_id == chat_id,
                    )
                    .execution_options(synchronize_session=False)
                )
                res = await session.execute(stmt_list)
                if res.rowcount != 1:
                    await session.rollback()
                    return False

                await session.commit()
                logger.info(
                    "Deleted user list %d in chat %d by actor %d",
                    list_id, chat_id, actor_user_id,
                )
                return True

        except OperationalError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.01 * (attempt + 1))

    return False
