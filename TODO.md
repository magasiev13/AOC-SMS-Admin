# TODO

Keep this list current. Once an item is fixed, **update this file by removing the completed item**.

## Fixes to Address

- [ ] Harden phone normalization/suppression handling so invalid inputs (e.g., non-digit strings) do not get stored as "+" or other invalid values. Consider validating phones before writing suppression/unsubscribed records.
- [ ] Add `db.session.rollback()` in CSV import error handlers so failed commits do not leave the session in a bad state.
- [ ] Preserve existing `MessageLog.details` when `send_bulk_job` hits an unexpected exception to avoid losing partial results.
