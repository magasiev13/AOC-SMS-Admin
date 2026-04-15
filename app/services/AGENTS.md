# app/services/ — Business Logic

Service modules encapsulate provider operations, billing, inbox automation, scheduled sends, and other business rules.

## MODULES

| Module | Responsibility |
|---|---|
| `auth_security_service.py` | Lockouts, password policy, auth events |
| `billing_service.py` | Stripe checkout, portal, webhook sync, usage billing |
| `inbox_service.py` | Inbound SMS handling, thread replies, keyword/survey automation |
| `legacy_import_service.py` | Legacy SQLite import into SaaS |
| `platform_operations_service.py` | Restart request queue and helper dispatch |
| `provider_secret_service.py` | Encrypt/decrypt provider secrets |
| `recipient_service.py` | Unsubscribe/suppression filtering |
| `scheduler_service.py` | Scheduled send processing and retry logic |
| `security_alert_service.py` | Security SMS alerts |
| `suppression_backfill.py` | Historical suppression extraction |
| `suppression_service.py` | Failure classification and suppression writes |
| `twilio_a2p_service.py` | A2P onboarding lifecycle |
| `twilio_service.py` | Twilio send path, provisioning, signature validation, usage |

## CONVENTIONS

- Prefer calling services from routes instead of embedding provider logic in route handlers.
- Service modules should remain usable from worker contexts and oneshot timer contexts.
- When a flow touches Twilio provider state, auditability matters; reuse provider audit helpers instead of inventing a second logging path.
- Treat billing readiness and provider readiness as separate checks; both matter for actual send eligibility.

## ANTI-PATTERNS

- **DO NOT** call Twilio directly from routes when an existing service path already exists.
- **DO NOT** store raw customer-managed secrets outside `provider_secret_service.py`.
- **DO NOT** mix tenant-agnostic platform operations with tenant-scoped workspace logic without being explicit about the boundary.
