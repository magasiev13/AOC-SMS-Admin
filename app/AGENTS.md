# app/ — Core Application

This package contains the Flask app, SaaS control-plane logic, workspace routes, tenant scoping, and both schema-management entrypoints.

## PRIMARY ENTRYPOINTS

- `__init__.py`
  - `create_app()` for pure app creation
  - `create_runtime_app()` for runtime startup with schema/bootstrap side effects
- `wsgi.py`
  - loads `.env`
  - creates the runtime app

## PRIMARY MODULES

| File | Purpose |
|---|---|
| `config.py` | All env-backed config and validation defaults |
| `auth.py` | Login surfaces, account gates, role decorator, unauthorized routing |
| `routes.py` | Main blueprint for setup, billing, platform, workspace, and webhook routes |
| `models.py` | ORM models for users, organizations, subscriptions, provider state, workspace data |
| `tenant.py` | Tenant context + automatic ORM scoping |
| `tasks.py` | RQ job entrypoints |
| `queue.py` | Redis/RQ connection helpers |
| `dbdoctor.py` | Legacy SQLite schema CLI |
| `saas_db.py` | Primary SaaS schema/import CLI |

## WHERE TO LOOK

| Task | File |
|---|---|
| Strict startup validation | `__init__.py` |
| Host/proxy behavior | `__init__.py`, `config.py` |
| Role checks and setup routing | `auth.py` |
| Platform org management | `routes.py` |
| Billing and setup routes | `routes.py` |
| Webhooks | `routes.py` |
| Tenant isolation hooks | `tenant.py` |
| Config surface | `config.py` |

## MODEL SURFACE

`models.py` currently includes:

- identity/access models
- organization + invitation + membership models
- subscription, webhook, usage, and restart queue models
- messaging profile and A2P onboarding models
- workspace recipient, log, inbox, keyword, survey, and scheduling models

Use `utc_now()` for stored timestamps and existing validators for phone/keyword normalization.

## CONVENTIONS

- `SAAS_MODE=1` is the primary production assumption.
- Tenant-scoped workspace writes should normally rely on `tenant.py` auto-assignment.
- Platform admins are intentionally blocked from acting as tenant users in workspace routes.
- Customer-managed Twilio credentials must go through the provider secret helpers.
- Use local imports where existing app-factory patterns already do so to avoid circular import problems.

## ANTI-PATTERNS

- **DO NOT** start the scheduler implicitly outside the explicit runtime paths.
- **DO NOT** bypass account-security gates by adding unaudited special routes.
- **DO NOT** assume SQLite-only behavior in new logic or docs.
- **DO NOT** use legacy deploy assumptions (`/opt/sms-admin`, queue `sms`) for SaaS work.
