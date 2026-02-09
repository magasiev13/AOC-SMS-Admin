# API Reference

This document describes all HTTP routes in the SMS Admin application.

## Authentication

All routes except `/health`, `/login`, and `/webhooks/twilio/inbound` require authentication via Flask-Login session.

### Login Flow

```
POST /login
Content-Type: application/x-www-form-urlencoded

username=admin&password=secret&remember=on
```

**Rate Limiting:**
- 5 failed attempts within 5 minutes triggers 10-minute lockout
- Tracked per client IP in database

## Public Routes

### Health Check

```
GET /health
```

Returns `OK` with status 200. Used for load balancer health checks.

---

## Dashboard

### View Dashboard

```
GET /dashboard
```

Displays:
- Recipient counts (community, event registrations)
- Unsubscribed count
- Pending scheduled messages
- 7-day delivery trends chart
- Recent message logs

### Send Message

```
POST /dashboard
Content-Type: application/x-www-form-urlencoded

message_body=Hello!&target=community|event&event_id=1&test_mode=on&include_unsubscribe=on&schedule_later=on&schedule_date=2024-01-15&schedule_time=14:30&client_timezone=America/Denver
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message_body` | Yes | Message content |
| `target` | Yes | `community` or `event` |
| `event_id` | If target=event | Event ID |
| `test_mode` | No | Send only to ADMIN_TEST_PHONE |
| `include_unsubscribe` | No | Append "Reply STOP to unsubscribe" |
| `schedule_later` | No | Schedule for future delivery |
| `schedule_date` | If schedule_later | Date (YYYY-MM-DD) |
| `schedule_time` | If schedule_later | Time (HH:MM) |
| `client_timezone` | No | Timezone for scheduled time |

**Message Templating:**

The message body supports personalization tokens:
- `{name}` or `{full_name}` - Recipient's full name
- `{first_name}` - Recipient's first name

Only these tokens are supported; invalid tokens will be rejected.

Example: `Hello {first_name}!` → `Hello John!`

---

## User Management (Admin Only)

### List Users

```
GET /users
```

### Add User

```
GET /users/add
POST /users/add
Content-Type: application/x-www-form-urlencoded

username=newuser&role=admin|social_manager&password=secret&must_change_password=on
```

### Edit User

```
GET /users/<user_id>/edit
POST /users/<user_id>/edit
```

### Delete User

```
POST /users/<user_id>/delete
```

**Constraints:**
- Cannot delete yourself
- At least one admin must remain

### Change Password

```
GET /account/password
POST /account/password
Content-Type: application/x-www-form-urlencoded

current_password=old&new_password=new&confirm_password=new
```

---

## Community Members

### List Members

```
GET /community
GET /community?search=john
```

### Add Member

```
GET /community/add
POST /community/add
Content-Type: application/x-www-form-urlencoded

name=John Doe&phone=+1234567890
```

### Edit Member

```
GET /community/<member_id>/edit
POST /community/<member_id>/edit
```

### Delete Member

```
POST /community/<member_id>/delete
```

### Bulk Delete Members

```
POST /community/bulk-delete
Content-Type: application/x-www-form-urlencoded

member_ids=1&member_ids=2&member_ids=3
```

### Import Members (CSV)

```
GET /community/import
POST /community/import
Content-Type: multipart/form-data

file=@members.csv
```

**CSV Formats Supported:**
- Single column: phone only
- Two columns: name, phone (auto-detected)
- Three columns: first_name, last_name, phone

### Export Members (CSV)

```
GET /community/export
```

Returns CSV with columns: `name`, `phone`, `created_at`

### Unsubscribe Member

```
POST /community/<member_id>/unsubscribe
```

Adds member to unsubscribed list (does not delete from community).

---

## Events

### List Events

```
GET /events
GET /events?search=conference
```

### Create Event

```
GET /events/add
POST /events/add
Content-Type: application/x-www-form-urlencoded

title=Annual Conference&date=2024-06-15
```

### View Event

```
GET /events/<event_id>
```

Shows event details and registrations.

### Edit Event

```
GET /events/<event_id>/edit
POST /events/<event_id>/edit
```

### Delete Event (Admin Only)

```
POST /events/<event_id>/delete
```

Cascade deletes all registrations.

### Add Registration

```
POST /events/<event_id>/register
Content-Type: application/x-www-form-urlencoded

name=Jane Doe&phone=+1234567890
```

### Remove Registration

```
POST /events/<event_id>/unregister/<registration_id>
```

### Unsubscribe Registration

```
POST /events/<event_id>/registrations/<registration_id>/unsubscribe
```

### Import Registrations (CSV)

```
POST /events/<event_id>/import
Content-Type: multipart/form-data

file=@registrations.csv
```

### Export Registrations (CSV)

```
GET /events/<event_id>/export
```

---

## Message Logs

### List Logs

```
GET /logs
GET /logs?search=hello
```

Returns most recent 100 logs.

### View Log Detail

```
GET /logs/<log_id>
```

Shows message content and per-recipient results.

### Poll Log Status (API)

```
GET /logs/status?ids=1,2,3
```

Returns JSON for polling processing logs:
```json
{
  "logs": [
    {"id": 1, "status": "processing", "success_count": 5, "failure_count": 0},
    {"id": 2, "status": "sent", "success_count": 10, "failure_count": 2}
  ]
}
```

### Clear All Logs (Admin Only)

```
POST /logs/clear
Content-Type: application/x-www-form-urlencoded

admin_password=secret
```

Requires current user's password confirmation.

---

## Scheduled Messages

### List Scheduled

```
GET /scheduled
GET /scheduled?search=reminder
```

### Cancel Scheduled

```
POST /scheduled/<scheduled_id>/cancel
```

Only pending/processing messages can be cancelled.

### Delete Scheduled

```
POST /scheduled/<scheduled_id>/delete
```

### Bulk Delete Scheduled

```
POST /scheduled/bulk-delete
Content-Type: application/x-www-form-urlencoded

scheduled_ids=1,2,3
```

### Poll Scheduled Status (API)

```
GET /scheduled/status
```

Returns JSON:
```json
{
  "pending_count": 5,
  "pending_ids": [1, 2, 3, 4, 5]
}
```

---

## Unsubscribed & Suppressed Contacts

### List All

```
GET /unsubscribed
GET /unsubscribed?search=john&page=1&sort=created_at&dir=desc
```

Combined view of unsubscribed and suppressed contacts.

**Sort Keys:** `name`, `phone`, `reason`, `category`, `source`, `created_at`
**Sort Directions:** `asc`, `desc`

### Add Unsubscribed

```
GET /unsubscribed/add
POST /unsubscribed/add
Content-Type: application/x-www-form-urlencoded

name=John&phone=+1234567890&reason=Requested removal&source=manual
```

### Import Unsubscribed (CSV)

```
GET /unsubscribed/import
POST /unsubscribed/import
Content-Type: multipart/form-data

file=@unsubscribed.csv
```

### Export Unsubscribed (CSV)

```
GET /unsubscribed/export
```

### Delete Unsubscribed

```
POST /unsubscribed/<entry_id>/delete
```

### Bulk Delete

```
POST /unsubscribed/bulk-delete
Content-Type: application/x-www-form-urlencoded

unsubscribed_ids=1&unsubscribed_ids=2&suppressed_ids=3
```

### Backfill Suppressions

```
POST /unsubscribed/backfill
```

Queues background job to process historical message logs and extract suppression data.

---

## Inbound Inbox & Automations

### Twilio Inbound Webhook (Public)

```
POST /webhooks/twilio/inbound
Content-Type: application/x-www-form-urlencoded

From=%2B15551234567&Body=HELP&MessageSid=SMxxxx
```

Receives inbound SMS from Twilio and:
- Stores the inbound message in the shared inbox
- Handles `STOP` / `START` suppression updates
- Applies matching keyword automation rules
- Starts or advances survey flows

If `TWILIO_VALIDATE_INBOUND_SIGNATURE=1`, requests require a valid `X-Twilio-Signature`.

### Shared Inbox

```
GET /inbox
GET /inbox?search=%2B1555&thread=12
POST /inbox/<thread_id>/reply
POST /inbox/threads/<thread_id>/update
POST /inbox/threads/<thread_id>/delete
POST /inbox/threads/bulk-delete
POST /inbox/messages/<message_id>/delete
POST /inbox/messages/bulk-delete
```

Inbox mutations (`reply`, `thread update/delete`, `message delete`) require `admin` or `social_manager`.

### Keyword Automations

```
GET /inbox/keywords
GET /inbox/keywords/add
POST /inbox/keywords/add
GET /inbox/keywords/<rule_id>/edit
POST /inbox/keywords/<rule_id>/edit
POST /inbox/keywords/<rule_id>/delete
```

### Survey Flows

```
GET /inbox/surveys
GET /inbox/surveys/add
POST /inbox/surveys/add
GET /inbox/surveys/<survey_id>/edit
POST /inbox/surveys/<survey_id>/edit
POST /inbox/surveys/<survey_id>/deactivate
```

---

## Phone Number Formats

The API accepts various phone formats and normalizes them to E.164:

| Input | Normalized |
|-------|------------|
| `720-383-2388` | `+17203832388` |
| `(303) 918-8410` | `+13039188410` |
| `3236300201` | `+13236300201` |
| `+1234567890` | `+1234567890` |
| `1-800-555-0123` | `+18005550123` |

**Validation:** E.164 format requires `+` followed by 7-15 digits.

---

## Error Responses

### Flash Messages

Most form submissions return redirects with flash messages:
- `success` - Operation completed
- `warning` - Partial success or informational
- `error` - Operation failed

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 302 | Redirect (after form submission) |
| 403 | Forbidden (wrong role) |
| 404 | Resource not found |
| 500 | Server error |

---

## Role Permissions

| Action | Admin | Social Manager |
|--------|-------|----------------|
| View dashboard | ✓ | ✓ |
| Send messages | ✓ | ✓ |
| View logs | ✓ | ✓ |
| View inbox | ✓ | ✓ |
| Reply from inbox | ✓ | ✓ |
| Update/delete inbox threads/messages | ✓ | ✓ |
| Manage keyword/survey automations | ✓ | ✓ |
| View community | ✓ | ✓ |
| Add/edit community | ✓ | ✗ |
| Delete community | ✓ | ✗ |
| View events | ✓ | ✓ |
| Create/edit events | ✓ | ✓ |
| Delete events | ✓ | ✗ |
| Add registrations | ✓ | ✓ |
| Manage users | ✓ | ✗ |
| Clear logs | ✓ | ✗ |
| Manage unsubscribed | ✓ | ✗ |
