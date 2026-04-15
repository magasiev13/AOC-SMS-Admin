# Relayn Route Reference

Relayn is an HTML-first Flask app with session auth, CSRF-protected form posts, and a small number of public webhook endpoints.

This document is organized by capability instead of file order. The canonical source remains `app/routes.py` and `app/auth.py`.

## Access Model

- public routes: health, favicon, Stripe webhook, Twilio webhooks
- authenticated workspace routes: owner or staff inside one organization
- owner-only SaaS routes: setup, billing, some invite flows
- platform-admin-only routes: `/platform` and organization management
- admin/social-manager workspace actions are enforced with `@require_roles('admin', 'social_manager')`

In SaaS mode:

- platform admins use `/platform/login`
- owners and staff use `/login`
- owner/staff users are tenant-scoped automatically
- users without a security phone are redirected to `/account/security-contact`

## Public And Webhook Routes

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/health` | `GET` | Public | Returns plain `OK` with HTTP 200. |
| `/favicon.ico` | `GET` | Public | Redirects to the static favicon. |
| `/webhooks/stripe` | `POST` | Public | Stripe webhook; verifies `Stripe-Signature` and requires `STRIPE_WEBHOOK_SECRET`. |
| `/webhooks/twilio/trusthub-status` | `POST` | Public | Twilio Trust Hub/A2P status callback; validates Twilio signature. |
| `/webhooks/twilio/inbound` | `POST` | Public | Twilio inbound SMS webhook; validates signature when enabled. |
| `/webhooks/twilio/a2p-events` | `POST` | Public | Optional Twilio Event Streams sink; bearer-protected when enabled. |

## Authentication And Account Routes

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/login` | `GET`, `POST` | Public | Workspace login surface for owner/staff and legacy users. |
| `/platform/login` | `GET`, `POST` | Public | Platform-admin login surface. |
| `/signup` | `GET`, `POST` | Public | SaaS self-serve signup. |
| `/logout` | `POST` | Authenticated | CSRF-protected logout only. |
| `/invites/<token>` | `GET`, `POST` | Public | Accept owner/staff invitation and create or link a user. |
| `/account/password` | `GET`, `POST` | Authenticated | Password change with policy, reuse, and session invalidation rules. |
| `/account/security-contact` | `GET`, `POST` | Authenticated | Mandatory security phone capture/update. |

### Auth behavior notes

- unauthorized access redirects to `/platform/login` for `/platform*` paths and `/login` elsewhere
- `must_change_password` forces the user through `/account/password`
- missing phone forces the user through `/account/security-contact`
- suspended organizations are logged out and denied workspace access

## Home And Platform Surfaces

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/` | `GET` | Authenticated | Redirects to the correct home surface for the current user. |
| `/platform` | `GET` | Platform admin | Platform home/dashboard. |
| `/platform/operations/restart-services` | `POST` | Platform admin | Queues a host restart request when enabled. |

## SaaS Setup And Billing

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/setup` | `GET`, `POST` | Owner | Owner setup runway for billing and messaging readiness. |
| `/setup/pending` | `GET` | Staff | Read-only setup status for non-owner workspace users. |
| `/setup/status` | `GET` | Workspace user | JSON status snapshot for setup UI polling. |
| `/setup/billing/checkout` | `POST` | Owner | Starts Stripe checkout from setup. |
| `/billing` | `GET` | Owner | Billing overview and post-checkout reconciliation. |
| `/billing/checkout` | `GET`, `POST` | Owner | POST starts Stripe checkout; GET redirects back to billing overview. |
| `/billing/portal` | `POST` | Owner | Starts Stripe billing portal session. |
| `/_test/stripe/checkout/<session_id>` | `GET`, `POST` | Owner | Fake checkout helper for local/test flows when `STRIPE_FAKE_CHECKOUT_ENABLED=1`. |

### Setup actions handled on `/setup`

Owner POSTs to `/setup` may:

- save business profile / A2P draft
- submit A2P onboarding
- refresh onboarding
- cancel onboarding

Customer-managed workspaces are intentionally blocked from editing external compliance in this flow.

## Platform Organization Management

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/platform/organizations` | `GET` | Platform admin | Organization directory and status overview. |
| `/platform/organizations/add` | `GET`, `POST` | Platform admin | Creates an organization, subscription shell, invite, and messaging profile. |
| `/platform/organizations/<organization_id>/access` | `GET` | Platform admin | Access, invite, owner/staff, and billing management surface. |
| `/platform/organizations/<organization_id>/access/invite-staff` | `POST` | Platform admin | Creates a staff invite. |
| `/platform/organizations/<organization_id>/access/billing` | `POST` | Platform admin | Grants or clears complimentary billing. |
| `/platform/organizations/<organization_id>/access/reissue-owner-invite` | `POST` | Platform admin | Reissues owner invite when appropriate. |
| `/platform/organizations/<organization_id>/messaging` | `GET`, `POST` | Platform admin | Platform-managed or customer-managed Twilio settings. |
| `/platform/organizations/<organization_id>/messaging/onboarding` | `GET`, `POST` | Platform admin | Twilio A2P onboarding review and actions. |
| `/platform/organizations/<organization_id>/toggle-status` | `POST` | Platform admin | Suspends or reactivates an organization and its provider status. |

## Workspace Messaging And Operations

### Dashboard

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/dashboard` | `GET`, `POST` | Workspace user | Main send surface. POST handles immediate or scheduled sends. |

Dashboard POST supports:

- community or event targeting
- test mode
- unsubscribe footer
- immediate send via worker queue
- scheduled send creation

## Workspace User And Team Management

| Path | Methods | Access | Notes |
|---|---|---|---|
| `/users` | `GET` | Workspace admin or platform admin | User directory. |
| `/users/add` | `GET`, `POST` | Workspace admin or platform admin | Adds a user directly. |
| `/users/<user_id>/edit` | `GET`, `POST` | Workspace admin or platform admin | Edits role, account state, and profile fields. |
| `/users/<user_id>/delete` | `POST` | Workspace admin or platform admin | Deletes a user with role-safety checks. |
| `/team/invite` | `GET`, `POST` | Workspace owner/admin | Creates an invitation-based team member. |
| `/team/invitations/<invitation_id>/revoke` | `POST` | Workspace owner/admin | Revokes a pending invitation. |
| `/security/events` | `GET` | Admin | Auth/security event viewer with filters. |

## Community Recipient Management

| Path | Methods | Access |
|---|---|---|
| `/community` | `GET` | Workspace user |
| `/community/add` | `GET`, `POST` | Workspace admin/social manager |
| `/community/<member_id>/edit` | `GET`, `POST` | Workspace admin/social manager |
| `/community/<member_id>/delete` | `POST` | Workspace admin/social manager |
| `/community/export` | `GET` | Workspace user |
| `/community/bulk-delete` | `POST` | Workspace admin/social manager |
| `/community/import` | `GET`, `POST` | Workspace admin/social manager |
| `/community/<member_id>/unsubscribe` | `POST` | Workspace admin/social manager |

## Event And Registration Management

| Path | Methods | Access |
|---|---|---|
| `/events` | `GET` | Workspace user |
| `/events/add` | `GET`, `POST` | Workspace admin/social manager |
| `/events/<event_id>` | `GET` | Workspace user |
| `/events/<event_id>/edit` | `GET`, `POST` | Workspace admin/social manager |
| `/events/<event_id>/delete` | `POST` | Workspace admin/social manager |
| `/events/bulk-delete` | `POST` | Workspace admin/social manager |
| `/events/<event_id>/register` | `POST` | Workspace admin/social manager |
| `/events/<event_id>/unregister/<registration_id>` | `POST` | Workspace admin/social manager |
| `/events/<event_id>/registrations/<registration_id>/unsubscribe` | `POST` | Workspace admin/social manager |
| `/events/<event_id>/import` | `POST` | Workspace admin/social manager |
| `/events/<event_id>/export` | `GET` | Workspace user |

## Logs And Scheduled Sends

| Path | Methods | Access |
|---|---|---|
| `/logs` | `GET` | Workspace user |
| `/logs/<log_id>` | `GET` | Workspace user |
| `/logs/status` | `GET` | Workspace user |
| `/logs/clear` | `POST` | Admin |
| `/scheduled` | `GET` | Workspace user |
| `/scheduled/<scheduled_id>/cancel` | `POST` | Workspace admin/social manager |
| `/scheduled/<scheduled_id>/delete` | `POST` | Workspace admin/social manager |
| `/scheduled/bulk-delete` | `POST` | Workspace admin/social manager |
| `/scheduled/bulk-cancel` | `POST` | Workspace admin/social manager |
| `/scheduled/status` | `GET` | Workspace user |

## Unsubscribe And Suppression Operations

| Path | Methods | Access |
|---|---|---|
| `/unsubscribed` | `GET` | Workspace user |
| `/unsubscribed/backfill` | `POST` | Admin |
| `/unsubscribed/add` | `GET`, `POST` | Workspace admin/social manager |
| `/unsubscribed/import` | `GET`, `POST` | Workspace admin/social manager |
| `/unsubscribed/export` | `GET` | Workspace user |
| `/unsubscribed/<entry_id>/delete` | `POST` | Workspace admin/social manager |
| `/unsubscribed/bulk-delete` | `POST` | Workspace admin/social manager |

## Inbox, Keywords, And Surveys

| Path | Methods | Access |
|---|---|---|
| `/inbox` | `GET` | Workspace user |
| `/inbox/status` | `GET` | Workspace user |
| `/inbox/<thread_id>/reply` | `POST` | Workspace admin/social manager |
| `/inbox/threads/<thread_id>/update` | `POST` | Workspace admin/social manager |
| `/inbox/threads/<thread_id>/delete` | `POST` | Workspace admin/social manager |
| `/inbox/messages/bulk-delete` | `POST` | Workspace admin/social manager |
| `/inbox/keywords` | `GET` | Workspace admin/social manager |
| `/inbox/keywords/add` | `GET`, `POST` | Workspace admin/social manager |
| `/inbox/keywords/<rule_id>/edit` | `GET`, `POST` | Workspace admin/social manager |
| `/inbox/keywords/<rule_id>/delete` | `POST` | Workspace admin/social manager |
| `/inbox/surveys` | `GET` | Workspace admin/social manager |
| `/inbox/surveys/<survey_id>/submissions` | `GET` | Workspace admin/social manager |
| `/inbox/surveys/<survey_id>/submissions/export` | `GET` | Workspace admin/social manager |
| `/inbox/surveys/add` | `GET`, `POST` | Workspace admin/social manager |
| `/inbox/surveys/<survey_id>/edit` | `GET`, `POST` | Workspace admin/social manager |
| `/inbox/surveys/<survey_id>/delete` | `POST` | Workspace admin/social manager |
| `/inbox/surveys/<survey_id>/deactivate` | `POST` | Workspace admin/social manager |

## Data Export And Polling Endpoints

The app uses HTML pages as the primary UI, but several endpoints support UI refresh or CSV export:

- `GET /logs/status`
- `GET /scheduled/status`
- `GET /inbox/status`
- `GET /setup/status`
- CSV exports under `/community/export`, `/events/<id>/export`, `/unsubscribed/export`, and survey submission export

All CSV exports use `sanitize_csv_cell()` to mitigate spreadsheet formula injection.

## Runtime Differences

### SaaS-only surfaces

- `/platform/login`
- `/signup`
- `/platform*`
- `/setup*`
- billing ownership checks tied to organization role

### Legacy compatibility behavior

The legacy runtime still uses the same main blueprint for most workspace features, but it does not use:

- tenant scoping
- SaaS signup/setup/platform routes as its primary workflow
- SaaS PostgreSQL schema tooling
