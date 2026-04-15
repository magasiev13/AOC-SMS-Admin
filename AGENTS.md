# PROJECT KNOWLEDGE BASE

Relayn is a Flask-based multi-tenant messaging workspace with a SaaS control plane, tenant-scoped workspaces, Stripe billing, Twilio provider management, and inbox/survey automation.

The repo still contains the original single-tenant `SMS Admin` runtime, but the SaaS/PostgreSQL path is now the primary production target.

## OVERVIEW

- primary runtime: `SAAS_MODE=1`
- primary database: PostgreSQL
- primary queue: Redis + RQ with `RQ_QUEUE_NAME=sms-saas`
- primary deploy root: `/opt/sms-saas`
- primary systemd family: `sms-saas*`
- supported/tested Python: `3.11`

Legacy compatibility remains for:

- `SAAS_MODE=0`
- SQLite
- `/opt/sms-admin`
- `sms*` units
- `dbdoctor`

## STRUCTURE

```text
./
├── app/                  # Flask app package (see app/AGENTS.md)
│   ├── __init__.py       # App factory, startup validation, scheduler bootstrap
│   ├── auth.py           # Login surfaces, account gates, role decorators
│   ├── config.py         # Env-driven config surface
│   ├── models.py         # 28 ORM models across SaaS + workspace domains
│   ├── routes.py         # Main blueprint: setup, billing, platform, workspace, webhooks
│   ├── tenant.py         # Tenant scoping via ContextVar + SQLAlchemy hooks
│   ├── services/         # Business logic modules (see app/services/AGENTS.md)
│   ├── migrations/       # Legacy SQLite migrations
│   └── saas_migrations/  # Explicit SaaS schema migrations
├── deploy/               # SaaS + legacy systemd/install/deploy helpers (see deploy/AGENTS.md)
├── docs/                 # Maintained documentation set
├── run/                  # Local setup, local stack, tests, signoff, demo seed
├── tests/                # Pytest suite + browser coverage (see tests/AGENTS.md)
├── bin/                  # Installed CLI wrappers
└── wsgi.py               # Runtime entrypoint
```

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| App startup/config validation | `app/__init__.py`, `app/config.py` | Production fail-closed rules live here. |
| Login and access gates | `app/auth.py` | Platform login, workspace login, phone gate, setup routing. |
| Platform/admin routes | `app/routes.py` | `/platform*`, org management, provider config. |
| Workspace routes | `app/routes.py` | `/dashboard`, `/community`, `/events`, `/logs`, `/scheduled`, `/inbox`. |
| Tenant isolation | `app/tenant.py` | ORM criteria + auto-assigned `organization_id`. |
| Billing | `app/services/billing_service.py` | Stripe checkout, webhooks, subscription sync. |
| Twilio provider lifecycle | `app/services/twilio_service.py` | Provisioning, sender sync, outbound sends, usage. |
| Twilio A2P | `app/services/twilio_a2p_service.py` | Draft/save/submit/refresh/reconcile. |
| Legacy import | `app/services/legacy_import_service.py` | SQLite snapshot import into SaaS. |
| SaaS schema CLI | `app/saas_db.py`, `bin/saas-dbdoctor` | Primary schema workflow. |
| Legacy schema CLI | `app/dbdoctor.py`, `bin/dbdoctor` | SQLite-only compatibility workflow. |
| SaaS deploy path | `deploy/install_saas.sh`, `deploy/deploy_sms_saas.sh` | Primary production path. |
| Local SaaS stack | `run/local_saas_stack.sh`, `run/seed_demo_saas.sh` | Preferred local acceptance loop. |

## CONVENTIONS

- **Product naming**: use `Relayn` in docs and UI discussion; keep literal service/path names where operationally necessary.
- **Schema tooling**: use `app.saas_db` / `saas-dbdoctor` for SaaS; use `app.dbdoctor` / `dbdoctor` only for the legacy SQLite line.
- **Tenant safety**: prefer scoped queries and helpers over hand-rolled `organization_id` filters when existing patterns already cover it.
- **Auth safety**: session invalidation depends on `session_nonce`; password/contact gates are enforced in `app/auth.py`.
- **Background work**: production uses systemd timers and RQ workers, not long-lived threads from request handlers.
- **Twilio secrets**: per-org customer-managed secrets belong encrypted in the DB, not in `.env`.

## ANTI-PATTERNS

- **DO NOT** use `dbdoctor` against a SaaS PostgreSQL database.
- **DO NOT** point SaaS services at queue `sms`.
- **DO NOT** mix `/opt/sms-admin` and `/opt/sms-saas` deploy assets.
- **DO NOT** bypass tenant scoping for workspace data unless you intentionally need cross-tenant admin behavior.
- **DO NOT** document or assume `/health` returns JSON; it returns plain `OK`.
- **DO NOT** add new dependencies without approval.

## COMMANDS

```bash
# Local setup
./run/setup.sh

# Local SaaS schema
./venv/bin/python -m app.saas_db --apply
./venv/bin/python -m app.saas_db --ensure-platform-admin
./venv/bin/python -m app.saas_db --doctor

# Local SaaS stack
./run/local_saas_stack.sh --no-open

# Verification
./run/verify.sh
./run/test.sh
./run/test_browser.sh

# Production deploy
sudo ./deploy/install_saas.sh
sudo ./deploy/deploy_sms_saas.sh
```
