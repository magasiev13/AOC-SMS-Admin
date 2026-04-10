# Public Readiness Signoff

Canonical release gate for first-customer readiness.

The app is ready only when:

- the deterministic local gate passes
- the beta walkthrough passes
- `/health` is HTTP 200
- `saas-dbdoctor --doctor` exits `0`
- required `sms-saas*` services and timers are active
- no `P0` or `P1` issues remain

`P2` issues are acceptable only if they do not affect signup, auth, billing, onboarding, messaging readiness, invites, or tenant isolation.

## Local Gate

Run:

```bash
./run/public_readiness_local.sh
```

Artifacts land in:

```text
output/signoff/<run-id>/local/
```

The local gate must cover:

- self-serve signup to `/setup`
- offline fake checkout return through the real success path
- billing state messaging after checkout
- owner setup business-profile save and A2P submit
- dashboard wait-state and disabled send messaging while approval is pending
- owner-created staff invite acceptance
- staff `403` on billing and platform routes
- platform review of onboarding, billing, and messaging state
- pending owner-invite flow
- blocked states for pending A2P, past-due billing, and suspended workspaces
- tenant isolation across at least two seeded organizations, including one direct route denial
- mobile sanity for setup, billing, and platform primary actions

## Beta Gate

Use one reusable control tenant by default:

- slug: `public-readiness-control`

If that slug does not exist yet, create a dedicated control tenant once and keep reusing it for signoff.

Capture a beta snapshot at each milestone:

1. Baseline before login or onboarding changes
2. Post-signup or post-owner-invite acceptance
3. Post-checkout return
4. Post-staff-invite acceptance
5. Post-messaging or onboarding review

Run:

```bash
./run/public_readiness_beta_snapshot.sh \
  --org-slug public-readiness-control \
  --label baseline
```

Artifacts land in:

```text
output/signoff/<run-id>/beta/<label>/
```

Each snapshot must include:

- live commit SHA
- `/health` result
- `saas-dbdoctor --doctor`
- required `sms-saas*` service and timer status
- worker log tail
- organization subscription, membership, invitation, messaging, and A2P state
- Twilio subaccount, Messaging Service, and `PN...` ownership output when already provisioned

## Live Walkthrough

Validate these routes on beta:

- `/login`
- `/signup`
- `/setup`
- `/billing`
- `/dashboard`
- `/users`
- `/platform`
- `/platform/organizations`
- `/platform/organizations/<id>/messaging`
- `/platform/organizations/<id>/messaging/onboarding`

The beta walkthrough must include:

- one real Stripe test checkout on the control tenant
- return to the correct post-checkout setup or billing state
- owner dashboard readiness copy and blocked send state while approval is incomplete
- one staff invite acceptance and staff access restrictions
- visible UI state matching worker logs and persisted DB state after each milestone

Use Stripe test-mode card values from Stripe’s official testing documentation:

- [Stripe testing docs](https://docs.stripe.com/testing)

## Twilio Safety Rules

- Beta Twilio checks are read-only during signoff.
- Do not submit new A2P packets solely for signoff.
- Do not buy numbers during signoff.
- Do not attach parent-account numbers during signoff.
- If a sender already exists, confirm the org subaccount SID, Messaging Service SID, and `PN...` ownership.
- If Twilio returns a `direct_customer` or Trust Hub architecture blocker, stop and mark launch blocked rather than retrying around it.

## Hard Blockers

- broken signup or login
- failed checkout return
- invite acceptance failures
- owner or staff role leaks
- tenant data leakage
- false “ready to send” state
- Twilio ownership mismatch
- non-green health
- schema drift or failed doctor run
- worker errors affecting onboarding or billing state

## Health Definition

Health is green only when all of these are true:

- `https://beta.theitwingman.com/health` returns HTTP `200`
- `saas-dbdoctor --doctor` exits `0`
- required SaaS services and timers are active
- the signoff tenant shows no onboarding or billing worker failures relevant to the current milestone

Do not rely on older documentation that expected a JSON health payload. The current app returns a simple HTTP `200 OK` response body.
