# Relayn Services

The service layer is the main business-logic boundary of the app. Routes coordinate HTTP concerns, while service modules handle provider operations, billing, inbox automation, and background processing.

## Service Map

| Module | Responsibility |
|---|---|
| `app/services/auth_security_service.py` | Login lockouts, password policy, auth event recording, retention pruning |
| `app/services/billing_service.py` | Stripe checkout creation, portal links, webhook processing, subscription reconciliation, usage overage posting |
| `app/services/inbox_service.py` | Shared inbox message handling, STOP/START logic, keyword replies, survey sessions, manual thread replies |
| `app/services/legacy_import_service.py` | Imports a legacy SQLite snapshot into the SaaS schema |
| `app/services/platform_operations_service.py` | Queues and dispatches SaaS host restart requests |
| `app/services/provider_secret_service.py` | Encrypts and decrypts provider credentials with Fernet |
| `app/services/recipient_service.py` | Filters unsubscribed and suppressed recipients |
| `app/services/scheduler_service.py` | Processes scheduled sends, retry backoff, and usage capture |
| `app/services/security_alert_service.py` | Sends security-alert SMS messages for account events |
| `app/services/suppression_backfill.py` | Backfills suppressions from historical log detail rows |
| `app/services/suppression_service.py` | Classifies failures and updates unsubscribe/suppression state |
| `app/services/twilio_a2p_service.py` | Manages Twilio A2P onboarding draft/save/submit/refresh/reconcile flows |
| `app/services/twilio_service.py` | Outbound Twilio messaging, provider provisioning, sender sync, inbound signature validation, usage reconciliation |

## Auth And Account Security

### `auth_security_service.py`

Key responsibilities:

- normalize login usernames consistently
- enforce password minimum-length and reuse rules
- maintain login-attempt counters shared across workers/processes
- calculate lockout windows from config
- record auth events such as login failures, resets, and platform restart actions
- prune old auth events according to `AUTH_EVENT_RETENTION_DAYS`

Used primarily by:

- `app/auth.py`
- account-management routes
- platform restart service audit logging

## Billing And Usage

### `billing_service.py`

Key responsibilities:

- create Stripe checkout sessions
- create Stripe billing portal sessions
- process and deduplicate Stripe webhook events
- keep `OrganizationSubscription` in sync with Stripe state
- support fake checkout mode for local/test flows
- reconcile closed usage periods into billable overage entries

Important helper concepts:

- `organization_can_send()` is the billing gate used by setup and messaging flows
- `StripeWebhookEvent` is the idempotency/audit ledger
- usage billing is downstream of Twilio usage reconciliation, not inline with a send request

## Twilio Provider Lifecycle

### `twilio_service.py`

Key responsibilities:

- send individual and bulk outbound messages
- resolve the correct Twilio client for platform-managed or customer-managed messaging
- validate inbound Twilio signatures
- provision, suspend, resume, release, and sync per-organization provider state
- configure service or phone-number inbound webhooks
- record provider audit entries
- reconcile message usage into `MessagingUsageRecord`

Important objects:

- `TwilioService`
- `TwilioTransientError`
- `ProviderProvisioningError`
- `InboundSignatureValidationResult`
- `CustomerManagedValidationResult`

### `twilio_a2p_service.py`

Key responsibilities:

- create or load per-org A2P onboarding state
- normalize and validate Twilio A2P form data
- save drafts
- submit onboarding for queued processing
- refresh or cancel onboarding
- react to Twilio status callbacks and optional Event Streams payloads
- reconcile pending onboarding records on a timer

It also exposes normalized choice lists used by setup and platform-admin forms.

### `provider_secret_service.py`

Minimal encryption wrapper used for:

- encrypted customer-managed Twilio auth tokens
- encrypted A2P business registration values
- encrypted verification tokens

It requires `TWILIO_CREDENTIAL_ENCRYPTION_KEY`.

## Inbox Automation

### `inbox_service.py`

Key responsibilities:

- upsert inbox threads and messages from inbound webhooks
- manage unread counts and thread rollups
- send manual replies from the workspace UI
- enforce unsubscribe behavior
- start, advance, cancel, and complete surveys
- match keyword automation rules
- handle ambiguous fast repeat survey answers safely

The inbox service is the main place where inbound messaging, automation, and survey state intersect.

## Recipient Filtering And Suppression

### `recipient_service.py`

Used before send execution to:

- gather unsubscribed numbers
- gather suppressed numbers
- split recipients into sendable and skipped groups

### `suppression_service.py`

Used after send execution to:

- classify failures into opt-out, hard-fail, or soft-fail-style outcomes
- upsert unsubscribe and suppression tables
- attribute changes back to source log rows

### `suppression_backfill.py`

Batch-scans existing `MessageLog.details` payloads and runs suppression handling retroactively.

## Scheduled Work

### `scheduler_service.py`

Key responsibilities:

- find due `ScheduledMessage` rows
- detect and recover stuck processing rows
- retry transient failures with backoff
- create corresponding `MessageLog` entries
- capture usage candidates without breaking the send path

Production usage:

- systemd oneshot timer

Development usage:

- explicit APScheduler start through `init_scheduler()`

## Platform Operations

### `platform_operations_service.py`

Key responsibilities:

- validate restart-helper configuration
- enqueue durable restart requests
- dispatch or poll restart-helper state via `sudo -n`
- refresh stale requests
- record auth events for queued/succeeded/failed restart operations

This service keeps host-level restarts out of the request-response path and makes them observable in the database.

## Legacy Import

### `legacy_import_service.py`

Used by `app.saas_db` to:

- validate import readiness
- create import-run audit records
- import legacy users, recipients, logs, inbox, surveys, and scheduled messages
- optionally map usernames during import
- create a fresh SaaS organization from a legacy SQLite snapshot

This is the main bridge from the old single-tenant product into the SaaS schema.

## Tasks And Queue Boundary

The service layer is paired with:

- `app/tasks.py`
- `app/queue.py`

Important runtime rule:

- worker jobs create their own app context instead of reusing web-request state

That keeps background execution aligned with the current config and database bindings.
