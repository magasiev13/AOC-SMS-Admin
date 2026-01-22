# Services Documentation

The services layer contains business logic separated from Flask routes.

## TwilioService (`app/services/twilio_service.py`)

Handles all SMS sending via the Twilio API.

### Class: `TwilioService`

```python
from app.services.twilio_service import TwilioService, get_twilio_service

# Factory function (recommended)
twilio = get_twilio_service()

# Direct instantiation
twilio = TwilioService()
```

**Constructor:**
Reads Twilio credentials from Flask config:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`

Raises `ValueError` if any credential is missing.

### Method: `send_message(to_number, body, raise_on_transient=False)`

Send a single SMS message.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `to_number` | str | Recipient phone (E.164 format) |
| `body` | str | Message content |
| `raise_on_transient` | bool | Raise `TwilioTransientError` on 429/5xx errors |

**Returns:**
```python
{
    'success': True,
    'sid': 'SM1234...',  # Twilio message SID
    'status': 'queued',
    'error': None
}
# or on failure
{
    'success': False,
    'sid': None,
    'status': 'failed',
    'error': 'Error message'
}
```

### Method: `send_bulk(recipients, body, delay=0.1, raise_on_transient=False)`

Send SMS to multiple recipients with rate limiting.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `recipients` | list[dict] | List of `{'phone': str, 'name': str}` |
| `body` | str | Message body (supports `{name}`, `{first_name}` tokens) |
| `delay` | float | Seconds between sends (default 0.1) |
| `raise_on_transient` | bool | Raise on transient errors for retry |

**Returns:**
```python
{
    'total': 10,
    'success_count': 8,
    'failure_count': 2,
    'details': [
        {'phone': '+1...', 'name': 'John', 'success': True, 'error': None},
        {'phone': '+1...', 'name': 'Jane', 'success': False, 'error': 'Invalid number'}
    ]
}
```

### Exception: `TwilioTransientError`

Raised on transient Twilio errors (429 rate limit, 5xx server errors) when `raise_on_transient=True`.

```python
class TwilioTransientError(Exception):
    results: dict | None     # Partial results before failure
    failed_index: int | None # Index where failure occurred
```

---

## Scheduler Service (`app/services/scheduler_service.py`)

Background scheduler for processing scheduled messages.

### Function: `send_scheduled_messages(app)`

Main scheduler function. Designed to be called repeatedly (e.g., by systemd timer).

**Process:**
1. Mark stuck 'processing' messages as failed (10-minute timeout)
2. Query pending messages where `scheduled_at <= now`
3. For each message:
   - Atomically update status to 'processing'
   - Skip if already claimed by another process
   - Check expiry (configurable max lag)
   - Fetch recipients (community or event)
   - Filter unsubscribed/suppressed
   - Send via TwilioService
   - Create MessageLog
   - Update ScheduledMessage status

**Configuration:**
- `SCHEDULED_MESSAGE_MAX_LAG` - Minutes before message expires (default: 1440 = 24 hours)

### Function: `init_scheduler(app)`

Initialize APScheduler background scheduler (for development).

Starts a background thread that calls `send_scheduled_messages()` every 5 seconds.

**Note:** In production, use systemd timer instead of APScheduler for reliability.

### Function: `shutdown_scheduler()`

Gracefully shutdown the background scheduler.

---

## Recipient Service (`app/services/recipient_service.py`)

Utilities for filtering recipients before sending.

### Function: `get_unsubscribed_phone_set(phones)`

Get set of phones that are unsubscribed.

```python
phones = ['+1234567890', '+1987654321']
unsubscribed = get_unsubscribed_phone_set(phones)
# {'+1234567890'}
```

### Function: `filter_unsubscribed_recipients(recipients)`

Filter out unsubscribed recipients.

```python
recipients = [
    {'phone': '+1234567890', 'name': 'John'},
    {'phone': '+1987654321', 'name': 'Jane'}
]
filtered, skipped, phones_set = filter_unsubscribed_recipients(recipients)
# filtered: recipients not unsubscribed
# skipped: recipients that were unsubscribed
# phones_set: set of unsubscribed phone numbers
```

### Function: `get_suppressed_phone_set(phones)`

Get set of phones that are suppressed (hard failures).

### Function: `filter_suppressed_recipients(recipients)`

Filter out suppressed recipients.

```python
filtered, skipped, phones_set = filter_suppressed_recipients(recipients)
```

---

## Suppression Service (`app/services/suppression_service.py`)

Automatic suppression management based on delivery failures.

### Function: `classify_failure(error_text)`

Classify a Twilio error into a failure category.

**Returns:** `'opt_out'`, `'hard_fail'`, or `'soft_fail'`

| Category | Examples | Action |
|----------|----------|--------|
| `opt_out` | STOP, unsubscribed, opted out, error 21610/30004 | Add to UnsubscribedContact |
| `hard_fail` | Invalid number, landline, unreachable, error 30003/30005/30007 | Add to SuppressedContact |
| `soft_fail` | Timeout, rate limit, server error, 429/5xx | No suppression (retry later) |

### Function: `process_failure_details(details, source_message_log_id)`

Process a list of delivery results and update suppression tables.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `details` | list[dict] | Per-recipient delivery results |
| `source_message_log_id` | int | MessageLog ID for tracking |

**Actions:**
- `opt_out` → Upsert to `unsubscribed_contacts`
- `hard_fail` → Upsert to `suppressed_contacts`
- Delete matching entries from `community_members` and `event_registrations`

**Returns:**
```python
{
    'total': 100,
    'failed': 5,
    'opt_out': 2,
    'hard_fail': 2,
    'soft_fail': 1,
    'unsubscribed_upserts': 2,
    'suppressed_upserts': 2,
    'community_member_deletes': 3,
    'event_registration_deletes': 1,
    'skipped_no_phone': 0,
    'skipped_invalid': 0
}
```

---

## Suppression Backfill (`app/services/suppression_backfill.py`)

Retroactively process historical message logs to extract suppression data.

### Function: `backfill_suppressions(batch_size=500, logger=None)`

Process all MessageLog entries in batches and extract suppression data.

**Use Case:** Run after deploying the suppression feature to populate suppression tables from historical data.

**Parameters:**
| Name | Type | Description |
|------|------|-------------|
| `batch_size` | int | Logs per batch (default 500) |
| `logger` | object | Logger instance (default: Flask app logger) |

**Returns:**
```python
{
    'batches': 10,
    'logs': 5000,
    'calls': 4500,  # Logs with details
    'details': 45000,  # Total recipient records
    'unsubscribed': 150,
    'suppressed': 75
}
```

**Invocation:**

Via background job:
```python
from app.queue import get_queue
queue = get_queue()
queue.enqueue('app.tasks.backfill_suppressions_job')
```

Via UI: POST `/unsubscribed/backfill`

---

## Tasks (`app/tasks.py`)

Background job definitions for RQ worker.

### Function: `send_bulk_job(log_id, recipient_data, final_message, delay=0.1)`

Background job for sending bulk SMS.

**Features:**
- Resume from partial progress (on retry)
- Transient error handling with RQ retry
- Automatic suppression processing

**RQ Configuration:**
```python
from rq import Retry
queue.enqueue(
    'app.tasks.send_bulk_job',
    log_id,
    recipient_data,
    final_message,
    retry=Retry(max=3, interval=[30, 120, 300])  # Retry after 30s, 2m, 5m
)
```

### Function: `backfill_suppressions_job()`

Background job wrapper for `backfill_suppressions()`.

---

## Queue (`app/queue.py`)

Redis/RQ connection utilities.

### Function: `get_redis_connection(app=None)`

Get Redis connection from app config.

```python
redis = get_redis_connection()
```

Uses `REDIS_URL` config (default: `redis://localhost:6379/0`).

### Function: `get_queue(app=None)`

Get RQ queue instance.

```python
queue = get_queue()
job = queue.enqueue('app.tasks.send_bulk_job', ...)
```

Uses `RQ_QUEUE_NAME` config (default: `sms`).
