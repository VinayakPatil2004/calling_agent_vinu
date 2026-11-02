"""
Timezone and Business Date Utility for CityLinc Voice Agent.
Enforces Asia/Kolkata (IST = UTC+05:30) date boundaries for daily call limits.
Timestamps are stored in UTC, and daily rate limits are calculated against the IST calendar date.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

# Indian Standard Time (IST): UTC + 05:30 (Fixed offset, no Daylight Saving Time)
IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")


def get_utc_now() -> datetime:
    """Returns current datetime in UTC with explicit timezone awareness."""
    return datetime.now(timezone.utc)


def get_ist_now(utc_dt: Optional[datetime] = None) -> datetime:
    """
    Converts a UTC datetime (or current time if None) to Asia/Kolkata (IST).
    """
    base_utc = utc_dt if utc_dt is not None else get_utc_now()
    if base_utc.tzinfo is None:
        # If naive datetime, assume UTC
        base_utc = base_utc.replace(tzinfo=timezone.utc)
    return base_utc.astimezone(IST)


def get_ist_business_date(dt: Optional[datetime] = None) -> str:
    """
    Returns the canonical business date string in YYYY-MM-DD format for Asia/Kolkata.
    Midnight (12:00:00 AM) IST marks the start of a new business date.
    11:59:59 PM IST belongs to the previous business date.
    """
    ist_dt = get_ist_now(dt)
    return ist_dt.date().isoformat()

