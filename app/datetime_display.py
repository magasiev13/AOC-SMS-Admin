"""Consistent customer-facing dates in the configured workspace timezone."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app


def workspace_timezone():
    name = str(current_app.config.get('APP_TIMEZONE') or 'UTC').strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _workspace_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(workspace_timezone())


def format_date(value: date | datetime | None, *, missing='-'):
    if value is None:
        return missing
    if isinstance(value, datetime):
        value = _workspace_datetime(value)
    return f'{value:%b} {value.day}, {value.year}'


def format_datetime(value: datetime | None, *, missing='-'):
    if value is None:
        return missing
    local = _workspace_datetime(value)
    hour = local.hour % 12 or 12
    return f'{local:%a}, {format_date(local)} · {hour}:{local.minute:02d} {local:%p}'
