# Twinevia Rebrand Tracker

## Status
- Overall: completed
- Phase 1: completed
- Phase 2: completed

## Summary
The repo now uses **Twinevia** as the customer-facing product brand and the **`twinevia-saas`** family for the primary SaaS runtime.

Completed outcomes:
- UI, docs, and branding defaults use `Twinevia`
- the primary SaaS deploy root is `/opt/twinevia-saas`
- the primary SaaS queue is `twinevia-saas`
- the primary SaaS systemd family is `twinevia-saas*`
- the restart helper is `restart-twinevia-saas-services`

Intentional exceptions that remain:
- the retained SaaS compatibility alias `saas-dbdoctor`
- local SQLite compatibility tooling such as `dbdoctor`
- external repo/worktree naming remains out of repo scope

## Completed Work
- Centralized brand defaults in `app/config.py` and `app/__init__.py`
- Rebranded user-facing UI, docs, and favicon labeling to `Twinevia`
- Updated safe branding defaults such as `TWILIO_PLATFORM_FRIENDLY_NAME`
- Migrated SaaS deploy scripts, systemd unit files, restart helper, docs, workflows, and tests to the `twinevia-saas` runtime family
- Updated SaaS operational examples for queue names, paths, logs, and sample database/backup naming
- Added or updated focused branding and operational tests

## Verification
- `./run/public_readiness_local.sh --run-id twinevia-audit-20260415`
  - result: `18` browser tests passed, `474` backend tests passed, and local signoff artifacts were written under `output/signoff/twinevia-audit-20260415/local`
- interactive Playwright verification against the local browser harness at `http://127.0.0.1:5010`
  - result: workspace login, platform surfaces, owner billing, and mobile signup all rendered `Twinevia` without old-brand leaks
- `./run/naming_audit.sh`
  - result: primary local and SaaS naming surfaces are clean; remaining old identifiers are intentional legacy compatibility references

## Acceptance
- The app renders `Twinevia` everywhere users see the product brand
- The product descriptor remains `Messaging Workspace`
- New platform-managed Twilio resource naming defaults use `Twinevia`
- The primary SaaS runtime now uses the `twinevia-saas` service, path, queue, log, and restart-helper family consistently
