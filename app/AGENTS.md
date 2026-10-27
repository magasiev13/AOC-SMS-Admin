# app/ — Application conventions

- `__init__.py` owns app construction and startup validation. `create_app()` binds configured extensions; `create_runtime_app()` additionally performs runtime bootstrap. The repository-root `wsgi.py` loads the environment and creates the runtime app.
- Start the scheduler only through explicit runtime paths. Preserve existing local imports where required to avoid circular initialization.
- New SaaS logic must work with PostgreSQL. Keep legacy SQLite assumptions confined to compatibility paths.
- Tenant-scoped workspace writes normally use `tenant.py` auto-assignment and ORM scoping. Platform admins are intentionally blocked from acting as tenant users in workspace routes.
- Preserve the account-security and setup gates in `auth.py`; do not create special routes that bypass them.
- Customer-managed Twilio credentials must go through the provider secret helpers.
- Use `utc_now()` for stored timestamps and the existing phone/keyword model validators.
- For host/proxy changes, inspect `__init__.py` and `config.py` together. Routes and webhooks live in `routes.py`; reuse the relevant service for business logic.
