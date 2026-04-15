# app/migrations/ — Legacy SQLite Migrations

This directory is the legacy SQLite migration system.

Use it for:

- compatibility changes to the legacy `dbdoctor` path
- SQLite-backed local/demo compatibility work that truly belongs to the legacy schema runner

Do not use it for primary SaaS PostgreSQL schema work. SaaS schema files belong in `app/saas_migrations/`.

## HOW IT WORKS

1. files are named `NNN_description.py`
2. each file exports `apply(connection, logger)`
3. `app.migrations.runner` discovers and applies pending versions
4. applied versions are tracked in `schema_migrations`

## CONVENTIONS

- keep migrations idempotent
- use raw SQL on the provided connection
- check state before changing schema
- do not edit already-applied migrations
- keep one schema concern per file

## ANTICIPATED SPLIT

- legacy SQLite path -> `app/migrations/`
- primary SaaS path -> `app/saas_migrations/`

If the change is SaaS-first, start in `app/saas_migrations/` and only add a legacy migration here when the compatibility runtime genuinely needs it.
