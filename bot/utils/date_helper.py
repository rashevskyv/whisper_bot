import dateparser
import datetime
import logging
import zoneinfo
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

def calculate_future_date(local_dt_string: str, user_timezone: str = None) -> str:
    """
    Takes an absolute LOCAL datetime string from AI and converts it to UTC ISO.
    AI must provide YYYY-MM-DD HH:MM:SS format.
    """
    tz_str = user_timezone or BOT_TIMEZONE
    try:
        # 1. Parse string as naive datetime
        # AI usually outputs '2025-12-28 00:09:00'
        dt_naive = datetime.datetime.strptime(local_dt_string.strip(), '%Y-%m-%d %H:%M:%S')
        
        # 2. Assign the user's timezone to the naive datetime
        try:
            user_tz = zoneinfo.ZoneInfo(tz_str)
        except:
            user_tz = zoneinfo.ZoneInfo("UTC")
            
        dt_localized = dt_naive.replace(tzinfo=user_tz)
        
        # 3. Convert to UTC
        dt_utc = dt_localized.astimezone(datetime.timezone.utc)
            
        return dt_utc.isoformat()
        
    except Exception as e:
        logger.error(f"Date parsing fatal error for '{local_dt_string}': {e}")
        # Try fallback to dateparser if AI didn't follow the format
        try:
            dt = dateparser.parse(local_dt_string, settings={'TIMEZONE': tz_str, 'TO_TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': True})
            if dt: return dt.isoformat()
        except: pass
        return f"Error: Invalid date format. Use YYYY-MM-DD HH:MM:SS"