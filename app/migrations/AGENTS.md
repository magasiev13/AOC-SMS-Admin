# app/migrations/ — Custom SQLite Migration System

NOT Alembic. Numbered Python scripts with `apply(connection, logger)` signature.

## HOW IT WORKS

1. Files named `NNN_description.py` (e.g., `006_add_inbox_automation_tables.py`)
2. Each file exports `apply(connection, logger)` function
3. `runner.py` discovers files, checks `schema_migrations` table, applies pending
4. Applied versions tracked in `schema_migrations` table with timestamp
5. `schema_migration_lock` table prevents concurrent migration runs

## ADDING A NEW MIGRATION

```python
# app/migrations/010_add_new_feature.py

def apply(connection, logger):
    """Add new_column to some_table."""
    # Check if column already exists (idempotency)
    cursor = connection.execute("PRAGMA table_info('some_table')")
    columns = {row[1] for row in cursor.fetchall()}
    
    if 'new_column' not in columns:
        connection.execute(
            "ALTER TABLE some_table ADD COLUMN new_column TEXT"
        )
        logger.info("Added new_column to some_table")
    else:
        logger.info("new_column already exists, skipping")
```

## CONVENTIONS

- **Idempotent**: Always check state before modifying. Use `PRAGMA table_info` for columns, `SELECT name FROM sqlite_master` for tables.
- **Sequential numbering**: Next number = max existing + 1. Zero-padded to 3 digits.
- **Raw SQL only**: Use `connection.execute()` with raw SQL. No ORM.
- **Logger**: Use the provided `logger` parameter for status messages.
- **One concern per file**: Each migration addresses one schema change.
- **SQLite limitations**: No `DROP COLUMN` before SQLite 3.35. No `ALTER COLUMN`. Recreate table if needed.

## RUNNING MIGRATIONS

```bash
python -m app.dbdoctor --apply    # Apply pending
python -m app.dbdoctor --print    # Show status
python -m app.dbdoctor --doctor   # Full health check
```

Auto-runs on systemd service start via `ExecStartPre`.

## ANTI-PATTERNS

- **DO NOT** use Alembic or any other migration framework.
- **DO NOT** use ORM objects inside migrations (models may not match schema at migration time).
- **DO NOT** skip version numbers (causes runner confusion).
- **DO NOT** modify already-applied migrations (tracked by version, not content hash).
