"""Organization defaults for reminders created by the WordPress integration."""
import re
from datetime import time

BEFORE = "Hi {first_name}, {org} reminder: {event} is {when}. Details and registration: {url}"
DAY_OF = "Hi {first_name}, {org} reminder: {event} is today at {event_time}. Details: {url}"
REMINDERS = {
    'two_week': (14, 'Two weeks before', 'two weeks away', '09:00', BEFORE),
    'seven_day': (7, 'One week before', 'one week away', '09:00', BEFORE),
    'one_day': (1, 'One day before', 'tomorrow', '09:00', BEFORE),
    'day_of': (0, 'Day of event', 'today', '08:00', DAY_OF),
}
TOKEN = re.compile(r'\{(first_name|org|event|when|url|event_time)\}')


def reminder_preferences(organization):
    stored = organization.reminder_preferences or {}
    return {
        kind: dict(enabled=True, time=clock, body=body) | stored.get(kind, {})
        for kind, (_, _, _, clock, body) in REMINDERS.items()
    }


def parse_reminder_preferences(form):
    preferences = {}
    for kind, (_, label, *_) in REMINDERS.items():
        clock = form.get(f'{kind}_time', '').strip()
        body = form.get(f'{kind}_body', '').strip()
        try:
            if not re.fullmatch(r'[0-9]{2}:[0-9]{2}', clock):
                raise ValueError()
            time.fromisoformat(clock)
        except ValueError:
            raise ValueError(f'{label} needs a valid local time.') from None
        if not body or len(body) > 1600:
            raise ValueError(f'{label} needs a message of 1–1600 characters.')
        if any(brace in TOKEN.sub('', body) for brace in '{}'):
            raise ValueError(f'{label} contains an unrecognized placeholder.')
        preferences[kind] = {'enabled': form.get(f'{kind}_enabled') == 'on', 'time': clock, 'body': body}
    return preferences
