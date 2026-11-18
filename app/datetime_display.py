"""Consistent customer-facing dates in the configured workspace timezone."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g

from app.tenant import get_current_organization_id, saas_mode_enabled


def workspace_timezone(organization=None):
    """Resolve explicit worker and request contexts through the same workspace clock."""
    if organization is None and saas_mode_enabled():
        org_id = get_current_organization_id()
        if org_id:
            from app import db
            from app.models import Organization
            # Keep a strong reference during rendering so 50 result rows require one lookup.
            organization = getattr(g, '_clock_organization', None)
            if organization is None or organization.id != org_id:
                organization = g._clock_organization = db.session.get(Organization, org_id)
    name = getattr(organization, 'timezone_name', None)
    if not name and getattr(organization, 'slug', None) == 'armenians-of-colorado':
        name = 'America/Denver'
    name = str(name or current_app.config.get('APP_TIMEZONE') or 'UTC').strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def workspace_timezone_name():
    return getattr(workspace_timezone(), 'key', 'UTC')


def workspace_timezone_label():
    name = workspace_timezone_name()
    return {
        'America/Denver': 'Mountain Time (Denver)',
        'America/New_York': 'Eastern Time (New York)',
        'America/Chicago': 'Central Time (Chicago)',
        'America/Los_Angeles': 'Pacific Time (Los Angeles)',
        'UTC': 'Coordinated Universal Time (UTC)',
    }.get(name, name.replace('_', ' '))


def parse_workspace_datetime(date_value, time_value):
    """Interpret a date/time input as workspace wall time, never browser time."""
    return datetime.strptime(f'{date_value} {time_value}', '%Y-%m-%d %H:%M').replace(
        tzinfo=workspace_timezone(),
    )


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
    return f'{local:%a}, {format_date(local)} · {hour}:{local.minute:02d} {local:%p} {local:%Z}'
