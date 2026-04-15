# Twinevia Demo Data

Use the demo seed when you need a realistic local multi-tenant environment without hand-entering organizations, contacts, inbox threads, billing states, and scheduled sends.

## Seed The Demo

```bash
./run/seed_demo_saas.sh --reset
```

Optional live sender assignment for the internal dogfooding org:

```bash
./run/seed_demo_saas.sh --reset \
  --live-from-number +15551234567 \
  --live-messaging-service-sid MGXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

Notes:

- `--reset` works only for SQLite databases
- without `--live-from-number`, every org remains safe for non-live local testing
- the command prints invite URLs that you can open directly

## Seeded Accounts

- platform admin: `platform@demo.test` / `Platform-pass123!`
- Twinevia Internal owner: `owner@twineviainternal.demo.test` / `Owner-pass123!`
- Twinevia Internal staff: `staff@twineviainternal.demo.test` / `Staff-pass123!`
- Northstar Fitness owner: `owner@northstar.demo.test` / `Owner-pass123!`
- Northstar Fitness staff: `staff@northstar.demo.test` / `Staff-pass123!`
- Sunset Realty Group owner: `owner@sunset.demo.test` / `Owner-pass123!`

## Seeded Organizations

### `Twinevia Internal`

- org status: `active`
- billing: `trialing`
- messaging: `active` when a live sender is supplied, otherwise `pending`
- includes owner and staff accounts, pending invite state, recipients, events, inbox activity, keyword automation, survey data, logs, and a pending scheduled send

### `Northstar Fitness`

- org status: `active`
- billing: `active`
- messaging: `pending`
- includes owner and staff, recipients, event registrations, inbox activity, and a pending scheduled send

### `Harbor Events Co`

- org status: `active`
- billing: `incomplete`
- messaging: `pending`
- includes a pending owner invite for onboarding testing

### `Sunset Realty Group`

- org status: `suspended`
- billing: `past_due`
- messaging: `pending`
- includes failed scheduled-message history for restriction testing

## What The Seed Is Good For

- platform admin organization review
- owner setup and billing state testing
- staff restriction checks
- tenant isolation checks
- populated workspace pages
- pending and blocked-state UI review

## Suggested Manual Pass

1. log in as `platform@demo.test`
2. confirm `/platform` and `/platform/organizations` show varied tenant state
3. log in as `owner@twineviainternal.demo.test`
4. confirm `/dashboard`, `/billing`, `/users`, `/inbox`, `/community`, `/events`, `/logs`, and `/scheduled` are populated
5. log in as `owner@northstar.demo.test` and confirm the org data is distinct
6. log in as `staff@twineviainternal.demo.test` and confirm `/billing` is denied
7. open the Harbor owner invite and confirm the owner lands on `/setup`
8. log in as `owner@sunset.demo.test` and confirm suspended/past-due restrictions are visible

## Intent Of The Dataset

The seed deliberately mixes:

- healthy orgs
- onboarding orgs
- billing-blocked orgs
- suspended orgs

That gives local testing a realistic spread of product states instead of multiple identical healthy tenants.
