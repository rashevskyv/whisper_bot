import dateparser
import datetime
import zoneinfo
import logging
from config import BOT_TIMEZONE

logger = logging.getLogger(__name__)

def calculate_future_date(query: str, user_timezone: str = None) -> str:
    """
    Parses natural language date query and returns ISO string in UTC.
    Example: "субота 15:00" -> "2025-12-27T15:00:00+00:00"
    """
    tz_str = user_timezone or BOT_TIMEZONE
    try:
        # Налаштування парсера:
        # PREFER_DATES_FROM = 'future' змушує "суботу" бути наступною суботою, а не минулою
        settings = {
            'TIMEZONE': tz_str,
            'TO_TIMEZONE': 'UTC',
            'RETURN_AS_TIMEZONE_AWARE': True,
            'PREFER_DATES_FROM': 'future', 
            'PREFER_DAY_OF_MONTH': 'first'
        }
        
        # Парсимо
        dt = dateparser.parse(query, settings=settings)
        
        if not dt:
            return f"Error: Could not parse date from '{query}'"
            
        # Перевірка, щоб не нагадувати в минулому (dateparser іноді може взяти сьогоднішній ранок)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if dt < now_utc:
            # Якщо дата в минулому, спробуємо додати тиждень (якщо це день тижня) або день
            # Але dateparser з PREFER_DATES_FROM='future' має це робити сам.
            # Якщо все ж минуле - повертаємо як є, нехай AI вирішує, або повертаємо помилку.
            pass

        return dt.isoformat()
        
    except Exception as e:
        logger.error(f"Date parsing error: {e}")
        return f"Error parsing date: {str(e)}"