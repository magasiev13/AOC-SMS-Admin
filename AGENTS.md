# Twinevia

Flask messaging SaaS with tenant-scoped workspaces, Stripe billing, and provider management. Use `Twinevia` in docs and UI; preserve literal compatibility names where operationally required.

## Runtime boundary

- The supported production target is `SAAS_MODE=1`, PostgreSQL, Redis/RQ queue `twinevia-saas`, deploy root `/opt/twinevia-saas`, and the `twinevia-saas*` systemd family. Use Python 3.11.
- `SAAS_MODE=0`, SQLite, and `dbdoctor` remain for legacy imports, tests, and local compatibility only. Do not reuse the retired legacy production path or queue `sms` for SaaS work.
- Use `app.saas_db` / `twinevia-saas-dbdoctor` for SaaS schema work; `saas-dbdoctor` is a compatibility alias. Never run legacy `app.dbdoctor` against PostgreSQL.
- Production deployment uses `deploy/install_saas.sh` and `deploy/deploy_twinevia_saas.sh`; read `deploy/AGENTS.md` for changes in that scope.

## Project invariants

- Reuse tenant scoping and query helpers instead of hand-rolled organization filters. Cross-tenant platform operations must have an intentional admin boundary.
- Staff contact privacy uses the active organization membership. Never emit phone fields, phone-derived identifiers or readable recipient snapshots to staff; dataset exports/raw-number management require owner/admin authority. Keep opaque machine tokens separate from display-only redaction.
- Event feedback purpose and session context are distinct from registration surveys. Feedback completion must never modify attendee names, registrations, booking spaces or contact identity.
- Session invalidation depends on `session_nonce`; preserve the password/contact gates in `app/auth.py`.
- Use RQ workers and explicit systemd timer paths for background work, not long-lived threads from request handlers.
- Customer-managed provider secrets belong encrypted in the database through the provider secret helpers, not in environment files or logs.
- `/health` returns plain `OK`; do not assume JSON.
- Add dependencies only with authorization for the current task.

## Task entrypoints

- Startup validation: `app/__init__.py` and `app/config.py`.
- Tenant context and ORM scoping: `app/tenant.py`; account gates: `app/auth.py`.
- Billing and provider lifecycle: `app/services/`, with narrower guidance in its `AGENTS.md`.
- Legacy snapshot imports: `app/services/legacy_import_service.py`.
- Local acceptance environment: `run/local_saas_stack.sh` and `run/seed_demo_saas.sh`. Check configured database and provider targets before setup or seed commands.

## Validation

Follow `tests/AGENTS.md` and run the smallest relevant existing checks for the behavior changed. Available commands include `./run/verify.sh`, `./run/test.sh`, and `./run/test_browser.sh`. Broaden checks when shared behavior or failures justify it.
