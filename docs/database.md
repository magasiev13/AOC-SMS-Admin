# Database Schema

SMS Admin uses SQLite with SQLAlchemy ORM. All timestamps are stored in UTC.

## Entity Relationship Diagram

```
┌─────────────────────┐       ┌─────────────────────┐
│      AppUser        │       │   CommunityMember   │
├─────────────────────┤       ├─────────────────────┤
│ id (PK)             │       │ id (PK)             │
│ username (UNIQUE)   │       │ name                │
│ password_hash       │       │ phone (UNIQUE)      │
│ role                │       │ created_at          │
│ must_change_password│       └─────────────────────┘
│ created_at          │
└─────────────────────┘       ┌─────────────────────┐
                              │ UnsubscribedContact │
┌─────────────────────┐       ├─────────────────────┤
│       Event         │       │ id (PK)             │
├─────────────────────┤       │ name                │
│ id (PK)             │       │ phone (UNIQUE)      │
│ title               │       │ reason              │
│ date                │       │ source              │
│ created_at          │       │ created_at          │
└──────────┬──────────┘       └─────────────────────┘
           │
           │ 1:N                ┌─────────────────────┐
           ▼                    │  SuppressedContact  │
┌─────────────────────┐        ├─────────────────────┤
│  EventRegistration  │        │ id (PK)             │
├─────────────────────┤        │ phone (UNIQUE)      │
│ id (PK)             │        │ reason              │
│ event_id (FK)       │        │ category            │
│ name                │        │ source              │
│ phone               │        │ source_type         │
│ created_at          │        │ source_message_log_id│
└─────────────────────┘        │ created_at          │
 (UNIQUE: event_id+phone)      │ updated_at          │
                               └─────────────────────┘
┌─────────────────────┐
│     MessageLog      │        ┌─────────────────────┐
├─────────────────────┤        │  ScheduledMessage   │
│ id (PK)             │        ├─────────────────────┤
│ created_at          │        │ id (PK)             │
│ message_body        │        │ created_at          │
│ target              │        │ scheduled_at        │
│ event_id (FK)       │        │ message_body        │
│ status              │        │ target              │
│ total_recipients    │        │ event_id (FK)       │
│ success_count       │        │ status              │
│ failure_count       │        │ test_mode           │
│ details (JSON)      │        │ processing_started_at│
└─────────────────────┘        │ sent_at             │
                               │ error_message       │
┌─────────────────────┐        │ message_log_id (FK) │
│    LoginAttempt     │        └─────────────────────┘
├─────────────────────┤
│ id (PK)             │
│ client_ip (INDEX)   │
│ attempt_count       │
│ first_attempt_at    │
│ locked_until        │
└─────────────────────┘
```

## Model Details

### AppUser

Application users with role-based access control.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `username` | String(80) | NOT NULL, UNIQUE | Login username |
| `password_hash` | String(255) | NOT NULL | Hashed password (pbkdf2/scrypt) |
| `phone` | String(20) | nullable, UNIQUE (non-empty) | Security alert phone number |
| `role` | String(30) | NOT NULL, default='admin' | User role: 'admin' or 'social_manager' |
| `must_change_password` | Boolean | NOT NULL, default=False | Force password change on login |
| `session_nonce` | String(64) | NOT NULL | Session revocation token |
| `created_at` | DateTime | default=utc_now | Account creation timestamp |

**Methods:**
- `set_password(password)` - Hash and store password
- `check_password(password)` - Verify password against hash
- `get_id()` - Returns nonce-bound session identifier (`id:session_nonce`)
- `rotate_session_nonce()` - Invalidates all active sessions
- `is_admin` - Property returning True if role is 'admin'
- `is_social_manager` - Property returning True if role is 'social_manager'

### CommunityMember

Recipients for community-wide SMS blasts.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `name` | String(100) | nullable | Contact name (optional) |
| `phone` | String(20) | NOT NULL, UNIQUE | E.164 phone number |
| `created_at` | DateTime | default=utc_now | Record creation timestamp |

### Event

Event definitions for event-specific SMS blasts.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `title` | String(200) | NOT NULL | Event title |
| `date` | Date | nullable | Event date (optional) |
| `created_at` | DateTime | default=utc_now | Record creation timestamp |

**Relationships:**
- `registrations` - One-to-many with EventRegistration (cascade delete)

### EventRegistration

Recipients registered for specific events (separate from community members).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `event_id` | Integer | FK(events.id), NOT NULL | Parent event |
| `name` | String(100) | nullable | Registrant name (optional) |
| `phone` | String(20) | NOT NULL | E.164 phone number |
| `created_at` | DateTime | default=utc_now | Record creation timestamp |

**Constraints:**
- UNIQUE(event_id, phone) - Same phone can't register twice for same event

### SurveyFlow

Inbound multi-step survey definitions started by keyword.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `name` | String(120) | NOT NULL, UNIQUE | Survey display name |
| `trigger_keyword` | String(40) | NOT NULL, UNIQUE | Keyword that starts survey |
| `intro_message` | Text | nullable | Optional first message |
| `questions_json` | Text | NOT NULL | JSON array of prompts |
| `completion_message` | Text | nullable | Optional completion message |
| `linked_event_id` | Integer | FK(events.id), nullable, INDEX | Optional event to upsert registrations on completion |
| `is_active` | Boolean | NOT NULL, default=True | Survey enabled state |
| `start_count` | Integer | NOT NULL, default=0 | Number of survey starts |
| `completion_count` | Integer | NOT NULL, default=0 | Number of completed sessions |
| `created_at` | DateTime | default=utc_now | Record creation timestamp |
| `updated_at` | DateTime | default=utc_now, onupdate | Last update timestamp |

### UnsubscribedContact

Phone numbers that have opted out and should not receive messages.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `name` | String(100) | nullable | Contact name (if known) |
| `phone` | String(20) | NOT NULL, UNIQUE | E.164 phone number |
| `reason` | Text | nullable | Unsubscribe reason or error message |
| `source` | String(50) | NOT NULL, default='manual' | How they unsubscribed |
| `created_at` | DateTime | default=utc_now | Unsubscribe timestamp |

**Source values:**
- `manual` - Manually added via UI
- `import` - CSV import
- `community` - Unsubscribed from community list
- `event:{id}` - Unsubscribed from event registration
- `message_failure` - Auto-detected from Twilio opt-out error

### SuppressedContact

Phone numbers that failed delivery and should be skipped.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `phone` | String(20) | NOT NULL, UNIQUE | E.164 phone number (auto-normalized) |
| `reason` | Text | nullable | Failure error message |
| `category` | String(20) | NOT NULL | Failure category |
| `source` | String(50) | nullable | Source identifier |
| `source_type` | String(50) | nullable | Source type (e.g., 'message_log') |
| `source_message_log_id` | Integer | FK(message_logs.id), nullable | Source message log |
| `created_at` | DateTime | default=utc_now | Suppression timestamp |
| `updated_at` | DateTime | default=utc_now, onupdate | Last update timestamp |

**Category values:**
- `opt_out` - User opted out (STOP, etc.)
- `hard_fail` - Invalid number, landline, etc.
- `soft_fail` - Temporary failure (not suppressed)

### MessageLog

Log of sent SMS blasts with per-recipient results.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `created_at` | DateTime | default=utc_now | Send timestamp |
| `message_body` | Text | NOT NULL | Message content |
| `target` | String(20) | NOT NULL | Target type: 'community' or 'event' |
| `event_id` | Integer | FK(events.id), nullable | Target event (if target='event') |
| `status` | String(20) | default='sent' | Status: 'processing', 'sent', 'failed' |
| `total_recipients` | Integer | default=0 | Total recipient count |
| `success_count` | Integer | default=0 | Successful deliveries |
| `failure_count` | Integer | default=0 | Failed deliveries |
| `details` | Text | nullable | JSON array of per-recipient results |

**Details JSON format:**
```json
[
  {"phone": "+1234567890", "name": "John", "success": true, "error": null},
  {"phone": "+1987654321", "name": "Jane", "success": false, "error": "Invalid number"}
]
```

### ScheduledMessage

Scheduled SMS blasts for future sending.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `created_at` | DateTime | default=utc_now | Creation timestamp |
| `scheduled_at` | DateTime | NOT NULL | Scheduled send time (UTC) |
| `message_body` | Text | NOT NULL | Message content |
| `target` | String(20) | NOT NULL | Target type: 'community' or 'event' |
| `event_id` | Integer | FK(events.id), nullable | Target event (if target='event') |
| `status` | String(20) | default='pending' | Status (see below) |
| `test_mode` | Boolean | default=False | Send only to admin test phone |
| `processing_started_at` | DateTime | nullable | When processing began |
| `sent_at` | DateTime | nullable | Actual send timestamp |
| `error_message` | Text | nullable | Error details if failed |
| `message_log_id` | Integer | FK(message_logs.id), nullable | Linked message log |

**Status values:**
- `pending` - Waiting to be sent
- `processing` - Currently being processed
- `sent` - Successfully sent
- `failed` - Failed to send
- `expired` - Exceeded max lag time
- `cancelled` - Manually cancelled

### LoginAttempt

Tracks failed login attempts for rate limiting across workers.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `client_ip` | String(45) | NOT NULL, INDEX | Client IP address |
| `username` | String(80) | NOT NULL, default='' | Username scope (`''` for IP scope) |
| `attempt_count` | Integer | NOT NULL, default=1 | Failed attempt count |
| `first_attempt_at` | DateTime | NOT NULL, default=utc_now | First attempt timestamp |
| `locked_until` | DateTime | nullable | Lockout expiration time |

### UserPasswordHistory

Stores previous password hashes per user to block password reuse.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `user_id` | Integer | FK(users.id), NOT NULL | User ID |
| `password_hash` | String(255) | NOT NULL | Previous password hash |
| `created_at` | DateTime | NOT NULL, default=utc_now | History entry timestamp |

### AuthEvent

Security audit trail for login, lockout, password updates, and alert failures.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | Integer | PRIMARY KEY | Auto-increment ID |
| `event_type` | String(50) | NOT NULL, INDEX | Event name (`login_success`, `password_changed`, etc.) |
| `outcome` | String(20) | NOT NULL | Event outcome (`success`, `failed`, `blocked`) |
| `user_id` | Integer | FK(users.id), nullable, INDEX | Related user id |
| `username` | String(80) | nullable, INDEX | Username at event time |
| `client_ip` | String(45) | nullable | Source IP |
| `metadata_json` | Text | nullable | Structured context payload |
| `created_at` | DateTime | NOT NULL, INDEX | Event timestamp |

## Migration System

Migrations are SQLite-specific Python files in `app/migrations/`. Each migration has an `apply(connection, logger)` function.

### Migration Tables

| Table | Purpose |
|-------|---------|
| `schema_migrations` | Tracks applied migration versions |
| `schema_migration_lock` | Prevents concurrent migrations |

### Running Migrations

Migrations run automatically on app startup or via:

```bash
# Check status
python -m app.dbdoctor --print

# Apply pending migrations
python -m app.dbdoctor --apply

# Full health check
python -m app.dbdoctor --doctor
```

## Indexes

| Table | Index | Columns |
|-------|-------|---------|
| `login_attempts` | `ix_login_attempts_client_ip` | `client_ip` |
| `login_attempts` | `ux_login_attempts_client_ip_username` | `client_ip, username` |
| `users` | `ix_users_phone` | `phone` |
| `users` | `uq_users_phone_nonempty` | `phone` (partial unique) |
| `user_password_history` | `ix_user_password_history_user_created` | `user_id, created_at` |
| `auth_events` | `ix_auth_events_created_at` | `created_at` |
| `auth_events` | `ix_auth_events_event_type` | `event_type` |
| `community_members` | implicit | `phone` (UNIQUE) |
| `unsubscribed_contacts` | implicit | `phone` (UNIQUE) |
| `suppressed_contacts` | implicit | `phone` (UNIQUE) |
| `event_registrations` | `unique_event_phone` | `event_id, phone` |

## Database File Location

Default: `instance/sms.db`

Override via `DATABASE_URL` environment variable:
```
DATABASE_URL=sqlite:///path/to/custom.db
```
