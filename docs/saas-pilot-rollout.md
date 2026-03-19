# SaaS Pilot Rollout

This branch adds a separate SaaS pilot deployment line. Do not deploy it over the legacy `sms` services.

## Separate Runtime

- Use a separate checkout path such as `/opt/sms-saas`
- Use separate service names:
  - `sms-saas.service`
  - `sms-saas-worker.service`
  - `sms-saas-scheduler.service`
  - `sms-saas-scheduler.timer`
- Use separate logs under `/var/log/sms-saas`
- Use a separate host or subdomain such as `beta.<host>` or `app.<host>`

## Required SaaS Env

- `SAAS_MODE=1`
- `DATABASE_URL=postgresql+psycopg://...`
- `REDIS_URL=redis://...`
- `RQ_QUEUE_NAME=sms-saas`
- `SAAS_BASE_URL=https://beta.example.com`
- `STRIPE_SECRET_KEY=...`
- `STRIPE_WEBHOOK_SECRET=...`
- `STRIPE_PRICE_ID=...`
- `TWILIO_ACCOUNT_SID=...`
- `TWILIO_AUTH_TOKEN=...`

## Safety Rules

- Keep legacy production on the baseline tag until the pilot is stable.
- Do not share webhook URLs between legacy and SaaS.
- Do not reuse the legacy SQLite database for SaaS.
- Do not point the SaaS worker at the legacy queue name.
