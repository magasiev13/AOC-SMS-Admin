# app/services/ — Business Logic Layer

Service modules encapsulating domain logic, separated from route handlers.

## OVERVIEW

All SMS sending, scheduling, recipient filtering, suppression management, and inbound message processing lives here. Routes call services; services call Twilio API and DB.

## WHERE TO LOOK

| Task | File | Key functions |
|------|------|---------------|
| Send SMS | `twilio_service.py` | `TwilioService.send_bulk()`, `get_twilio_service()` |
| Validate inbound webhook | `twilio_service.py` | `validate_inbound_signature()` |
| Process inbound SMS | `inbox_service.py` | `process_inbound_sms()` — webhook handler |
| Keyword auto-reply | `inbox_service.py` | Matches `KeywordAutomationRule`, sends response |
| Survey flow | `inbox_service.py` | `SurveySession` state machine, question progression |
| Reply to thread | `inbox_service.py` | `send_thread_reply()` |
| Scheduled message processing | `scheduler_service.py` | `send_scheduled_messages()`, `init_scheduler()` |
| Filter unsubscribed | `recipient_service.py` | `filter_unsubscribed_recipients()`, `get_unsubscribed_phone_set()` |
| Filter suppressed | `recipient_service.py` | `filter_suppressed_recipients()` |
| Classify send failures | `suppression_service.py` | `process_failure_details()`, `classify_failure()` |
| Backfill suppressions | `suppression_backfill.py` | `backfill_suppressions()` — scans historical logs |

## SMS PIPELINE (UI → Twilio)

```
Route (routes.py)
  → Parse recipients from community_members or event_registrations
  → Filter: filter_unsubscribed_recipients() + filter_suppressed_recipients()
  → Create MessageLog(status='processing')
  → Enqueue: get_queue().enqueue(send_bulk_job, log_id, recipients, message)

RQ Worker (tasks.py → send_bulk_job)
  → Create own app context
  → Resume from partial sends (existing details)
  → TwilioService.send_bulk(recipients, message, delay=0.1)
  → Persist progress incrementally
  → process_failure_details() → upsert SuppressedContact
```

## INBOUND PIPELINE (Twilio webhook → response)

```
Twilio POST /inbound-sms → routes.py
  → validate_inbound_signature()
  → process_inbound_sms(phone, body, payload)
    → Upsert InboxThread + InboxMessage
    → Check active SurveySession → advance survey
    → Match KeywordAutomationRule → send auto-reply
    → Handle STOP/START → update UnsubscribedContact
```

## CONVENTIONS

- Services are stateless functions or thin classes. No global state.
- `TwilioService` instantiated via `get_twilio_service()` (reads config from `current_app`).
- Scheduler dual-mode: APScheduler in dev (`init_scheduler()`), systemd timer in prod (`scheduler_runner.py`).
- `TwilioTransientError` raised on rate limits / temporary failures; RQ retries handle these.
- Suppression categories: `invalid_number`, `opted_out`, `unreachable`, `carrier_violation`.

## ANTI-PATTERNS

- **DO NOT** call Twilio directly from routes. Always go through `twilio_service.py`.
- **DO NOT** filter recipients in services. Filtering happens in routes before enqueuing.
- **DO NOT** import `app` or `db` at module top level in services that run in RQ jobs.
