# SaaS Demo Data

Use this dataset when you want a realistic local multi-tenant environment without hand-entering organizations, contacts, events, inbox threads, and billing states.

## Seed The Demo

From `/Users/magasiev/Desktop/Projects/AOC-SMS-saas`:

```bash
./run/seed_demo_saas.sh --reset
```

If you want your one real Twilio sender assigned to the internal test business at seed time:

```bash
./run/seed_demo_saas.sh --reset \
  --live-from-number +17207305515 \
  --live-messaging-service-sid MGXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

Notes:

- `--reset` only works for SQLite databases.
- Without `--live-from-number`, every organization stays non-live and safe for local testing.
- The command prints the invite URLs you can open directly.

## Seeded Accounts

- Platform admin:
  - `platform@demo.test`
  - `Platform-pass123!`
  - lands on `/platform`
- AOC SMS Internal owner:
  - `owner@aocinternal.demo.test`
  - `Owner-pass123!`
  - lands on `/dashboard`
- AOC SMS Internal staff:
  - `staff@aocinternal.demo.test`
  - `Staff-pass123!`
- Northstar Fitness owner:
  - `owner@northstar.demo.test`
  - `Owner-pass123!`
- Northstar Fitness staff:
  - `staff@northstar.demo.test`
  - `Staff-pass123!`
- Sunset Realty owner:
  - `owner@sunset.demo.test`
  - `Owner-pass123!`

## Seeded Organizations

- `AOC SMS Internal`
  - status: `active`
  - billing: `trialing`
  - messaging: `active` if a live sender is supplied, otherwise `pending`
  - has owner + staff, pending staff invite, community members, inbox threads, keyword automation, survey flow, events, message history, and a pending scheduled send
- `Northstar Fitness`
  - status: `active`
  - billing: `active`
  - messaging: `pending`
  - has owner + staff, pending staff invite, contacts, event registrations, inbox activity, and a pending scheduled send
- `Harbor Events Co`
  - status: `active`
  - billing: `incomplete`
  - messaging: `pending`
  - has a pending owner invite for onboarding testing
- `Sunset Realty Group`
  - status: `suspended`
  - billing: `past_due`
  - messaging: `pending`
  - has an owner account plus failed scheduled-message history for access-restriction testing

## What This Lets You Test

- Platform admin workflow:
  - platform homepage
  - organizations directory
  - org billing and messaging states
  - owner/staff invite access
- Owner workflow:
  - workspace dashboard
  - billing state messaging
  - pending invitations
  - inbox, logs, community, events, and scheduled messages
- Staff workflow:
  - workspace access
  - billing restriction
- Organization isolation:
  - same app, different orgs, separate contacts and history
- One-number live messaging strategy:
  - one org can carry the real sender
  - all other orgs stay pending and non-live

## Suggested Manual Acceptance Pass

1. Log in as `platform@demo.test` and confirm:
   - `/platform` shows all seeded organizations
   - `/platform/organizations` shows billing and onboarding differences across orgs
2. Log in as `owner@aocinternal.demo.test` and confirm:
   - `/dashboard` loads a populated workspace
   - `/billing` shows `trialing`
   - `/users` shows a pending staff invite
   - `/inbox`, `/community`, `/events`, `/logs`, and `/scheduled` are populated
3. Log in as `owner@northstar.demo.test` and confirm that org data is different from AOC SMS Internal.
4. Log in as `staff@aocinternal.demo.test` and confirm `/billing` returns `403`.
5. Open the Harbor owner invite URL from the seed output and test the onboarding flow.
6. Log in as `owner@sunset.demo.test` and confirm billing restrictions are visible for a `past_due` org.

## Recommended Fake Business Stories

Use the seeded orgs as if they were real customers:

- `AOC SMS Internal`: your own dogfooding org
- `Northstar Fitness`: active customer with staff and event traffic
- `Harbor Events Co`: onboarding customer not yet through billing
- `Sunset Realty Group`: suspended customer with billing problems

This mix is deliberate. It gives you a realistic spread of platform states instead of four healthy tenants that all look the same.
