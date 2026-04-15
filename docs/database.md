# Twinevia Data Model

Twinevia has one ORM model module, `app/models.py`, but two practical data shapes:

- primary SaaS: multi-tenant PostgreSQL with explicit SaaS schema management
- secondary legacy: SQLite with compatibility migrations and optional local/demo use

## Model Domains

`app/models.py` currently defines 28 ORM models.

### Identity And Access

| Model | Table | Purpose |
|---|---|---|
| `AppUser` | `users` | User account, role, session nonce, phone, platform-admin flag |
| `Organization` | `organizations` | SaaS tenant boundary |
| `OrganizationMembership` | `organization_memberships` | Owner/staff membership within an org |
| `OrganizationInvitation` | `organization_invitations` | Pending owner/staff invite tokens |
| `UserPasswordHistory` | `user_password_history` | Recent password hashes for reuse prevention |
| `AuthEvent` | `auth_events` | Security-relevant login/account activity audit log |
| `LoginAttempt` | `login_attempts` | Shared lockout counters across workers/processes |

### Billing And Platform Operations

| Model | Table | Purpose |
|---|---|---|
| `OrganizationSubscription` | `organization_subscriptions` | Stripe subscription state for one org |
| `StripeWebhookEvent` | `stripe_webhook_events` | Stripe event idempotency and operational ledger |
| `MessagingUsageRecord` | `messaging_usage_records` | Per-message outbound usage and cost reconciliation |
| `OrganizationUsageBillingPeriod` | `organization_usage_billing_periods` | Closed-period overage summary |
| `PlatformServiceRestartRequest` | `platform_service_restart_requests` | Durable restart queue entries for host-level operations |

### Messaging Provider And Compliance

| Model | Table | Purpose |
|---|---|---|
| `OrganizationMessagingProfile` | `organization_messaging_profiles` | Provider mode, sender identity, Messaging Service, provider state |
| `OrganizationA2POnboarding` | `organization_a2p_onboardings` | Twilio A2P/trust data, submission state, remote identifiers |
| `OrganizationProviderAuditLog` | `organization_provider_audit_logs` | Audit trail for provider lifecycle actions |

### Workspace Messaging Data

| Model | Table | Purpose |
|---|---|---|
| `CommunityMember` | `community_members` | General recipient list |
| `UnsubscribedContact` | `unsubscribed_contacts` | STOP/manual unsubscribe ledger |
| `SuppressedContact` | `suppressed_contacts` | Hard-failure suppression ledger |
| `Event` | `events` | Event definition |
| `EventRegistration` | `event_registrations` | Event-specific recipients |
| `MessageLog` | `message_logs` | Blast send history and per-recipient details |
| `ScheduledMessage` | `scheduled_messages` | Future sends and retry state |
| `InboxThread` | `inbox_threads` | Shared inbox conversation thread |
| `InboxMessage` | `inbox_messages` | Inbound/outbound message in a thread |
| `KeywordAutomationRule` | `keyword_automation_rules` | Keyword-triggered auto-replies |
| `SurveyFlow` | `survey_flows` | Keyword-started survey definitions |
| `SurveySession` | `survey_sessions` | Per-phone survey progress |
| `SurveyResponse` | `survey_responses` | Captured survey answers |

## Tenant Scoping

The following workspace tables are tenant-scoped in SaaS mode:

- `auth_events`
- `community_members`
- `event_registrations`
- `events`
- `inbox_messages`
- `inbox_threads`
- `keyword_automation_rules`
- `message_logs`
- `organization_invitations`
- `organization_subscriptions`
- `scheduled_messages`
- `suppressed_contacts`
- `survey_flows`
- `survey_responses`
- `survey_sessions`
- `unsubscribed_contacts`

Tenant scoping is enforced in `app/tenant.py` by:

- setting the current organization on request entry
- automatically adding `organization_id == current_org` criteria to ORM selects
- auto-filling `organization_id` on new rows before flush

Platform tables such as `organizations`, `users`, `platform_service_restart_requests`, provider audit logs, and Stripe webhook events stay globally queryable.

## Core Relationships

### User and tenant relationships

- one `Organization` has many `OrganizationMembership`
- one `AppUser` can have many `OrganizationMembership`, though the current product assumes one primary org membership at a time
- one `Organization` has many `OrganizationInvitation`
- one `Organization` has one `OrganizationSubscription`
- one `Organization` has one `OrganizationMessagingProfile`
- one `Organization` has one `OrganizationA2POnboarding`

### Messaging relationships

- one `Event` has many `EventRegistration`
- one `InboxThread` has many `InboxMessage`
- one `SurveyFlow` has many `SurveySession` and `SurveyResponse`
- one `SurveySession` has many `SurveyResponse`
- one `ScheduledMessage` may point to one `MessageLog`

## Key State Fields

### Organization lifecycle

- `organizations.status`: `active` or `suspended`
- `organization_subscriptions.status`: values such as `incomplete`, `trialing`, `active`, `past_due`, `canceled`, `complimentary`
- `organization_messaging_profiles.provider_mode`: `platform_managed` or `customer_managed`
- `organization_messaging_profiles.provider_status`: `pending`, `provisioning`, `active`, `suspended`, `error`
- `organization_messaging_profiles.sender_review_status`: `pending`, `approved`, `rejected`
- `organization_a2p_onboardings.onboarding_status`: `draft`, `queued`, `processing`, `pending`, `approved`, `needs_action`, `rejected`, `canceled`, `error`

### Workspace lifecycle

- `message_logs.status`: `processing`, `sent`, `failed`
- `scheduled_messages.status`: `pending`, `processing`, `sent`, `failed`, `expired`, `cancelled`
- `survey_sessions.status`: `active`, `completed`, `cancelled`

## Operational Invariants

- usernames are case-insensitively unique
- user emails are case-insensitively unique when present
- community members, unsubscribes, suppressions, keyword rules, and surveys are unique per organization where applicable
- event registrations are unique per organization, event, and phone
- inbox threads are unique per organization and phone
- message SID usage records are globally unique
- each organization has at most one subscription, messaging profile, and A2P onboarding row

## Migration Systems

### Legacy migration system

Path: `app/migrations/`

- file format: numbered Python files such as `021_add_customer_managed_twilio_fields.py`
- runner: `app.migrations.runner`
- tracking table: `schema_migrations`
- CLI: `./venv/bin/python -m app.dbdoctor` locally, `dbdoctor` in production
- intended for the legacy SQLite line

Important detail:

- `dbdoctor` explicitly rejects the SaaS non-SQLite path

### SaaS migration system

Path: `app/saas_migrations/`

- file format: numbered Python files such as `008_add_customer_managed_twilio_fields.py`
- runner: `app.saas_migrations.runner`
- CLI: `./venv/bin/python -m app.saas_db` locally, `twinevia-saas-dbdoctor` in production
- intended for SaaS databases, especially PostgreSQL

The SaaS path is explicit by design:

- production SaaS deploys should run `twinevia-saas-dbdoctor --apply` from `/opt/twinevia-saas` with `.env` sourced
- startup validates SaaS schema readiness rather than relying on the legacy migration CLI

## Runtime Notes

### Primary SaaS path

- recommended database: PostgreSQL
- explicit schema commands:

```bash
./venv/bin/python -m app.saas_db --apply
./venv/bin/python -m app.saas_db --doctor
./venv/bin/python -m app.saas_db --ensure-platform-admin
```

The primary local SaaS fallback database is `sqlite:///instance/twinevia.db` when `DATABASE_URL` is unset. Production SaaS should still use PostgreSQL.

### Legacy compatibility path

- default database: `sqlite:///instance/sms.db`
- compatibility schema commands:

```bash
./venv/bin/python -m app.dbdoctor --apply
./venv/bin/python -m app.dbdoctor --doctor
```

## Sensitive Data

The app stores or derives sensitive operational data in these areas:

- password hashes in `users` and `user_password_history`
- encrypted Twilio auth secrets in `organization_messaging_profiles`
- encrypted registration/business values in `organization_a2p_onboardings`
- security metadata in `auth_events`
- webhook and provider error payloads in Stripe, Twilio, inbox, and A2P-related tables

Production docs and tooling should treat those tables as operationally sensitive even when the app exposes a higher-level UI for the same flows.
