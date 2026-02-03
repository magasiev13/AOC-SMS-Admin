# Architecture Overview

## System Components

SMS Admin is a Flask-based web application for sending community and event SMS blasts via Twilio.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SMS Admin Architecture                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐                │
│   │    Nginx     │────▶│   Gunicorn   │────▶│  Flask App   │                │
│   │ (Reverse     │     │  (WSGI)      │     │              │                │
│   │  Proxy +     │     └──────────────┘     └──────┬───────┘                │
│   │  HTTP Auth)  │                                 │                         │
│   └──────────────┘                                 │                         │
│                                                    ▼                         │
│                          ┌────────────────────────────────────────┐         │
│                          │             Services Layer             │         │
│                          │  ┌─────────┐ ┌─────────┐ ┌───────────┐ │         │
│                          │  │ Twilio  │ │Scheduler│ │Suppression│ │         │
│                          │  │ Service │ │ Service │ │  Service  │ │         │
│                          │  └────┬────┘ └────┬────┘ └─────┬─────┘ │         │
│                          └───────┼───────────┼────────────┼───────┘         │
│                                  │           │            │                 │
│   ┌──────────────┐               │           │            │                 │
│   │    Redis     │◀──────────────┤           │            │                 │
│   │   (Queue)    │               │           │            │                 │
│   └──────┬───────┘               │           │            │                 │
│          │                       │           │            │                 │
│          ▼                       ▼           ▼            ▼                 │
│   ┌──────────────┐        ┌──────────────────────────────────┐             │
│   │  RQ Worker   │        │           SQLite Database         │             │
│   │ (Background  │        │  ┌────────┐ ┌────────┐ ┌───────┐ │             │
│   │   Jobs)      │───────▶│  │Members │ │Events  │ │ Logs  │ │             │
│   └──────────────┘        │  └────────┘ └────────┘ └───────┘ │             │
│                           └──────────────────────────────────┘             │
│   ┌──────────────┐                                                          │
│   │   systemd    │        ┌──────────────────────────────────┐             │
│   │    Timer     │───────▶│     Scheduler (Oneshot)          │             │
│   │ (every 30s)  │        │  Processes pending scheduled     │             │
│   └──────────────┘        │  messages                        │             │
│                           └──────────────────────────────────┘             │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Component Descriptions

### Web Application (`app/`)

| Component | File | Purpose |
|-----------|------|---------|
| **App Factory** | `__init__.py` | Creates Flask app, initializes extensions, runs migrations |
| **Configuration** | `config.py` | Environment-based configuration for Flask, Twilio, Redis |
| **Models** | `models.py` | SQLAlchemy ORM models for all database entities |
| **Routes** | `routes.py` | HTTP endpoints for web UI and API |
| **Authentication** | `auth.py` | Flask-Login setup, login/logout, rate limiting, role decorators |
| **Utilities** | `utils.py` | Phone normalization, CSV parsing, message templating |
| **Queue** | `queue.py` | Redis/RQ connection factory |
| **Tasks** | `tasks.py` | Background job definitions for async SMS sending |

### Services (`app/services/`)

| Service | File | Purpose |
|---------|------|---------|
| **Twilio Service** | `twilio_service.py` | SMS sending via Twilio API with retry support |
| **Scheduler Service** | `scheduler_service.py` | Background scheduler for delayed message sending |
| **Recipient Service** | `recipient_service.py` | Filtering unsubscribed/suppressed recipients |
| **Suppression Service** | `suppression_service.py` | Failure classification, auto-suppression management |
| **Suppression Backfill** | `suppression_backfill.py` | Retroactively process historical failures |

### Database Migrations (`app/migrations/`)

SQLite-specific migrations using a custom runner. Each migration is a Python file with an `apply(connection, logger)` function.

| Migration | Description |
|-----------|-------------|
| `001_add_message_logs_columns.py` | Adds status, counts, details columns to message_logs |
| `002_add_users_must_change_password.py` | Adds must_change_password flag to users |
| `003_add_suppressed_contacts.py` | Creates suppressed_contacts table |
| `004_add_unsubscribed_reason.py` | Adds reason column to unsubscribed_contacts |
| `005_add_scheduled_processing_started_at.py` | Adds processing_started_at to scheduled_messages |

### Deployment (`deploy/`)

| File | Purpose |
|------|---------|
| `install.sh` | Automated deployment script for Debian VPS |
| `sms.service` | systemd unit for main web application |
| `sms-worker.service` | systemd unit for RQ background worker |
| `sms-scheduler.service` | systemd oneshot unit for scheduler |
| `sms-scheduler.timer` | systemd timer triggering scheduler every 30s |
| `run_scheduler_once.sh` | Shell wrapper for scheduler oneshot execution |
| `nginx.conf` | Nginx reverse proxy configuration sample |

## Data Flow

### Immediate SMS Blast

```
1. User submits message via dashboard
2. Flask route validates input
3. MessageLog created with status='processing'
4. Job enqueued to Redis/RQ
5. User redirected to log detail page
6. RQ Worker picks up job
7. TwilioService.send_bulk() sends messages
8. Results saved to MessageLog
9. SuppressionService processes failures
10. MessageLog status updated to 'sent' or 'failed'
```

### Scheduled SMS Blast

```
1. User submits message with schedule_later=True
2. ScheduledMessage created with status='pending'
3. User redirected to scheduled list
4. systemd timer triggers sms-scheduler.service every 30s
5. Scheduler queries for due pending messages
6. For each message:
   a. Atomic status update to 'processing'
   b. Fetch recipients
   c. Filter unsubscribed/suppressed
   d. Send via TwilioService
   e. Create MessageLog
   f. Update ScheduledMessage status to 'sent'
```

## Design Decisions

### Why SQLite?
- Single-server deployment
- Simple backup (file copy)
- No external database server needed
- Suitable for low-concurrency (<1000 recipients per blast)

### Why systemd Timer vs Long-Running Scheduler?
- **Reliability**: Each invocation is independent—no background threads that can die silently
- **Recovery**: If scheduler crashes, systemd invokes it again on next tick
- **Auditability**: Clear logs per run via journald
- **Simplicity**: No daemon management complexity

### Why RQ for Background Jobs?
- Simple Redis-based queue
- Retry support with configurable intervals
- Job status tracking
- Minimal configuration

### Why Separate Recipient Pools?
- **Community Members**: Receive community-wide blasts
- **Event Registrations**: Receive event-specific blasts only
- Allows same person to be in both pools with different contexts
- Prevents accidental cross-targeting

## Security Model

| Layer | Protection |
|-------|------------|
| **Network** | HTTPS via Let's Encrypt + Nginx |
| **Authentication** | Flask-Login with password hashing (pbkdf2/scrypt) |
| **Authorization** | Role-based access (admin, social_manager) |
| **Rate Limiting** | Login attempt tracking with lockout |
| **CSRF** | Flask-WTF CSRF protection |
| **Secrets** | Environment variables, never committed |
| **Session** | HTTP-only, secure, SameSite cookies |
