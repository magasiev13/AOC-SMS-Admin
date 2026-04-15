# Relayn Public Readiness Signoff

Canonical release gate for SaaS customer readiness.

The release is ready only when all of the following are true:

- the deterministic local gate passes
- the beta snapshot and walkthrough pass
- `/health` returns HTTP 200
- the sourced `saas-dbdoctor --doctor` check exits `0`
- required `sms-saas*` services and timers are active
- no `P0` or `P1` issues remain

`P2` issues are acceptable only when they do not affect:

- signup
- login
- billing
- onboarding
- invite acceptance
- tenant isolation
- workspace readiness state
- outbound message safety

## Local Gate

Run:

```bash
./run/public_readiness_local.sh
```

Artifacts land in:

```text
output/signoff/<run-id>/local/
```

The local gate currently collects:

- browser smoke run via `./run/test_browser.sh`
- backend pytest run via `./run/test.sh`
- static verification via `./run/verify.sh`

The local acceptance signal must still cover these product outcomes:

- self-serve signup to `/setup`
- checkout completion and return handling
- billing state messaging after checkout
- owner setup progression and messaging-readiness copy
- staff invite acceptance
- staff restriction on billing and platform surfaces
- platform review of org access, billing, and messaging state
- blocked states for incomplete billing, incomplete messaging, and suspended orgs
- tenant isolation across multiple seeded organizations

## Beta Snapshot Gate

Use one reusable control tenant by default:

- slug: `public-readiness-control`

Collect snapshots at meaningful milestones:

1. baseline
2. post-signup or post-owner-invite acceptance
3. post-checkout return
4. post-staff-invite acceptance
5. post-messaging/onboarding review

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

Each snapshot should include:

- live commit SHA
- `/health` result
- sourced `saas-dbdoctor --doctor` output
- `sms-saas*` service and timer state
- worker log tail
- organization subscription, invite, membership, messaging, and A2P state
- Twilio ownership details when a sender already exists

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

The walkthrough should include:

- one real Stripe test checkout on the control tenant
- correct return to setup or billing state
- owner dashboard blocked-send messaging until the workspace is truly ready
- one staff invite acceptance
- visible UI state matching DB and worker/log state

## Twilio Safety Rules

- keep beta Twilio checks read-only during signoff when possible
- do not submit new A2P packets solely for signoff
- do not buy numbers solely for signoff
- do not attach parent-account numbers solely for signoff
- if a sender already exists, verify ownership and assignment instead of re-provisioning it
- if Twilio reports a structural blocker, stop and mark the launch blocked instead of retrying around it

## Hard Blockers

- broken signup or login
- failed checkout return
- invite acceptance failures
- owner/staff permission leaks
- tenant data leakage
- false “ready to send” state
- Twilio ownership mismatch
- non-green health
- schema drift
- worker failures affecting onboarding or billing state

## Health Definition

Health is green only when all of these are true:

- the public health URL returns HTTP 200
- the local service health endpoint returns HTTP 200 with the right `Host` header when `TRUSTED_HOSTS` is enforced
- the sourced `saas-dbdoctor --doctor` check exits `0`
- required SaaS services and timers are active
- the signoff tenant has no unresolved onboarding or billing worker failures relevant to the current milestone

Current health response detail:

- the app returns plain `OK`, not JSON
