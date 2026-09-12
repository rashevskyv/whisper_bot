import re
import datetime
import logging
import zoneinfo
from typing import Optional, Tuple
import dateparser
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

WEEKDAYS_MAP = {
    # Ukrainian (all common inflections and apostrophes)
    "понеділок": 0, "понеділка": 0, "понеділком": 0, "понеділку": 0, "пн": 0,
    "вівторок": 1, "вівторка": 1, "вівторком": 1, "вівторку": 1, "вт": 1,
    "середа": 2, "середи": 2, "середу": 2, "середою": 2, "ср": 2,
    "четвер": 3, "четверга": 3, "четвергом": 3, "четвергу": 3, "чт": 3,
    "п'ятниця": 4, "п'ятниці": 4, "п'ятницю": 4, "п'ятницею": 4, "пт": 4,
    "пʼятниця": 4, "пʼятниці": 4, "пʼятницю": 4, "пʼятницею": 4,
    "п’ятниця": 4, "п’ятниці": 4, "п’ятницю": 4, "п’ятницею": 4,
    "пятниця": 4, "пятниці": 4, "пятницю": 4, "пятницею": 4,
    "субота": 5, "суботи": 5, "суботу": 5, "суботою": 5, "сб": 5,
    "неділя": 6, "неділі": 6, "неділю": 6, "неділею": 6, "нд": 6,
    # English
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

WEEKDAYS_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_NAMES_UK = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]


def _extract_time_of_day(s: str) -> Tuple[Optional[int], int]:
    """Extracts hour and minute from natural language string."""
    # 1. Patterns like "о 15:00", "в 15:30", "at 15:00", "о 10", "в 8"
    time_match = re.search(r"(?:^|\s)(?:о|об|в|у|at)\s+(\d{1,2})(?::(\d{2}))?(?:\s|$)", s)
    if time_match:
        h = int(time_match.group(1))
        m = int(time_match.group(2)) if time_match.group(2) else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m

    # 2. 12-hour format with am/pm: "3 pm", "10 am", "3:30 pm"
    ampm_match = re.search(r"(?:^|\s)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s)
    if ampm_match:
        h = int(ampm_match.group(1))
        m = int(ampm_match.group(2)) if ampm_match.group(2) else 0
        is_pm = ampm_match.group(3).lower() == "pm"
        if 1 <= h <= 12 and 0 <= m <= 59:
            if is_pm and h < 12:
                h += 12
            elif not is_pm and h == 12:
                h = 0
            return h, m

    # 3. Direct HH:MM (e.g. "15:00", "08:30")
    hm_match = re.search(r"(?:^|\s)(\d{1,2}):(\d{2})(?:\s|$)", s)
    if hm_match:
        h = int(hm_match.group(1))
        m = int(hm_match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m

    # 4. Words for time of day
    if any(w in s for w in ["вранці", "зранку", "morning"]):
        return 9, 0
    if any(w in s for w in ["вдень", "в обід", "afternoon", "noon"]):
        return 13, 0
    if any(w in s for w in ["ввечері", "вечором", "evening"]):
        return 19, 0
    if any(w in s for w in ["вночі", "night"]):
        return 22, 0

    return None, 0


def calculate_future_date_details(
    local_dt_string: str,
    user_timezone: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Parses a local datetime string or free-form natural language expression and returns:
    (iso_utc, local_dt_str, weekday_en, weekday_uk).
    Returns (None, None, None, None) if parsing fails or time is in the past.
    """
    if not isinstance(local_dt_string, str) or not local_dt_string.strip():
        return None, None, None, None

    tz_str = user_timezone or BOT_TIMEZONE
    try:
        user_tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        user_tz = zoneinfo.ZoneInfo("UTC")

    now = datetime.datetime.now(user_tz)
    s = local_dt_string.strip().lower()
    # Normalize unicode apostrophes to standard ASCII single quote
    s = s.replace("’", "'").replace("ʼ", "'")

    resolved_dt: Optional[datetime.datetime] = None

    # 1. Strict YYYY-MM-DD HH:MM:SS or YYYY-MM-DD HH:MM
    strict_match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}(?::\d{2})?)", s)
    if strict_match:
        try:
            d_part = strict_match.group(1)
            t_part = strict_match.group(2)
            if t_part.count(":") == 1:
                t_part += ":00"
            dt_naive = datetime.datetime.strptime(f"{d_part} {t_part}", "%Y-%m-%d %H:%M:%S")
            resolved_dt = dt_naive.replace(tzinfo=user_tz)
        except Exception:
            resolved_dt = None

    time_hour, time_minute = _extract_time_of_day(s)

    # 2. Relative time intervals (e.g. "через 2 години", "через 15 хвилин", "через 3 дні", "in 2 hours")
    if resolved_dt is None:
        delta_match = re.search(
            r"(?:через|in)\s+(\d+)\s*(хв|хвилин|хвилини|хвилину|min|mins|minute|minutes|год|годин|години|годину|h|hour|hours|днів|дні|день|дня|d|day|days|тиж|тижні|тижнів|тиждень|w|week|weeks|місяц|місяців|місяці|місяць|month|months)",
            s
        )
        if delta_match:
            qty = int(delta_match.group(1))
            unit = delta_match.group(2)
            if unit.startswith("хв") or unit.startswith("min"):
                resolved_dt = now + datetime.timedelta(minutes=qty)
            elif unit.startswith("год") or unit.startswith("h"):
                resolved_dt = now + datetime.timedelta(hours=qty)
            elif unit.startswith("дн") or unit.startswith("д") or unit.startswith("day"):
                target_date = now.date() + datetime.timedelta(days=qty)
                h = time_hour if time_hour is not None else 9
                m = time_minute if time_hour is not None else 0
                resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)
            elif unit.startswith("тиж") or unit.startswith("w"):
                target_date = now.date() + datetime.timedelta(weeks=qty)
                h = time_hour if time_hour is not None else 9
                m = time_minute if time_hour is not None else 0
                resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)
            elif unit.startswith("місяц") or unit.startswith("month"):
                target_date = now.date() + datetime.timedelta(days=qty * 30)
                h = time_hour if time_hour is not None else 9
                m = time_minute if time_hour is not None else 0
                resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)

    # 3. Special word intervals ("через півгодини", "через півтори години", "через годину", "через день", "через тиждень")
    if resolved_dt is None:
        if re.search(r"(?:через|in)\s+(?:пів\s*години|півгодини|half an hour)", s):
            resolved_dt = now + datetime.timedelta(minutes=30)
        elif re.search(r"(?:через|in)\s+(?:півтори\s*години|півтора\s*години)", s):
            resolved_dt = now + datetime.timedelta(minutes=90)
        elif re.search(r"(?:через|in)\s+(?:годину|an hour|1 hour)", s):
            resolved_dt = now + datetime.timedelta(hours=1)
        elif re.search(r"(?:через|in)\s+(?:день|a day|1 day)", s):
            target_date = now.date() + datetime.timedelta(days=1)
            h = time_hour if time_hour is not None else 9
            m = time_minute if time_hour is not None else 0
            resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)
        elif re.search(r"(?:через|in)\s+(?:тиждень|a week|1 week)", s):
            target_date = now.date() + datetime.timedelta(weeks=1)
            h = time_hour if time_hour is not None else 9
            m = time_minute if time_hour is not None else 0
            resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)

    # 4. Relative days: "сьогодні", "завтра", "післязавтра", "today", "tomorrow", "day after tomorrow"
    if resolved_dt is None:
        rel_day: Optional[int] = None
        if "післязавтра" in s or "day after tomorrow" in s:
            rel_day = 2
        elif "завтра" in s or "tomorrow" in s:
            rel_day = 1
        elif "сьогодні" in s or "today" in s:
            rel_day = 0

        if rel_day is not None:
            target_date = now.date() + datetime.timedelta(days=rel_day)
            if rel_day == 0:
                # Today: default to current time + 1h if time omitted
                h = time_hour if time_hour is not None else now.hour + 1
                m = time_minute if time_hour is not None else 0
            else:
                # Tomorrow or after tomorrow: default to 09:00 if time omitted
                h = time_hour if time_hour is not None else 9
                m = time_minute if time_hour is not None else 0
            resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)

    # 5. Weekday references: "наступної п'ятниці", "в цю п'ятницю", "в понеділок"
    if resolved_dt is None:
        found_weekday: Optional[int] = None
        for w_name, w_idx in WEEKDAYS_MAP.items():
            if re.search(rf"\b{re.escape(w_name)}\b", s):
                found_weekday = w_idx
                break

        if found_weekday is not None:
            is_next = bool(re.search(r"\b(наступн\w*|next)\b", s))
            is_this = bool(re.search(r"\b(ц\w+|this)\b", s))
            current_w = now.weekday()

            if is_next:
                if found_weekday == current_w:
                    days_ahead = 7
                elif found_weekday > current_w:
                    days_ahead = 7 + (found_weekday - current_w)
                else:
                    days_ahead = (found_weekday - current_w) % 7
                    if days_ahead == 0:
                        days_ahead = 7
            elif is_this:
                days_ahead = (found_weekday - current_w) % 7
            else:
                # "в п'ятницю", "friday", "у вівторок"
                days_ahead = (found_weekday - current_w) % 7
                if days_ahead == 0:
                    # Same weekday: check if specified time is still in the future today
                    if time_hour is not None:
                        if time_hour > now.hour or (time_hour == now.hour and time_minute > now.minute):
                            days_ahead = 0
                        else:
                            days_ahead = 7
                    else:
                        days_ahead = 7

            target_date = now.date() + datetime.timedelta(days=days_ahead)
            h = time_hour if time_hour is not None else 9
            m = time_minute if time_hour is not None else 0
            resolved_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, h, m, 0, tzinfo=user_tz)

    # 6. DD.MM or DD.MM.YYYY with optional time (e.g. "15.09 о 12:00", "25.12.2026 10:00")
    if resolved_dt is None:
        dot_date_match = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b", s)
        if dot_date_match:
            try:
                day_val = int(dot_date_match.group(1))
                month_val = int(dot_date_match.group(2))
                year_val = int(dot_date_match.group(3)) if dot_date_match.group(3) else now.year
                h = time_hour if time_hour is not None else 9
                m = time_minute if time_hour is not None else 0
                temp_dt = datetime.datetime(year_val, month_val, day_val, h, m, 0, tzinfo=user_tz)
                # If year wasn't specified and date is in past, advance to next year
                if dot_date_match.group(3) is None and temp_dt <= now:
                    temp_dt = datetime.datetime(year_val + 1, month_val, day_val, h, m, 0, tzinfo=user_tz)
                resolved_dt = temp_dt
            except Exception:
                resolved_dt = None

    # 7. Time-only expression (e.g. "о 15:00", "в 10", "15:30") without day
    if resolved_dt is None and time_hour is not None:
        temp_dt = datetime.datetime(now.year, now.month, now.day, time_hour, time_minute, 0, tzinfo=user_tz)
        if temp_dt <= now:
            temp_dt += datetime.timedelta(days=1)
        resolved_dt = temp_dt

    # 8. Fallback to dateparser with relative base and future preference
    if resolved_dt is None:
        try:
            dt = dateparser.parse(
                s,
                languages=['uk', 'ru', 'en'],
                settings={
                    'RELATIVE_BASE': now.replace(tzinfo=None),
                    'TIMEZONE': tz_str,
                    'TO_TIMEZONE': 'UTC',
                    'RETURN_AS_TIMEZONE_AWARE': True,
                    'PREFER_DATES_FROM': 'future',
                }
            )
            if dt:
                resolved_dt = dt.astimezone(user_tz)
        except Exception:
            resolved_dt = None

    # Validate resolved datetime
    if resolved_dt is None:
        return None, None, None, None

    dt_utc = resolved_dt.astimezone(datetime.timezone.utc)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if dt_utc <= now_utc:
        # If it resolved to past or present, it's invalid for future reminders
        return None, None, None, None

    w_idx = resolved_dt.weekday()
    weekday_en = WEEKDAYS_NAMES_EN[w_idx]
    weekday_uk = WEEKDAYS_NAMES_UK[w_idx]
    local_str = resolved_dt.strftime("%Y-%m-%d %H:%M:%S")
    iso_utc = dt_utc.isoformat()

    return iso_utc, local_str, weekday_en, weekday_uk


def calculate_future_date(local_dt_string: str, user_timezone: Optional[str] = None) -> str:
    """
    Takes an absolute LOCAL datetime string or free-form natural language expression
    from AI or user and converts it to UTC ISO string.
    Returns error string if parsing fails or target date is in the past.
    """
    iso_utc, _, _, _ = calculate_future_date_details(local_dt_string, user_timezone)
    if iso_utc:
        return iso_utc
    return "Error: Invalid date format. Use YYYY-MM-DD HH:MM:SS or natural language like 'наступної пʼятниці о 15:00'"
