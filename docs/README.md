# Relayn Documentation

This directory is the maintained documentation set for Relayn.

Documentation is SaaS-first:

- canonical production target: `SAAS_MODE=1`
- canonical database: PostgreSQL
- canonical deploy root: `/opt/sms-saas`
- canonical service family: `sms-saas*`

The legacy `SMS Admin` SQLite runtime remains documented only where required for compatibility or migration.

## Start Here

- [Architecture](architecture.md): system boundaries, tenant scoping, jobs, billing, provider flows
- [Configuration](configuration.md): full env-var reference from `app/config.py`
- [Deployment](deployment.md): SaaS production deploy guide plus legacy appendix
- [SaaS Operations](saas-operations.md): day-2 operations, backups, deploy updates, cutover

## Reference

- [API Reference](api.md): current route surface grouped by capability
- [Database](database.md): model domains, tenant scoping, migration systems
- [Services](services.md): business logic and background processing modules
- [CLI Tools](cli.md): `dbdoctor`, `saas-dbdoctor`, local scripts, worker and scheduler entrypoints
- [Troubleshooting](troubleshooting.md): current failure modes and recovery paths

## Release And Rollout

- [Public Readiness Signoff](public-readiness-signoff.md): local and beta evidence gate
- [SaaS Pilot Rollout](saas-pilot-rollout.md): local acceptance and rollout expectations
- [Ubuntu VPS SaaS Checklist](ubuntu-vps-saas-checklist.md): step-by-step VPS bring-up
- [SaaS Demo Data](saas-demo-data.md): seeded tenants, accounts, and manual walkthrough

## Security And Account Operations

- [Security Hardening Summary](security-hardening-summary.md): current production security checklist
- [Auth Hardening Phases](auth-hardening-phases.md): current auth/account-security reference

## Pointers Back To The Repo

- [Main README](../README.md)
- [Environment Sample](../.env.example)
- [Top-Level Agent Guide](../AGENTS.md)
