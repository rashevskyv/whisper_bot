import json
import html
import logging
import datetime
import re
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Set

from bot.utils.date_helper import calculate_future_date
from bot.utils.scheduler import scheduler_service
from bot.utils.scheduled_tasks import create_scheduled_tasks_batch
from bot.utils.search import perform_search, extract_source_links
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    update_action_draft_information,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
    DRAFT_STATUS_CONFIRMED,
    DRAFT_STATUS_CANCELLED,
    DRAFT_STATUS_EXPIRED,
)
from bot.utils.lists import (
    LIST_TYPE_SHOPPING,
    DEFAULT_SHOPPING_LIST_NAME,
    find_existing_user_list,
    get_list_item,
    get_user_list,
    list_list_items,
    resolve_user_list,
    add_list_items,
    set_list_item_done,
    delete_list_item,
    clear_done_list_items,
    delete_user_list,
)
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

# --- 1. Tool Schemas ---

CALCULATE_DATE_SCHEMA: Dict[str, Any] = {
    "name": "calculate_date",
    "description": "Convert LOCAL datetime string to UTC ISO.",
    "parameters": {
        "type": "object",
        "properties": {"local_datetime": {"type": "string"}},
        "required": ["local_datetime"]
    }
}

SCHEDULE_REMINDER_SCHEMA: Dict[str, Any] = {
    "name": "schedule_reminder",
    "description": "Schedule a reminder. Call this tool even if details like time or text are missing, as the application manages clarification.",
    "parameters": {
        "type": "object",
        "properties": {
            "iso_time_utc": {"type": "string"},
            "text": {"type": "string"}
        },
        "required": []
    }
}

DELETE_REMINDER_SCHEMA: Dict[str, Any] = {
    "name": "delete_reminder",
    "description": "Delete a reminder by ID. Call this tool even if the ID is missing, as the application manages clarification.",
    "parameters": {
        "type": "object",
        "properties": {"reminder_id": {"type": "integer"}},
        "required": []
    }
}

WEB_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "web_search",
    "description": "Search web.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }
}

CREATE_SCHEDULED_TASKS_SCHEMA: Dict[str, Any] = {
    "name": "create_scheduled_tasks",
    "description": (
        "Create one or several recurring medication or generic schedules. "
        "Include every recurring item from the current instruction in one call. "
        "Copy medication/task names and dosages from the user's instruction. "
        "Never diagnose, select a medication, or invent a dosage. "
        "If a required value is unknown, omit it rather than guessing. "
        "For a relative rule such as 'two hours after breakfast', supply "
        "relative_to='сніданок', offset_minutes=120, and omit reference_time if the user "
        "has not supplied breakfast time. "
        "days_of_week describes the days on which the concrete task or reference event occurs. "
        "Call the tool even when information is incomplete; the application owns clarification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "context_type": {
                "type": "string",
                "enum": ["medication", "generic"],
                "description": "Context type: 'medication' or 'generic'."
            },
            "items": {
                "type": "array",
                "description": "List of recurring medication or generic task items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the medication or task."
                        },
                        "details": {
                            "type": "string",
                            "description": "Optional details or notes."
                        },
                        "dosage": {
                            "type": "string",
                            "description": "Dosage (required for medication, e.g. '1 таблетка'). Do not guess."
                        },
                        "local_time": {
                            "type": "string",
                            "description": "Local wall-clock time in HH:MM format (e.g. '08:30')."
                        },
                        "days_of_week": {
                            "type": "array",
                            "description": "Days of week using Monday 0 through Sunday 6.",
                            "items": {
                                "type": "integer"
                            }
                        },
                        "relative_to": {
                            "type": "string",
                            "description": "Named reference event (e.g. 'сніданок')."
                        },
                        "offset_minutes": {
                            "type": "integer",
                            "description": "Offset in minutes relative to reference event (e.g. 120 or -30)."
                        },
                        "reference_time": {
                            "type": "string",
                            "description": "Local wall-clock time of the reference event in HH:MM format."
                        }
                    },
                    "required": []
                }
            }
        },
        "required": []
    }
}

SHOW_SHOPPING_LIST_SCHEMA: Dict[str, Any] = {
    "name": "show_shopping_list",
    "description": "Show shopping list items. Call this tool to view the current list.",
    "parameters": {
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Optional name of the list. Omit to view the default or single shopping list."
            }
        },
        "required": []
    }
}

ADD_SHOPPING_ITEMS_SCHEMA: Dict[str, Any] = {
    "name": "add_shopping_items",
    "description": (
        "Add one or several items to a shopping list. "
        "Call this tool even if details or items are missing; do not invent a list name. "
        "If list_name is not specified, omit it. "
        "The application handles clarification and confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Optional explicit name of the shopping list."
            },
            "items": {
                "type": "array",
                "description": "One or more item names to add.",
                "items": {
                    "type": "string"
                }
            }
        },
        "required": []
    }
}

SET_SHOPPING_ITEM_STATE_SCHEMA: Dict[str, Any] = {
    "name": "set_shopping_item_state",
    "description": (
        "Change the status of a shopping list item to 'done' (bought) or 'active' (not bought). "
        "Call this tool even if item_id or state is missing; the application handles clarification and confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "integer",
                "description": "Numeric ID of the shopping list item."
            },
            "state": {
                "type": "string",
                "enum": ["done", "active"],
                "description": "Target status: 'done' or 'active'."
            }
        },
        "required": []
    }
}

DELETE_SHOPPING_ITEM_SCHEMA: Dict[str, Any] = {
    "name": "delete_shopping_item",
    "description": (
        "Delete an item from a shopping list by its numeric ID. "
        "Call this tool even if item_id is missing; the application handles clarification and confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "integer",
                "description": "Numeric ID of the shopping list item to delete."
            }
        },
        "required": []
    }
}

CLEAR_BOUGHT_ITEMS_SCHEMA: Dict[str, Any] = {
    "name": "clear_bought_items",
    "description": (
        "Clear all bought (done) items from a shopping list. "
        "Call this tool even if list_name is missing. "
        "The application handles confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Optional explicit name of the shopping list."
            }
        },
        "required": []
    }
}

DELETE_SHOPPING_LIST_SCHEMA: Dict[str, Any] = {
    "name": "delete_shopping_list",
    "description": (
        "Delete an entire shopping list and all of its items. "
        "Use this tool when the user asks to delete the whole shopping list, "
        "not an individual item (use delete_shopping_item) and not just bought items (use clear_bought_items). "
        "Call this tool even if list_name is missing. "
        "The application handles confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "list_name": {
                "type": "string",
                "description": "Optional explicit name of the shopping list to delete."
            }
        },
        "required": []
    }
}

def get_tool_definitions(allow_search: bool = True) -> List[Dict[str, Any]]:
    """Повертає список описів дозволених інструментів."""
    tools = [
        CALCULATE_DATE_SCHEMA,
        SCHEDULE_REMINDER_SCHEMA,
        DELETE_REMINDER_SCHEMA,
        CREATE_SCHEDULED_TASKS_SCHEMA,
        SHOW_SHOPPING_LIST_SCHEMA,
        ADD_SHOPPING_ITEMS_SCHEMA,
        SET_SHOPPING_ITEM_STATE_SCHEMA,
        DELETE_SHOPPING_ITEM_SCHEMA,
        CLEAR_BOUGHT_ITEMS_SCHEMA,
        DELETE_SHOPPING_LIST_SCHEMA,
    ]
    if allow_search:
        tools.append(WEB_SEARCH_SCHEMA)
    return tools

def get_openai_tools(allow_search: bool = True) -> List[Dict[str, Any]]:
    """Повертає обгортку схем для OpenAI/OpenRouter API."""
    return [{"type": "function", "function": t} for t in get_tool_definitions(allow_search)]


_DAY_MAP: Dict[str, int] = {
    "пн": 0, "понеділок": 0, "mon": 0, "monday": 0, "0": 0,
    "вт": 1, "вівторок": 1, "tue": 1, "tues": 1, "tuesday": 1, "1": 1,
    "ср": 2, "середа": 2, "wed": 2, "wednesday": 2, "2": 2,
    "чт": 3, "четвер": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "3": 3,
    "пт": 4, "п'ятниця": 4, "п’ятниця": 4, "пятниця": 4, "fri": 4, "friday": 4, "4": 4,
    "сб": 5, "субота": 5, "sat": 5, "saturday": 5, "5": 5,
    "нд": 6, "неділя": 6, "sun": 6, "sunday": 6, "6": 6,
}

MEDICATION_SYNONYMS: Set[str] = {"ліки", "лікарство", "медикамент", "medication", "medicine"}
GENERIC_SYNONYMS: Set[str] = {"інше", "завдання", "нагадування", "generic", "task"}


def _canonicalize_time_str(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    s = val.strip()
    match = re.fullmatch(r"^(?:(?:[во]|об|у|[vo])\s+)?(\d{1,2})(?::(\d{2}))?$", s, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) is not None else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _normalize_days_of_week(val: Any) -> Optional[List[int]]:
    if not isinstance(val, (list, tuple)) or not val:
        return None
    res = set()
    for d in val:
        if not isinstance(d, int) or isinstance(d, bool):
            return None
        if not (0 <= d <= 6):
            return None
        res.add(d)
    return sorted(res) if res else None


def _normalize_offset_minutes(val: Any) -> Optional[int]:
    if not isinstance(val, int) or isinstance(val, bool):
        return None
    if not (-1440 <= val <= 1440):
        return None
    return val


def _calculate_relative_time_and_days(
    reference_time: str,
    offset_minutes: int,
    days_of_week: Optional[List[int]],
) -> Tuple[str, Optional[List[int]]]:
    h, m = map(int, reference_time.split(":"))
    base_dt = datetime.datetime(2000, 1, 3, h, m)  # Monday
    target_dt = base_dt + datetime.timedelta(minutes=offset_minutes)
    resolved_time = f"{target_dt.hour:02d}:{target_dt.minute:02d}"
    day_shift = (target_dt.date() - base_dt.date()).days
    resolved_days = None
    if days_of_week is not None:
        resolved_days = sorted(set((d + day_shift) % 7 for d in days_of_week))
    return resolved_time, resolved_days


def _parse_days_reply(text: str) -> Optional[List[int]]:
    s = text.strip().lower()
    if s in {"щодня", "кожен день", "кожного дня", "daily"}:
        return [0, 1, 2, 3, 4, 5, 6]
    if s in {"будні", "у будні", "по буднях", "weekdays"}:
        return [0, 1, 2, 3, 4]
    if s in {"вихідні", "у вихідні", "по вихідних", "weekends"}:
        return [5, 6]
    parts = [p for p in re.split(r"[,;\s]+", s) if p]
    if not parts:
        return None
    days = set()
    for p in parts:
        if p in _DAY_MAP:
            days.add(_DAY_MAP[p])
        else:
            return None
    return sorted(days) if days else None


def _normalize_create_scheduled_tasks_payload(
    raw_payload: Dict[str, Any],
    trusted_timezone: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    # Timezone must be derived and validated ONLY from the explicit trusted timezone argument.
    # Untrusted raw_payload["timezone"] is strictly ignored.
    canonical_tz = None
    if isinstance(trusted_timezone, str) and trusted_timezone.strip():
        try:
            zoneinfo.ZoneInfo(trusted_timezone.strip())
            canonical_tz = trusted_timezone.strip()
        except Exception:
            canonical_tz = None

    if canonical_tz is None:
        try:
            zoneinfo.ZoneInfo(BOT_TIMEZONE)
            canonical_tz = BOT_TIMEZONE
        except Exception:
            canonical_tz = "UTC"

    raw_context = raw_payload.get("context_type")
    clean_context = None
    if isinstance(raw_context, str):
        c_low = raw_context.strip().lower()
        if c_low in ("medication", "generic"):
            clean_context = c_low

    raw_items = raw_payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raw_items = [{}]

    normalized_items: List[Dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, dict):
            it = {}
        raw_name = it.get("name")
        name = str(raw_name).strip() if isinstance(raw_name, str) and raw_name.strip() else None

        raw_details = it.get("details")
        details = str(raw_details).strip() if isinstance(raw_details, str) and raw_details.strip() else None

        raw_dosage = it.get("dosage")
        dosage = str(raw_dosage).strip() if isinstance(raw_dosage, str) and raw_dosage.strip() else None

        days = _normalize_days_of_week(it.get("days_of_week"))
        local_time = _canonicalize_time_str(it.get("local_time"))

        raw_rel = it.get("relative_to")
        relative_to = str(raw_rel).strip() if isinstance(raw_rel, str) and raw_rel.strip() else None

        offset_minutes = _normalize_offset_minutes(it.get("offset_minutes"))
        reference_time = _canonicalize_time_str(it.get("reference_time"))

        is_relative = (relative_to is not None and offset_minutes is not None)

        resolved_local_time = None
        resolved_days_of_week = None

        if is_relative:
            if reference_time is not None:
                resolved_local_time, resolved_days_of_week = _calculate_relative_time_and_days(
                    reference_time, offset_minutes, days
                )
        else:
            resolved_local_time = local_time
            resolved_days_of_week = days

        normalized_items.append({
            "name": name,
            "details": details,
            "dosage": dosage,
            "days_of_week": days,
            "local_time": local_time,
            "relative_to": relative_to,
            "offset_minutes": offset_minutes,
            "reference_time": reference_time,
            "resolved_local_time": resolved_local_time,
            "resolved_days_of_week": resolved_days_of_week,
        })

    if not normalized_items:
        normalized_items = [{
            "name": None,
            "details": None,
            "dosage": None,
            "days_of_week": None,
            "local_time": None,
            "relative_to": None,
            "offset_minutes": None,
            "reference_time": None,
            "resolved_local_time": None,
            "resolved_days_of_week": None,
        }]

    missing_fields: List[str] = []
    if clean_context is None:
        missing_fields.append("context_type")

    seen_rel_events: Set[str] = set()
    for idx, it in enumerate(normalized_items):
        if it["name"] is None:
            missing_fields.append(f"item_name:{idx}")
        if clean_context == "medication" and it["dosage"] is None:
            missing_fields.append(f"item_dosage:{idx}")
        if it["days_of_week"] is None:
            missing_fields.append(f"item_days:{idx}")

        is_rel = (it["relative_to"] is not None and it["offset_minutes"] is not None)
        if is_rel and it["reference_time"] is None:
            norm_rel = it["relative_to"].strip().lower()
            if norm_rel not in seen_rel_events:
                seen_rel_events.add(norm_rel)
                missing_fields.append(f"reference_time:{idx}")
        elif not is_rel:
            if it["resolved_local_time"] is None:
                missing_fields.append(f"item_time:{idx}")

    clean_payload = {
        "context_type": clean_context,
        "timezone": canonical_tz,
        "items": normalized_items,
    }
    return clean_payload, missing_fields


# --- 2. Shared Execution Result ---

@dataclass(frozen=True)
class ToolResult:
    payload: Dict[str, Any]
    display_text: Optional[str] = None
    stop: bool = False
    source_urls: Tuple[str, ...] = ()
    draft_id: Optional[int] = None
    shopping_list_id: Optional[int] = None


# --- 3. Active Reminders Summary for Clock Metadata ---

async def get_active_reminders_summary(chat_id: int, timezone_name: str) -> str:
    """Повертає форматований рядок активних нагадувань для метаданих годинника."""
    return await scheduler_service.get_active_reminders_string(chat_id, timezone_name)


def format_draft_preview_or_question(
    action_type: str,
    payload: Dict[str, Any],
    missing_fields: List[str],
    timezone_name: Optional[str] = None,
) -> str:
    """
    Чистий детермінований форматувальник чернеток дій:
    - якщо missing_fields не порожній: повертає питання лише для missing_fields[0];
    - якщо missing_fields порожній: формує HTML-безпечний прев'ю підтвердження.
    """
    tz_str = timezone_name or BOT_TIMEZONE
    try:
        local_tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")

    if action_type == "schedule_reminder":
        if missing_fields:
            first_missing = missing_fields[0]
            if first_missing == "text":
                return "❓ Що саме вам нагадати?"
            elif first_missing == "iso_time_utc":
                return "❓ На коли встановити нагадування?"
            else:
                return "❓ Будь ласка, надайте необхідну інформацію."

        iso_str = payload.get("iso_time_utc")
        dt_utc = None
        if isinstance(iso_str, str) and iso_str.strip():
            clean_iso = iso_str.strip()
            if clean_iso.endswith("Z") or clean_iso.endswith("z"):
                clean_iso = clean_iso[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(clean_iso)
                if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
                    dt_utc = dt.astimezone(datetime.timezone.utc)
            except Exception:
                dt_utc = None

        raw_text = payload.get("text", "")
        safe_text = html.escape(str(raw_text))

        if dt_utc:
            l_dt = dt_utc.astimezone(local_tz)
            days = {
                "Monday": "Пн",
                "Tuesday": "Вт",
                "Wednesday": "Ср",
                "Thursday": "Чт",
                "Friday": "Пт",
                "Saturday": "Сб",
                "Sunday": "Нд",
            }
            d_name = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
            time_str = f"{d_name}, {l_dt.strftime('%d.%m %H:%M')}"
        else:
            time_str = "невідомий час"

        return (
            f"\n📋 <b>Підтвердження нагадування:</b>\n"
            f"🕒 {time_str}\n"
            f"📝 <i>{safe_text}</i>\n\n"
            f"⚠️ <i>Потрібне підтвердження.</i>"
        )

    elif action_type == "delete_reminder":
        if missing_fields:
            return "❓ Яке саме нагадування ви хочете видалити? Вкажіть його номер."

        rem_id = payload.get("reminder_id")
        return (
            f"\n🗑 <b>Підтвердження видалення:</b>\n"
            f"Нагадування #{rem_id}\n\n"
            f"⚠️ <i>Потрібне підтвердження для видалення.</i>"
        )

    elif action_type == "create_scheduled_tasks":
        if missing_fields:
            first_missing = missing_fields[0]
            if first_missing == "context_type":
                return "❓ Це розклад ліків чи інше повторюване завдання?"
            elif first_missing.startswith("item_name:"):
                try:
                    idx = int(first_missing.split(":")[1])
                except (ValueError, IndexError):
                    idx = 0
                items = payload.get("items") or []
                if len(items) > 1:
                    return f"❓ Як називається ліки чи завдання для пункту {idx + 1}?"
                return "❓ Як називається ліки чи завдання?"
            elif first_missing.startswith("item_dosage:"):
                try:
                    idx = int(first_missing.split(":")[1])
                except (ValueError, IndexError):
                    idx = 0
                items = payload.get("items") or []
                item = items[idx] if idx < len(items) else {}
                safe_name = html.escape(str(item.get("name") or f"пункту {idx + 1}"))
                return (
                    f"❓ Вкажіть точне дозування для <b>{safe_name}</b> згідно з інструкцією "
                    f"лікаря (бот не обирає та не призначає дозування самостійно)."
                )
            elif first_missing.startswith("item_days:"):
                try:
                    idx = int(first_missing.split(":")[1])
                except (ValueError, IndexError):
                    idx = 0
                items = payload.get("items") or []
                item = items[idx] if idx < len(items) else {}
                safe_name = html.escape(str(item.get("name") or f"пункту {idx + 1}"))
                return (
                    f"❓ У які дні повторювати <b>{safe_name}</b>? "
                    f"(наприклад: щодня, будні, Пн, Ср, Пт)"
                )
            elif first_missing.startswith("item_time:"):
                try:
                    idx = int(first_missing.split(":")[1])
                except (ValueError, IndexError):
                    idx = 0
                items = payload.get("items") or []
                item = items[idx] if idx < len(items) else {}
                safe_name = html.escape(str(item.get("name") or f"пункту {idx + 1}"))
                return (
                    f"❓ О котрій годині виконувати <b>{safe_name}</b>? "
                    f"Вкажіть локальний час (наприклад: 08:30)."
                )
            elif first_missing.startswith("reference_time:"):
                try:
                    idx = int(first_missing.split(":")[1])
                except (ValueError, IndexError):
                    idx = 0
                items = payload.get("items") or []
                item = items[idx] if idx < len(items) else {}
                safe_rel = html.escape(str(item.get("relative_to") or "подія"))
                return f"❓ О котрій у вас {safe_rel}?"
            else:
                return "❓ Будь ласка, надайте необхідну інформацію."

        # Preview (missing_fields is empty)
        c_type = payload.get("context_type")
        c_label = "Ліки" if c_type == "medication" else "Завдання"
        items = payload.get("items") or []
        canon_tz = timezone_name
        if not canon_tz or not isinstance(canon_tz, str):
            canon_tz = payload.get("timezone")
        try:
            if canon_tz:
                zoneinfo.ZoneInfo(str(canon_tz).strip())
                canon_tz = str(canon_tz).strip()
            else:
                canon_tz = None
        except Exception:
            canon_tz = None

        if canon_tz is None:
            try:
                zoneinfo.ZoneInfo(BOT_TIMEZONE)
                canon_tz = BOT_TIMEZONE
            except Exception:
                canon_tz = "UTC"

        tz_display = html.escape(str(canon_tz))

        ukr_days = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Нд"}

        lines = [
            "\n📋 <b>Підтвердження розкладу:</b>",
            f"Тип: <b>{c_label}</b>",
            f"Часовий пояс: <code>{tz_display}</code>\n",
        ]

        for i, it in enumerate(items, 1):
            name_str = html.escape(str(it.get("name") or f"Завдання {i}"))
            lines.append(f"<b>{i}. {name_str}</b>")
            if c_type == "medication" and it.get("dosage"):
                safe_dosage = html.escape(str(it["dosage"]))
                lines.append(f"  💊 Дозування: <i>{safe_dosage}</i>")
            if it.get("details"):
                safe_details = html.escape(str(it["details"]))
                lines.append(f"  ℹ️ Деталі: <i>{safe_details}</i>")

            res_time = it.get("resolved_local_time") or it.get("local_time") or "00:00"
            res_days = it.get("resolved_days_of_week") or it.get("days_of_week") or []
            res_days_str = ", ".join(ukr_days.get(d, str(d)) for d in res_days)

            if it.get("relative_to") and it.get("offset_minutes") is not None:
                safe_rel = html.escape(str(it["relative_to"]))
                offset = it["offset_minutes"]
                ref_time = it.get("reference_time", "")
                src_days = it.get("days_of_week") or []
                if src_days != res_days:
                    src_days_str = ", ".join(ukr_days.get(d, str(d)) for d in src_days)
                    lines.append(f"  📅 Дні події: {src_days_str} → Дні виконання: {res_days_str}")
                else:
                    lines.append(f"  📅 Дні: {res_days_str}")
                lines.append(f"  🕒 Час: {res_time} ({offset:+d} хв від «{safe_rel}» о {ref_time})")
            else:
                lines.append(f"  🕒 Час: {res_time}")
                lines.append(f"  📅 Дні: {res_days_str}")
            lines.append("")

        lines.append("ℹ️ <i>Бот лише структурує вказану вами інструкцію та не обирає ліки чи дозування.</i>")
        lines.append("⚠️ <i>Потрібне підтвердження.</i>")
        return "\n".join(lines).strip()

    elif action_type == "add_shopping_items":
        if missing_fields:
            return "❓ Що додати до списку покупок?"

        target_name = payload.get("list_name") or DEFAULT_SHOPPING_LIST_NAME
        safe_name = html.escape(_safe_truncate_raw(target_name, 150))
        items = payload.get("items") or []
        count = len(items)
        header_lines = [
            "\n📋 <b>Підтвердження додавання до списку:</b>",
            f"Список: <b>{safe_name}</b>",
            f"Кількість пунктів: <b>{count}</b>\n",
        ]
        static_footer = "\n⚠️ <i>Потрібне підтвердження.</i>"

        lines = list(header_lines)
        added_count = 0
        truncated = False

        for it in items:
            safe_it = html.escape(_safe_truncate_raw(it, 200))
            line = f"• {safe_it}"
            remaining = count - (added_count + 1)
            footer = f"… ще {remaining + 1} пунктів" if remaining >= 0 else ""
            footer_space = len(footer) + 1 if footer else 0
            current_len = sum(len(l) + 1 for l in lines)
            if current_len + len(line) + footer_space + len(static_footer) > SHOPPING_DISPLAY_LIMIT:
                truncated = True
                break
            lines.append(line)
            added_count += 1

        if truncated:
            remaining = count - added_count
            lines.append(f"… ще {remaining} пунктів")

        lines.append(static_footer)
        return "\n".join(lines).strip()

    elif action_type == "set_shopping_item_state":
        if missing_fields:
            first_missing = missing_fields[0]
            if first_missing == "item_id":
                return "❓ Вкажіть номер пункту, стан якого потрібно змінити."
            elif first_missing == "state":
                return "❓ Позначити пункт купленим чи повернути до активних?"
            else:
                return "❓ Будь ласка, надайте необхідну інформацію."

        item_id = payload.get("item_id")
        st = payload.get("state")
        st_label = "куплено" if st == "done" else "повернено до активних"
        return (
            f"\n📋 <b>Підтвердження зміни стану:</b>\n"
            f"Пункт: #{item_id}\n"
            f"Новий стан: <b>{st_label}</b>\n\n"
            f"⚠️ <i>Потрібне підтвердження.</i>"
        )

    elif action_type == "delete_shopping_item":
        if missing_fields:
            return "❓ Вкажіть номер пункту, який потрібно видалити."

        item_id = payload.get("item_id")
        return (
            f"\n🗑 <b>Підтвердження видалення:</b>\n"
            f"Пункт #{item_id} буде видалено зі списку.\n\n"
            f"⚠️ <i>Потрібне підтвердження для видалення.</i>"
        )

    elif action_type == "clear_bought_items":
        if missing_fields:
            return "❓ Будь ласка, надайте необхідну інформацію."

        target_name = payload.get("list_name") or DEFAULT_SHOPPING_LIST_NAME
        safe_name = html.escape(_safe_truncate_raw(target_name, 150))
        return (
            f"\n🗑 <b>Підтвердження очищення:</b>\n"
            f"Список: <b>{safe_name}</b>\n"
            f"Усі куплені пункти цього списку будуть видалені.\n\n"
            f"⚠️ <i>Потрібне підтвердження для очищення.</i>"
        )

    elif action_type == "delete_shopping_list":
        if missing_fields:
            return "❓ Будь ласка, надайте необхідну інформацію."

        target_name = payload.get("list_name") or DEFAULT_SHOPPING_LIST_NAME
        safe_name = html.escape(_safe_truncate_raw(target_name, 150))
        return (
            f"\n🗑 <b>Підтвердження видалення списку</b>\n"
            f"Список: <b>{safe_name}</b>\n"
            f"Увесь список і всі його пункти буде видалено.\n\n"
            f"⚠️ <i>Потрібне підтвердження для видалення.</i>"
        )

    return "⚠️ Невідомий або непідтримуваний тип дії."


def _clean_optional_string(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    cleaned = re.sub(r"\s+", " ", val.strip())
    return cleaned if cleaned else None


def _clean_items_list(val: Any) -> List[str]:
    if not isinstance(val, (list, tuple)):
        return []
    res: List[str] = []
    for it in val:
        if isinstance(it, str):
            c = re.sub(r"\s+", " ", it.strip())
            if c:
                res.append(c)
    return res


def _clean_item_id(val: Any) -> Optional[int]:
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return val
    return None


def _clean_state(val: Any) -> Optional[str]:
    if isinstance(val, str):
        c = val.strip().lower()
        if c in ("done", "active"):
            return c
    return None


SHOPPING_DISPLAY_LIMIT: int = 3500


def _safe_truncate_raw(text: Any, max_chars: int = 150) -> str:
    """Обрізає сирий динамічний текст до безпечної довжини перед HTML-escaping."""
    s = str(text) if text is not None else ""
    if len(s) > max_chars:
        return s[:max_chars].rstrip() + "…"
    return s


def format_shopping_list_view(title: str, items: List[Any]) -> str:
    safe_title = html.escape(_safe_truncate_raw(title, 150))
    if not items:
        return f"🛒 <b>{safe_title}</b>\n\nСписок порожній."

    lines = [f"🛒 <b>{safe_title}</b>\n"]
    total_items = len(items)
    added_count = 0
    truncated = False

    for it in items:
        checkbox = "✅" if it.is_done else "☐"
        safe_text = html.escape(_safe_truncate_raw(str(it.text), 200))
        line = f"{checkbox} #{it.id} {safe_text}"
        remaining = total_items - (added_count + 1)
        footer = f"\n… ще {remaining + 1} пунктів" if remaining >= 0 else ""
        current_len = sum(len(l) + 1 for l in lines)
        if current_len + len(line) + len(footer) > SHOPPING_DISPLAY_LIMIT:
            truncated = True
            break
        lines.append(line)
        added_count += 1

    if truncated:
        remaining = total_items - added_count
        lines.append(f"\n… ще {remaining} пунктів")

    return "\n".join(lines).strip()


_format_shopping_list_view = format_shopping_list_view


# --- 4. Shared Tool Executor ---

async def execute_tool(
    name: str,
    args: Any,
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    timezone_name: Optional[str] = None,
    source_message_id: Optional[int] = None,
    execute_mutation: bool = False,
) -> ToolResult:
    """
    Валідує вхідні дані та виконує зареєстровані інструменти (shared executor для tool definitions).
    Повертає ToolResult зі структурованим результатом або помилкою.
    """
    tz_str = timezone_name or BOT_TIMEZONE

    # Parse and validate args dictionary
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return ToolResult(payload={"success": False, "error": "Malformed JSON in tool arguments"}, stop=False)

    if not isinstance(args, dict):
        return ToolResult(payload={"success": False, "error": "Tool arguments must be a dictionary"}, stop=False)

    # 1. calculate_date
    if name == "calculate_date":
        local_dt = args.get("local_datetime")
        if not isinstance(local_dt, str) or not local_dt.strip():
            return ToolResult(
                payload={"success": False, "error": "local_datetime is required and must be a non-empty string"},
                stop=False
            )
        iso_res = calculate_future_date(local_dt.strip(), tz_str)
        if not iso_res or iso_res.startswith("Error"):
            return ToolResult(
                payload={"success": False, "error": iso_res or "Invalid date format"},
                stop=False
            )
        return ToolResult(
            payload={"success": True, "iso_time_utc": iso_res},
            stop=False
        )

    # 2. schedule_reminder
    elif name == "schedule_reminder":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False
            )

        if execute_mutation:
            # Trusted direct execution branch (reserved for confirmed draft callback)
            iso_time_utc = args.get("iso_time_utc")
            if not isinstance(iso_time_utc, str) or not iso_time_utc.strip():
                return ToolResult(
                    payload={"success": False, "error": "iso_time_utc is required and must be a non-empty string"},
                    stop=False
                )

            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                return ToolResult(
                    payload={"success": False, "error": "text is required and must be a non-empty string"},
                    stop=False
                )

            raw_iso = iso_time_utc.strip()
            if raw_iso.endswith("Z") or raw_iso.endswith("z"):
                raw_iso = raw_iso[:-1] + "+00:00"

            try:
                dt = datetime.datetime.fromisoformat(raw_iso)
            except Exception as e:
                return ToolResult(
                    payload={"success": False, "error": f"Malformed ISO datetime: {e}"},
                    stop=False
                )

            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                return ToolResult(
                    payload={"success": False, "error": "Timezone-naive datetime is rejected"},
                    stop=False
                )

            dt_utc = dt.astimezone(datetime.timezone.utc)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if dt_utc <= now_utc:
                return ToolResult(
                    payload={"success": False, "error": "Scheduled time must be in the future"},
                    stop=False
                )

            try:
                rem_id = await scheduler_service.add_reminder(user_id, chat_id, text.strip(), dt_utc)
            except Exception as e:
                logger.error("Failed to add reminder: %s", e)
                return ToolResult(
                    payload={"success": False, "error": "reminder_create_failed"},
                    stop=False
                )

            try:
                local_tz = zoneinfo.ZoneInfo(tz_str)
            except Exception:
                local_tz = zoneinfo.ZoneInfo("UTC")

            l_dt = dt_utc.astimezone(local_tz)
            days = {"Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср", "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"}
            d_name = days.get(l_dt.strftime("%A"), l_dt.strftime("%a"))
            safe_text = html.escape(text.strip())
            display_text = f"\n✅ <b>Встановлено:</b> {d_name}, {l_dt.strftime('%d.%m %H:%M')}\n📝 <i>{safe_text}</i>"

            return ToolResult(
                payload={"success": True, "reminder_id": rem_id},
                display_text=display_text,
                stop=True
            )

        # Default draft interception mode (execute_mutation=False)
        raw_text = args.get("text")
        valid_text: Optional[str] = None
        if isinstance(raw_text, str) and raw_text.strip():
            valid_text = raw_text.strip()

        raw_iso = args.get("iso_time_utc")
        valid_iso_utc: Optional[str] = None
        parsed_dt_utc: Optional[datetime.datetime] = None

        if isinstance(raw_iso, str) and raw_iso.strip():
            clean_iso = raw_iso.strip()
            if clean_iso.endswith("Z") or clean_iso.endswith("z"):
                clean_iso = clean_iso[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(clean_iso)
                if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
                    dt_utc = dt.astimezone(datetime.timezone.utc)
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    if dt_utc > now_utc:
                        valid_iso_utc = dt_utc.isoformat()
                        parsed_dt_utc = dt_utc
            except Exception:
                pass

        missing_fields: List[str] = []
        draft_payload: Dict[str, Any] = {}
        if valid_text is not None:
            draft_payload["text"] = valid_text
        else:
            missing_fields.append("text")

        if valid_iso_utc is not None:
            draft_payload["iso_time_utc"] = valid_iso_utc
        else:
            missing_fields.append("iso_time_utc")

        draft = await create_action_draft(
            user_id=user_id,
            chat_id=chat_id,
            action_type="schedule_reminder",
            payload=draft_payload,
            missing_fields=missing_fields,
            source_message_id=source_message_id,
        )

        display_text = format_draft_preview_or_question(
            action_type="schedule_reminder",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "schedule_reminder",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 3. delete_reminder
    elif name == "delete_reminder":
        if not chat_id:
            return ToolResult(
                payload={"success": False, "error": "chat_id is required"},
                stop=False
            )

        if execute_mutation:
            # Trusted direct execution branch (reserved for confirmed draft callback)
            raw_rem_id = args.get("reminder_id")
            if raw_rem_id is None or isinstance(raw_rem_id, bool):
                return ToolResult(
                    payload={"success": False, "error": "reminder_id must be a positive integer"},
                    stop=False
                )

            if isinstance(raw_rem_id, int):
                rem_id = raw_rem_id
            elif isinstance(raw_rem_id, str) and raw_rem_id.isdigit():
                rem_id = int(raw_rem_id)
            else:
                return ToolResult(
                    payload={"success": False, "error": "reminder_id must be a positive integer"},
                    stop=False
                )

            if rem_id <= 0:
                return ToolResult(
                    payload={"success": False, "error": "reminder_id must be a positive integer"},
                    stop=False
                )

            success = await scheduler_service.delete_reminder_by_id(rem_id, chat_id=chat_id)
            if success:
                return ToolResult(payload={"success": True}, stop=False)
            else:
                return ToolResult(payload={"success": False, "error": "not_found"}, stop=False)

        # Default draft interception mode (execute_mutation=False)
        if not user_id:
            return ToolResult(
                payload={"success": False, "error": "user_id is required"},
                stop=False
            )

        raw_rem_id = args.get("reminder_id")
        rem_id: Optional[int] = None
        if raw_rem_id is not None and not isinstance(raw_rem_id, bool):
            if isinstance(raw_rem_id, int) and raw_rem_id > 0:
                rem_id = raw_rem_id
            elif isinstance(raw_rem_id, str) and raw_rem_id.isdigit():
                val = int(raw_rem_id)
                if val > 0:
                    rem_id = val

        if rem_id is not None:
            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="delete_reminder",
                payload={"reminder_id": rem_id},
                missing_fields=[],
                source_message_id=source_message_id,
            )
        else:
            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="delete_reminder",
                payload={},
                missing_fields=["reminder_id"],
                source_message_id=source_message_id,
            )

        display_text = format_draft_preview_or_question(
            action_type="delete_reminder",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "delete_reminder",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 4. web_search
    elif name == "web_search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                payload={"success": False, "error": "query is required and must be a non-empty string"},
                stop=False
            )

        raw_search_res = await perform_search(query.strip())
        extracted = extract_source_links(str(raw_search_res))
        source_urls: List[str] = []
        for link in extracted:
            if link not in source_urls:
                source_urls.append(link)
            if len(source_urls) == 5:
                break

        return ToolResult(
            payload={"results": str(raw_search_res)},
            display_text="\n🔎 <i>Шукаю...</i>\n",
            stop=False,
            source_urls=tuple(source_urls)
        )

    # 5. create_scheduled_tasks
    elif name == "create_scheduled_tasks":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False
            )

        if execute_mutation:
            # Trusted direct execution branch (reserved for confirmed draft callback)
            clean_payload, missing = _normalize_create_scheduled_tasks_payload(args, tz_str)
            if missing or not clean_payload.get("items"):
                return ToolResult(
                    payload={"success": False, "error": "incomplete_or_invalid_payload"},
                    stop=False
                )

            clean_context = clean_payload.get("context_type")
            if clean_context not in ("medication", "generic"):
                return ToolResult(
                    payload={"success": False, "error": "incomplete_or_invalid_payload"},
                    stop=False
                )

            clean_tz = clean_payload["timezone"]

            try:
                tasks = await create_scheduled_tasks_batch(
                    user_id=user_id,
                    chat_id=chat_id,
                    context_type=clean_context,
                    timezone_name=clean_tz,
                    items=clean_payload["items"],
                )
            except Exception:
                logger.error(
                    f"Database error during batch task creation for user {user_id}, chat {chat_id}, context {clean_context}"
                )
                return ToolResult(
                    payload={"success": False, "error": "database_error"},
                    stop=False
                )

            scheduler_errors = 0
            for task in tasks:
                try:
                    scheduler_service.schedule_recurring_task(task)
                except Exception:
                    scheduler_errors += 1
                    logger.error(
                        f"Scheduler registration failed for task {task.id}, user {user_id}, chat {chat_id}"
                    )

            count = len(tasks)
            task_ids = [t.id for t in tasks]
            if scheduler_errors == 0:
                display_text = f"\n✅ <b>Успішно створено {count} розклад(ів)!</b>"
            else:
                display_text = f"\n✅ <b>Створено {count} розклад(ів).</b> (Деякі завдання будуть активовані після перезапуску бота)"

            logger.info(
                f"Executed create_scheduled_tasks mutation: user_id={user_id}, chat_id={chat_id}, "
                f"context_type={clean_context}, count={count}, scheduler_errors={scheduler_errors}"
            )

            return ToolResult(
                payload={
                    "success": True,
                    "task_ids": task_ids,
                    "count": count,
                    "scheduler_errors": scheduler_errors,
                },
                display_text=display_text,
                stop=True
            )

        # Default draft interception mode (execute_mutation=False)
        clean_payload, missing_fields = _normalize_create_scheduled_tasks_payload(args, tz_str)

        draft = await create_action_draft(
            user_id=user_id,
            chat_id=chat_id,
            action_type="create_scheduled_tasks",
            payload=clean_payload,
            missing_fields=missing_fields,
            source_message_id=source_message_id,
        )

        display_text = format_draft_preview_or_question(
            action_type="create_scheduled_tasks",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=clean_payload["timezone"],
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "create_scheduled_tasks",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 6. show_shopping_list
    elif name == "show_shopping_list":
        if not chat_id:
            return ToolResult(
                payload={"success": False, "error": "chat_id is required"},
                stop=False,
            )

        raw_list_name = _clean_optional_string(args.get("list_name"))
        try:
            target_list = await find_existing_user_list(chat_id, raw_list_name, LIST_TYPE_SHOPPING)

            if target_list is None:
                display_title = raw_list_name if raw_list_name else DEFAULT_SHOPPING_LIST_NAME
                display_text = format_shopping_list_view(display_title, [])
                return ToolResult(
                    payload={"success": True, "list_id": None, "items": []},
                    display_text=display_text,
                    stop=True,
                    shopping_list_id=None,
                )

            items = await list_list_items(target_list.id, chat_id)
        except Exception:
            logger.error(
                f"Database error in show_shopping_list for user {user_id}, chat {chat_id}"
            )
            return ToolResult(
                payload={"success": False, "error": "database_error"},
                display_text="⚠️ Не вдалося прочитати список покупок. Спробуйте ще раз.",
                stop=True,
                shopping_list_id=None,
            )

        display_text = format_shopping_list_view(target_list.name, items or [])
        return ToolResult(
            payload={"success": True, "list_id": target_list.id, "count": len(items or [])},
            display_text=display_text,
            stop=True,
            shopping_list_id=target_list.id,
        )

    # 7. add_shopping_items
    elif name == "add_shopping_items":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False,
            )

        if execute_mutation:
            clean_name = _clean_optional_string(args.get("list_name"))
            clean_items = _clean_items_list(args.get("items"))
            if not clean_items:
                return ToolResult(
                    payload={"success": False, "error": "items_required"},
                    stop=True,
                )

            target_list_id = args.get("list_id")
            try:
                target_list = None
                if (
                    target_list_id is not None
                    and isinstance(target_list_id, int)
                    and not isinstance(target_list_id, bool)
                    and target_list_id > 0
                ):
                    target_list = await get_user_list(target_list_id, chat_id)

                if target_list is None:
                    target_list, _ = await resolve_user_list(
                        user_id=user_id,
                        chat_id=chat_id,
                        list_type=LIST_TYPE_SHOPPING,
                        explicit_name=clean_name,
                    )

                created_items = await add_list_items(target_list.id, chat_id, user_id, clean_items)
            except Exception:
                logger.error(
                    f"Database error in add_shopping_items mutation for user {user_id}, chat {chat_id}"
                )
                return ToolResult(
                    payload={"success": False, "error": "database_error"},
                    display_text="❌ Помилка при додаванні пунктів до списку.",
                    stop=True,
                )

            if created_items is None:
                return ToolResult(
                    payload={"success": False, "error": "list_not_found"},
                    display_text="❌ Список не знайдено.",
                    stop=True,
                )

            item_ids = [it.id for it in created_items]
            count = len(created_items)
            safe_name = html.escape(_safe_truncate_raw(target_list.name, 150))
            display_text = f"\n✅ <b>Додано {count} пункт(ів) до списку «{safe_name}»!</b>"
            logger.info(
                f"Executed add_shopping_items mutation: user_id={user_id}, chat_id={chat_id}, list_id={target_list.id}, count={count}"
            )
            return ToolResult(
                payload={"success": True, "list_id": target_list.id, "item_ids": item_ids, "count": count},
                display_text=display_text,
                stop=True,
            )

        # Default draft interception mode (execute_mutation=False)
        clean_name = _clean_optional_string(args.get("list_name"))
        clean_items = _clean_items_list(args.get("items"))

        try:
            existing = await find_existing_user_list(chat_id, clean_name, LIST_TYPE_SHOPPING)
            draft_payload: Dict[str, Any] = {}
            if existing is not None:
                draft_payload["list_id"] = existing.id
                draft_payload["list_name"] = existing.name
            else:
                draft_payload["list_name"] = clean_name if clean_name else DEFAULT_SHOPPING_LIST_NAME

            missing_fields: List[str] = []
            if clean_items:
                draft_payload["items"] = clean_items
            else:
                missing_fields.append("items")

            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="add_shopping_items",
                payload=draft_payload,
                missing_fields=missing_fields,
                source_message_id=source_message_id,
            )
        except Exception:
            logger.error(
                f"Database error in add_shopping_items draft preparation for user {user_id}, chat {chat_id}"
            )
            return ToolResult(
                payload={"success": False, "error": "database_error"},
                display_text="⚠️ Не вдалося підготувати дію. Спробуйте ще раз.",
                stop=True,
            )

        display_text = format_draft_preview_or_question(
            action_type="add_shopping_items",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "add_shopping_items",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 8. set_shopping_item_state
    elif name == "set_shopping_item_state":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False,
            )

        if execute_mutation:
            item_id = _clean_item_id(args.get("item_id"))
            state_val = _clean_state(args.get("state"))
            if not item_id or not state_val:
                return ToolResult(
                    payload={"success": False, "error": "invalid_payload"},
                    display_text="❌ Некоректні дані дії.",
                    stop=True,
                )

            is_done = (state_val == "done")
            try:
                item, changed = await set_list_item_done(item_id, chat_id, user_id, is_done)
            except Exception:
                logger.error(
                    f"Database error in set_shopping_item_state mutation for user {user_id}, chat {chat_id}"
                )
                return ToolResult(
                    payload={"success": False, "error": "database_error"},
                    display_text="❌ Помилка при зміні стану пункту.",
                    stop=True,
                )

            if item is None:
                return ToolResult(
                    payload={"success": False, "error": "item_not_found"},
                    display_text="❌ Пункт не знайдено або він не належить цьому чату.",
                    stop=True,
                )

            st_label = "купленим" if is_done else "активним"
            if changed:
                display_text = f"\n✅ Пункт #{item.id} позначено {st_label}."
            else:
                display_text = f"\nℹ️ Пункт #{item.id} вже був позначений {st_label}."

            logger.info(
                f"Executed set_shopping_item_state mutation: user_id={user_id}, chat_id={chat_id}, item_id={item.id}, is_done={is_done}, changed={changed}"
            )
            return ToolResult(
                payload={"success": True, "item_id": item.id, "is_done": item.is_done, "changed": changed},
                display_text=display_text,
                stop=True,
            )

        # Default draft interception mode (execute_mutation=False)
        raw_item_id = args.get("item_id")
        item_id = _clean_item_id(raw_item_id)
        raw_state = args.get("state")
        state_val = _clean_state(raw_state)

        draft_payload: Dict[str, Any] = {}
        missing_fields: List[str] = []

        try:
            if item_id is not None:
                existing_item = await get_list_item(item_id, chat_id)
                if existing_item is not None:
                    draft_payload["item_id"] = item_id
                else:
                    missing_fields.append("item_id")
            else:
                missing_fields.append("item_id")

            if state_val is not None:
                draft_payload["state"] = state_val
            else:
                missing_fields.append("state")

            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="set_shopping_item_state",
                payload=draft_payload,
                missing_fields=missing_fields,
                source_message_id=source_message_id,
            )
        except Exception:
            logger.error(
                f"Database error in set_shopping_item_state draft preparation for user {user_id}, chat {chat_id}"
            )
            return ToolResult(
                payload={"success": False, "error": "database_error"},
                display_text="⚠️ Не вдалося підготувати дію. Спробуйте ще раз.",
                stop=True,
            )

        display_text = format_draft_preview_or_question(
            action_type="set_shopping_item_state",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "set_shopping_item_state",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 9. delete_shopping_item
    elif name == "delete_shopping_item":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False,
            )

        if execute_mutation:
            item_id = _clean_item_id(args.get("item_id"))
            if not item_id:
                return ToolResult(
                    payload={"success": False, "error": "invalid_payload"},
                    display_text="❌ Некоректні дані дії.",
                    stop=True,
                )

            try:
                deleted = await delete_list_item(item_id, chat_id, user_id)
            except Exception:
                logger.error(
                    f"Database error in delete_shopping_item mutation for user {user_id}, chat {chat_id}"
                )
                return ToolResult(
                    payload={"success": False, "error": "database_error"},
                    display_text="❌ Помилка при видаленні пункту.",
                    stop=True,
                )

            if not deleted:
                return ToolResult(
                    payload={"success": False, "error": "item_not_found"},
                    display_text="❌ Пункт не знайдено або він не належить цьому чату.",
                    stop=True,
                )

            logger.info(
                f"Executed delete_shopping_item mutation: user_id={user_id}, chat_id={chat_id}, item_id={item_id}"
            )
            return ToolResult(
                payload={"success": True, "item_id": item_id},
                display_text=f"\n🗑 Пункт #{item_id} успішно видалено.",
                stop=True,
            )

        # Default draft interception mode (execute_mutation=False)
        raw_item_id = args.get("item_id")
        item_id = _clean_item_id(raw_item_id)

        draft_payload: Dict[str, Any] = {}
        missing_fields: List[str] = []

        try:
            if item_id is not None:
                existing_item = await get_list_item(item_id, chat_id)
                if existing_item is not None:
                    draft_payload["item_id"] = item_id
                else:
                    missing_fields.append("item_id")
            else:
                missing_fields.append("item_id")

            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="delete_shopping_item",
                payload=draft_payload,
                missing_fields=missing_fields,
                source_message_id=source_message_id,
            )
        except Exception:
            logger.error(
                f"Database error in delete_shopping_item draft preparation for user {user_id}, chat {chat_id}"
            )
            return ToolResult(
                payload={"success": False, "error": "database_error"},
                display_text="⚠️ Не вдалося підготувати дію. Спробуйте ще раз.",
                stop=True,
            )

        display_text = format_draft_preview_or_question(
            action_type="delete_shopping_item",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "delete_shopping_item",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 10. clear_bought_items
    elif name == "clear_bought_items":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False,
            )

        if execute_mutation:
            clean_name = _clean_optional_string(args.get("list_name"))
            target_list_id = args.get("list_id")
            try:
                target_list = None
                if (
                    target_list_id is not None
                    and isinstance(target_list_id, int)
                    and not isinstance(target_list_id, bool)
                    and target_list_id > 0
                ):
                    target_list = await get_user_list(target_list_id, chat_id)

                if target_list is None:
                    target_list = await find_existing_user_list(chat_id, clean_name, LIST_TYPE_SHOPPING)

                if target_list is None:
                    target_list, _ = await resolve_user_list(
                        user_id=user_id,
                        chat_id=chat_id,
                        list_type=LIST_TYPE_SHOPPING,
                        explicit_name=clean_name,
                    )

                deleted_count = await clear_done_list_items(target_list.id, chat_id, user_id)
            except Exception:
                logger.error(
                    f"Database error in clear_bought_items mutation for user {user_id}, chat {chat_id}"
                )
                return ToolResult(
                    payload={"success": False, "error": "database_error"},
                    display_text="❌ Помилка при очищенні куплених пунктів.",
                    stop=True,
                )

            if deleted_count is None:
                return ToolResult(
                    payload={"success": False, "error": "list_not_found"},
                    display_text="❌ Список не знайдено.",
                    stop=True,
                )

            safe_name = html.escape(_safe_truncate_raw(target_list.name, 150))
            if deleted_count == 0:
                display_text = f"\nℹ️ У списку «{safe_name}» немає куплених пунктів."
            else:
                display_text = f"\n🗑 Видалено {deleted_count} куплених пункт(ів) зі списку «{safe_name}»."

            logger.info(
                f"Executed clear_bought_items mutation: user_id={user_id}, chat_id={chat_id}, list_id={target_list.id}, deleted_count={deleted_count}"
            )
            return ToolResult(
                payload={"success": True, "list_id": target_list.id, "deleted_count": deleted_count},
                display_text=display_text,
                stop=True,
            )

        # Default draft interception mode (execute_mutation=False)
        clean_name = _clean_optional_string(args.get("list_name"))
        try:
            existing = await find_existing_user_list(chat_id, clean_name, LIST_TYPE_SHOPPING)

            draft_payload: Dict[str, Any] = {}
            if existing is not None:
                draft_payload["list_id"] = existing.id
                draft_payload["list_name"] = existing.name
            else:
                draft_payload["list_name"] = clean_name if clean_name else DEFAULT_SHOPPING_LIST_NAME

            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="clear_bought_items",
                payload=draft_payload,
                missing_fields=[],
                source_message_id=source_message_id,
            )
        except Exception:
            logger.error(
                f"Database error in clear_bought_items draft preparation for user {user_id}, chat {chat_id}"
            )
            return ToolResult(
                payload={"success": False, "error": "database_error"},
                display_text="⚠️ Не вдалося підготувати дію. Спробуйте ще раз.",
                stop=True,
            )

        display_text = format_draft_preview_or_question(
            action_type="clear_bought_items",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "clear_bought_items",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # 11. delete_shopping_list
    elif name == "delete_shopping_list":
        if not user_id or not chat_id:
            return ToolResult(
                payload={"success": False, "error": "user_id and chat_id are required"},
                stop=False,
            )

        if execute_mutation:
            target_list_id = args.get("list_id")
            list_name = args.get("list_name") or DEFAULT_SHOPPING_LIST_NAME
            if (
                target_list_id is None
                or not isinstance(target_list_id, int)
                or isinstance(target_list_id, bool)
                or target_list_id <= 0
            ):
                return ToolResult(
                    payload={"success": False, "error": "list_not_found"},
                    display_text="❌ Список не знайдено.",
                    stop=True,
                )

            try:
                deleted = await delete_user_list(target_list_id, chat_id, user_id)
            except Exception:
                logger.error(
                    "Database error in delete_shopping_list mutation for user %d, chat %d",
                    user_id, chat_id,
                )
                return ToolResult(
                    payload={"success": False, "error": "database_error"},
                    display_text="❌ Помилка при видаленні списку.",
                    stop=True,
                )

            if not deleted:
                return ToolResult(
                    payload={"success": False, "error": "list_not_found"},
                    display_text="❌ Список не знайдено.",
                    stop=True,
                )

            safe_name = html.escape(_safe_truncate_raw(list_name, 150))
            display_text = f"\n🗑 Список «{safe_name}» та всі його пункти видалено."

            logger.info(
                "Executed delete_shopping_list mutation: user_id=%d, chat_id=%d, list_id=%d",
                user_id, chat_id, target_list_id,
            )
            return ToolResult(
                payload={"success": True, "list_id": target_list_id},
                display_text=display_text,
                stop=True,
            )

        # Default draft interception mode (execute_mutation=False)
        clean_name = _clean_optional_string(args.get("list_name"))
        try:
            existing = await find_existing_user_list(chat_id, clean_name, LIST_TYPE_SHOPPING)
            if existing is None:
                return ToolResult(
                    payload={"success": False, "error": "list_not_found"},
                    display_text="❌ Список не знайдено.",
                    stop=True,
                )

            draft_payload: Dict[str, Any] = {
                "list_id": existing.id,
                "list_name": existing.name,
            }
            draft = await create_action_draft(
                user_id=user_id,
                chat_id=chat_id,
                action_type="delete_shopping_list",
                payload=draft_payload,
                missing_fields=[],
                source_message_id=source_message_id,
            )
        except Exception:
            logger.error(
                "Database error in delete_shopping_list draft preparation for user %d, chat %d",
                user_id, chat_id,
            )
            return ToolResult(
                payload={"success": False, "error": "database_error"},
                display_text="⚠️ Не вдалося підготувати дію. Спробуйте ще раз.",
                stop=True,
            )

        display_text = format_draft_preview_or_question(
            action_type="delete_shopping_list",
            payload=draft.payload,
            missing_fields=draft.missing_fields,
            timezone_name=tz_str,
        )

        return ToolResult(
            payload={
                "success": True,
                "draft_id": draft.id,
                "action_type": "delete_shopping_list",
                "status": draft.status,
                "missing_fields": draft.missing_fields,
            },
            display_text=display_text,
            stop=True,
            draft_id=draft.id,
        )

    # Unknown tool
    else:
        return ToolResult(
            payload={"success": False, "error": f"Unknown tool: {name}"},
            stop=False
        )


# --- 5. Shared Clarification Handler ---

async def apply_action_draft_reply(
    draft_id: int,
    user_id: int,
    chat_id: int,
    reply_text: str,
    timezone_name: Optional[str] = None,
) -> ToolResult:
    """
    Обробляє відповідь-уточнення до активної ActionDraft у статусі awaiting_info.
    Заповнює рівно ОДНЕ перше поле зі списку missing_fields.
    """
    tz_str = timezone_name or BOT_TIMEZONE

    # 1. Basic argument validation
    if not isinstance(draft_id, int) or isinstance(draft_id, bool) or draft_id <= 0:
        return ToolResult(
            payload={"success": False, "error": "invalid_argument"},
            display_text="❌ Некоректні дані запиту.",
            stop=True,
        )
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return ToolResult(
            payload={"success": False, "error": "invalid_argument"},
            display_text="❌ Некоректні дані запиту.",
            stop=True,
        )
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        return ToolResult(
            payload={"success": False, "error": "invalid_argument"},
            display_text="❌ Некоректні дані запиту.",
            stop=True,
        )
    if not isinstance(reply_text, str) or not reply_text.strip():
        return ToolResult(
            payload={"success": False, "draft_id": draft_id, "error": "empty_reply"},
            display_text="⚠️ Повідомлення не може бути порожнім.",
            stop=True,
            draft_id=draft_id,
        )

    # 2. Load exact draft
    try:
        draft = await get_action_draft(draft_id, user_id, chat_id)
    except Exception:
        logger.error(
            f"Database error fetching action draft for user {user_id}, chat {chat_id}, draft {draft_id}"
        )
        return ToolResult(
            payload={"success": False, "draft_id": draft_id, "error": "database_error"},
            display_text="⚠️ Не вдалося прочитати чернетку дії. Спробуйте ще раз.",
            stop=True,
            draft_id=draft_id,
        )
    if not draft:
        return ToolResult(
            payload={"success": False, "draft_id": draft_id, "error": "draft_not_found"},
            display_text="❌ Чернетку не знайдено або вона вам не належить.",
            stop=True,
            draft_id=draft_id,
        )

    # 3. Verify awaiting_info status
    if draft.status != DRAFT_STATUS_AWAITING_INFO:
        if draft.status == DRAFT_STATUS_CONFIRMED:
            msg = "⚠️ Цю дію вже підтверджено."
        elif draft.status == DRAFT_STATUS_CANCELLED:
            msg = "❌ Цю дію було скасовано."
        elif draft.status == DRAFT_STATUS_EXPIRED:
            msg = "⏳ Термін дії чернетки вичерпано."
        elif draft.status == DRAFT_STATUS_PENDING_CONFIRMATION:
            msg = "⚠️ Дія вже очікує на ваше підтвердження."
        else:
            msg = "⚠️ Дія недоступна для оновлення."

        return ToolResult(
            payload={
                "success": False,
                "draft_id": draft.id,
                "action_type": draft.action_type,
                "status": draft.status,
                "missing_fields": list(draft.missing_fields or []),
                "error": "invalid_status",
            },
            display_text=msg,
            stop=True,
            draft_id=draft.id,
        )

    # 4. Expiration check
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = (
        draft.expires_at.replace(tzinfo=datetime.timezone.utc)
        if draft.expires_at and draft.expires_at.tzinfo is None
        else draft.expires_at
    )
    if exp and exp <= now:
        return ToolResult(
            payload={
                "success": False,
                "draft_id": draft.id,
                "action_type": draft.action_type,
                "status": "expired",
                "missing_fields": list(draft.missing_fields or []),
                "error": "expired",
            },
            display_text="⏳ Термін дії чернетки вичерпано.",
            stop=True,
            draft_id=draft.id,
        )

    # 5. Missing fields check
    if not draft.missing_fields:
        return ToolResult(
            payload={
                "success": False,
                "draft_id": draft.id,
                "action_type": draft.action_type,
                "status": draft.status,
                "missing_fields": [],
                "error": "no_missing_fields",
            },
            display_text="⚠️ Немає полів, що потребують уточнення.",
            stop=True,
            draft_id=draft.id,
        )

    target_field = draft.missing_fields[0]
    payload_updates: Dict[str, Any] = {}
    new_missing_fields: List[str] = list(draft.missing_fields)

    # A. schedule_reminder / text
    if draft.action_type == "schedule_reminder" and target_field == "text":
        clean_text = reply_text.strip()
        payload_updates["text"] = clean_text
        new_missing_fields = [f for f in draft.missing_fields if f != "text"]

    # B. schedule_reminder / iso_time_utc
    elif draft.action_type == "schedule_reminder" and target_field == "iso_time_utc":
        iso_res = calculate_future_date(reply_text.strip(), tz_str)
        valid_dt_utc = None
        if iso_res and not iso_res.startswith("Error"):
            clean_iso = iso_res.strip()
            if clean_iso.endswith("Z") or clean_iso.endswith("z"):
                clean_iso = clean_iso[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(clean_iso)
                if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
                    dt_utc = dt.astimezone(datetime.timezone.utc)
                    if dt_utc > now:
                        valid_dt_utc = dt_utc
            except Exception:
                valid_dt_utc = None

        if valid_dt_utc is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "invalid_future_datetime",
                },
                display_text="⚠️ Будь ласка, вкажіть коректні дату та час у майбутньому (наприклад: завтра о 15:00 або 2026-12-31 10:00).",
                stop=True,
                draft_id=draft.id,
            )

        payload_updates["iso_time_utc"] = valid_dt_utc.isoformat()
        new_missing_fields = [f for f in draft.missing_fields if f != "iso_time_utc"]

    # C. delete_reminder / reminder_id
    elif draft.action_type == "delete_reminder" and target_field == "reminder_id":
        clean_input = reply_text.strip()
        if clean_input.startswith("#"):
            clean_input = clean_input[1:].strip()

        valid_rem_id = None
        if clean_input.isdigit():
            val = int(clean_input)
            if val > 0:
                valid_rem_id = val

        if valid_rem_id is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "invalid_reminder_id",
                },
                display_text="⚠️ Вкажіть лише номер нагадування, яке потрібно видалити (наприклад: 12 або #12).",
                stop=True,
                draft_id=draft.id,
            )

        payload_updates["reminder_id"] = valid_rem_id
        new_missing_fields = [f for f in draft.missing_fields if f != "reminder_id"]

    # D. create_scheduled_tasks
    elif draft.action_type == "create_scheduled_tasks":
        raw_items = [dict(it) for it in (draft.payload.get("items") or [])]
        cur_context = draft.payload.get("context_type")

        if target_field == "context_type":
            clean_reply = reply_text.strip().lower()
            if clean_reply in MEDICATION_SYNONYMS:
                cur_context = "medication"
            elif clean_reply in GENERIC_SYNONYMS:
                cur_context = "generic"
            else:
                return ToolResult(
                    payload={
                        "success": False,
                        "draft_id": draft.id,
                        "action_type": draft.action_type,
                        "status": draft.status,
                        "missing_fields": list(draft.missing_fields or []),
                        "error": "invalid_context_type",
                    },
                    display_text="⚠️ Будь ласка, вкажіть 'ліки' або 'завдання'.",
                    stop=True,
                    draft_id=draft.id,
                )

        elif target_field.startswith("item_name:"):
            try:
                idx = int(target_field.split(":")[1])
            except (ValueError, IndexError):
                idx = 0
            clean_name = reply_text.strip()
            if not clean_name:
                return ToolResult(
                    payload={
                        "success": False,
                        "draft_id": draft.id,
                        "action_type": draft.action_type,
                        "status": draft.status,
                        "missing_fields": list(draft.missing_fields or []),
                        "error": "empty_item_name",
                    },
                    display_text="⚠️ Будь ласка, вкажіть назву.",
                    stop=True,
                    draft_id=draft.id,
                )
            while len(raw_items) <= idx:
                raw_items.append({})
            raw_items[idx]["name"] = clean_name

        elif target_field.startswith("item_dosage:"):
            try:
                idx = int(target_field.split(":")[1])
            except (ValueError, IndexError):
                idx = 0
            clean_dosage = reply_text.strip()
            if not clean_dosage:
                return ToolResult(
                    payload={
                        "success": False,
                        "draft_id": draft.id,
                        "action_type": draft.action_type,
                        "status": draft.status,
                        "missing_fields": list(draft.missing_fields or []),
                        "error": "empty_item_dosage",
                    },
                    display_text="⚠️ Будь ласка, вкажіть точне дозування згідно з інструкцією лікаря.",
                    stop=True,
                    draft_id=draft.id,
                )
            while len(raw_items) <= idx:
                raw_items.append({})
            raw_items[idx]["dosage"] = clean_dosage

        elif target_field.startswith("item_days:"):
            try:
                idx = int(target_field.split(":")[1])
            except (ValueError, IndexError):
                idx = 0
            parsed_days = _parse_days_reply(reply_text)
            if not parsed_days:
                return ToolResult(
                    payload={
                        "success": False,
                        "draft_id": draft.id,
                        "action_type": draft.action_type,
                        "status": draft.status,
                        "missing_fields": list(draft.missing_fields or []),
                        "error": "invalid_item_days",
                    },
                    display_text="⚠️ Будь ласка, вкажіть дні повторення (наприклад: щодня, будні або Пн, Ср, Пт).",
                    stop=True,
                    draft_id=draft.id,
                )
            while len(raw_items) <= idx:
                raw_items.append({})
            raw_items[idx]["days_of_week"] = parsed_days

        elif target_field.startswith("item_time:"):
            try:
                idx = int(target_field.split(":")[1])
            except (ValueError, IndexError):
                idx = 0
            canon_time = _canonicalize_time_str(reply_text)
            if not canon_time:
                return ToolResult(
                    payload={
                        "success": False,
                        "draft_id": draft.id,
                        "action_type": draft.action_type,
                        "status": draft.status,
                        "missing_fields": list(draft.missing_fields or []),
                        "error": "invalid_item_time",
                    },
                    display_text="⚠️ Вкажіть час, наприклад: 10, в 10 або 08:30.",
                    stop=True,
                    draft_id=draft.id,
                )
            while len(raw_items) <= idx:
                raw_items.append({})
            raw_items[idx]["local_time"] = canon_time

        elif target_field.startswith("reference_time:"):
            try:
                idx = int(target_field.split(":")[1])
            except (ValueError, IndexError):
                idx = 0
            canon_time = _canonicalize_time_str(reply_text)
            if not canon_time:
                return ToolResult(
                    payload={
                        "success": False,
                        "draft_id": draft.id,
                        "action_type": draft.action_type,
                        "status": draft.status,
                        "missing_fields": list(draft.missing_fields or []),
                        "error": "invalid_reference_time",
                    },
                    display_text="⚠️ Вкажіть час, наприклад: 10, в 10 або 08:30.",
                    stop=True,
                    draft_id=draft.id,
                )
            target_rel = ""
            if idx < len(raw_items):
                target_rel = str(raw_items[idx].get("relative_to") or "").strip().lower()

            for i, it in enumerate(raw_items):
                it_rel = str(it.get("relative_to") or "").strip().lower()
                if (target_rel and it_rel == target_rel) or i == idx:
                    raw_items[i]["reference_time"] = canon_time

        else:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "unsupported_field_or_action",
                },
                display_text="⚠️ Невідоме поле для уточнення.",
                stop=True,
                draft_id=draft.id,
            )

        temp_payload = {
            "context_type": cur_context,
            "items": raw_items,
        }
        clean_payload, new_missing_fields = _normalize_create_scheduled_tasks_payload(temp_payload, tz_str)
        payload_updates = clean_payload

    # F. add_shopping_items / items
    elif draft.action_type == "add_shopping_items" and target_field == "items":
        raw_chunks = re.split(r"[\n,;]+", reply_text)
        parsed_items: List[str] = []
        for chunk in raw_chunks:
            c = re.sub(r"\s+", " ", chunk.strip())
            if c:
                parsed_items.append(c)

        if not parsed_items:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "empty_items",
                },
                display_text="⚠️ Будь ласка, вкажіть хоча б один пункт для списку.",
                stop=True,
                draft_id=draft.id,
            )

        payload_updates["items"] = parsed_items
        new_missing_fields = [f for f in draft.missing_fields if f != "items"]

    # G. set_shopping_item_state / item_id
    elif draft.action_type == "set_shopping_item_state" and target_field == "item_id":
        clean_input = reply_text.strip()
        if clean_input.startswith("#"):
            clean_input = clean_input[1:].strip()

        valid_id = None
        if clean_input.isdigit() and int(clean_input) > 0:
            valid_id = int(clean_input)

        if valid_id is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "invalid_item_id",
                },
                display_text="⚠️ Вкажіть лише номер пункту (наприклад: 12 або #12).",
                stop=True,
                draft_id=draft.id,
            )

        try:
            item = await get_list_item(valid_id, chat_id)
        except Exception:
            logger.error(
                f"Database error checking list item in {draft.action_type} clarification for user {user_id}, chat {chat_id}, draft {draft.id}"
            )
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "database_error",
                },
                display_text="⚠️ Не вдалося перевірити пункт. Спробуйте ще раз або скасуйте дію.",
                stop=True,
                draft_id=draft.id,
            )

        if item is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "item_not_found",
                },
                display_text="⚠️ Пункт з таким номером не знайдено в цьому чаті. Вкажіть коректний номер.",
                stop=True,
                draft_id=draft.id,
            )

        payload_updates["item_id"] = valid_id
        new_missing_fields = [f for f in draft.missing_fields if f != "item_id"]

    # H. set_shopping_item_state / state
    elif draft.action_type == "set_shopping_item_state" and target_field == "state":
        s_low = reply_text.strip().lower()
        done_synonyms = {"done", "куплено", "куплений", "куплена", "куплене", "готово"}
        active_synonyms = {"active", "повернути", "не куплено", "активний", "активне", "активна"}

        matched_state = None
        if s_low in done_synonyms:
            matched_state = "done"
        elif s_low in active_synonyms:
            matched_state = "active"

        if matched_state is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "invalid_state",
                },
                display_text="⚠️ Будь ласка, вкажіть 'куплено' або 'активний'.",
                stop=True,
                draft_id=draft.id,
            )

        payload_updates["state"] = matched_state
        new_missing_fields = [f for f in draft.missing_fields if f != "state"]

    # I. delete_shopping_item / item_id
    elif draft.action_type == "delete_shopping_item" and target_field == "item_id":
        clean_input = reply_text.strip()
        if clean_input.startswith("#"):
            clean_input = clean_input[1:].strip()

        valid_id = None
        if clean_input.isdigit() and int(clean_input) > 0:
            valid_id = int(clean_input)

        if valid_id is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "invalid_item_id",
                },
                display_text="⚠️ Вкажіть лише номер пункту, який потрібно видалити (наприклад: 12 або #12).",
                stop=True,
                draft_id=draft.id,
            )

        try:
            item = await get_list_item(valid_id, chat_id)
        except Exception:
            logger.error(
                f"Database error checking list item in {draft.action_type} clarification for user {user_id}, chat {chat_id}, draft {draft.id}"
            )
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "database_error",
                },
                display_text="⚠️ Не вдалося перевірити пункт. Спробуйте ще раз або скасуйте дію.",
                stop=True,
                draft_id=draft.id,
            )

        if item is None:
            return ToolResult(
                payload={
                    "success": False,
                    "draft_id": draft.id,
                    "action_type": draft.action_type,
                    "status": draft.status,
                    "missing_fields": list(draft.missing_fields or []),
                    "error": "item_not_found",
                },
                display_text="⚠️ Пункт з таким номером не знайдено в цьому чаті. Вкажіть коректний номер.",
                stop=True,
                draft_id=draft.id,
            )

        payload_updates["item_id"] = valid_id
        new_missing_fields = [f for f in draft.missing_fields if f != "item_id"]

    # J. Unsupported action type or unexpected missing field
    else:
        return ToolResult(
            payload={
                "success": False,
                "draft_id": draft.id,
                "action_type": draft.action_type,
                "status": draft.status,
                "missing_fields": list(draft.missing_fields or []),
                "error": "unsupported_field_or_action",
            },
            display_text="⚠️ Не вдалося обробити уточнення для цієї дії.",
            stop=True,
            draft_id=draft.id,
        )

    # 6. Authoritative update on the SAME ActionDraft row
    try:
        updated_draft = await update_action_draft_information(
            draft_id=draft.id,
            user_id=user_id,
            chat_id=chat_id,
            payload_updates=payload_updates,
            missing_fields=new_missing_fields,
        )
    except Exception:
        logger.error(
            f"Database error updating action draft for user {user_id}, chat {chat_id}, draft {draft.id}, action {draft.action_type}"
        )
        return ToolResult(
            payload={
                "success": False,
                "draft_id": draft.id,
                "action_type": draft.action_type,
                "status": draft.status,
                "missing_fields": list(draft.missing_fields or []),
                "error": "database_error",
            },
            display_text="⚠️ Не вдалося оновити дію. Спробуйте ще раз або скасуйте її.",
            stop=True,
            draft_id=draft.id,
        )

    if not updated_draft:
        return ToolResult(
            payload={
                "success": False,
                "draft_id": draft.id,
                "action_type": draft.action_type,
                "status": "expired_or_conflict",
                "error": "state_conflict",
            },
            display_text="⏳ Термін дії чернетки вичерпано або стан чернетки змінився.",
            stop=True,
            draft_id=draft.id,
        )

    # 7. Render next question or confirmation preview
    preview_tz = clean_payload["timezone"] if updated_draft.action_type == "create_scheduled_tasks" else tz_str
    display_text = format_draft_preview_or_question(
        action_type=updated_draft.action_type,
        payload=updated_draft.payload,
        missing_fields=updated_draft.missing_fields,
        timezone_name=preview_tz,
    )

    logger.info(
        f"Applied action draft reply: draft_id={updated_draft.id}, action={updated_draft.action_type}, "
        f"status={updated_draft.status}, user_id={user_id}, chat_id={chat_id}"
    )

    return ToolResult(
        payload={
            "success": True,
            "draft_id": updated_draft.id,
            "action_type": updated_draft.action_type,
            "status": updated_draft.status,
            "missing_fields": list(updated_draft.missing_fields or []),
        },
        display_text=display_text,
        stop=True,
        draft_id=updated_draft.id,
    )
