# Twinevia Public Readiness Signoff

Canonical release gate for SaaS customer readiness.

The release is ready only when all of the following are true:

- the deterministic local gate passes
- the production snapshot and read-only live smoke pass
- `/health` returns HTTP 200
- the sourced `twinevia-saas-dbdoctor --doctor` check exits `0`
- required `twinevia-saas*` services and timers are active
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

## Production Snapshot Gate

Use `IT Wingman LLC` as the reusable greenlight tenant:

- slug: `it-wingman-llc`

Collect snapshots at meaningful milestones:

1. baseline
2. post-signup or post-owner-invite acceptance
3. post-checkout return
4. post-staff-invite acceptance
5. post-messaging/onboarding review

Run:

```bash
./run/public_readiness_production_snapshot.sh \
  --org-slug it-wingman-llc \
  --label baseline
```

Artifacts land in:

```text
output/signoff/<run-id>/production/<label>/
```

Each snapshot should include:

- live commit SHA
- live branch and tracking ref
- `/health` result
- sourced `twinevia-saas-dbdoctor --doctor` output
- `twinevia-saas*` service and timer state
- worker log tail
- organization subscription, invite, membership, messaging, and A2P state
- Twilio ownership details when a sender already exists

## Safe Production Cutover

Canonical production is the Twinevia SaaS runtime rooted at `/opt/twinevia-saas`. Treat the live update as an in-place deploy against the existing PostgreSQL database, Redis instance, and the current production `.env`.

Do not:

- point production at a new database
- run a legacy import
- treat the cutover like a fresh-host install
- rename the live PostgreSQL database or Redis instance for branding

Preferred operator flow:

1. pick the exact branch production should follow
2. require the local gate to pass on that branch
3. announce a short freeze window for org edits, invites, billing mutations, sender provisioning, and outbound sends
4. capture a pre-deploy production snapshot and backup bundle
5. run the in-place deploy
6. capture a post-deploy production snapshot and compare parity

Use:

```bash
./run/production_cutover.sh \
  --org-slug it-wingman-llc \
  --freeze-note "Pause org edits, invites, billing mutations, sender changes, and outbound sends." \
  --deploy
```

What the cutover script does:

- runs `./run/public_readiness_local.sh` unless `--skip-local-gate` is set
- captures the pre-deploy production snapshot
- verifies the current live app root and unit family
- locks the remote checkout to the expected branch and tracking ref
- backs up PostgreSQL with `pg_dump`
- captures a Redis backup bundle
- copies the current live `.env` and reverse-proxy evidence off-host into local artifacts
- performs `deploy/deploy_twinevia_saas.sh` only when `--deploy` is supplied
- records pre/post runtime root and user in the cutover artifacts
- captures a post-deploy production snapshot when deploy or canonicalization actually runs

Artifacts land in:

```text
output/signoff/<run-id>/production-cutover/
```

Keep the pre-deploy backups until production signoff is complete.

## Authenticated Live Smoke

Run:

```bash
TWINEVIA_OWNER_USERNAME=owner@example.com \
TWINEVIA_OWNER_PASSWORD=... \
TWINEVIA_PLATFORM_USERNAME=platform@example.com \
TWINEVIA_PLATFORM_PASSWORD=... \
./run/public_readiness_live_smoke.sh
```

Validate these routes on production:

- `/login`
- `/signup`
- `/platform`
- `/platform/organizations`
- `/platform/organizations/<id>/messaging`
- `/platform/organizations/<id>/messaging/onboarding`
- owner `/setup` or `/dashboard`
- owner `/billing`

The live smoke should include:

- public auth surfaces and `/health`
- owner login to the current readiness surface without mutating billing or onboarding
- owner billing and dashboard gating copy
- platform login plus read-only inspection of org directory, messaging, and onboarding pages
- visible UI state matching DB and worker/log state
- no clicks that create invites, start checkout, submit A2P, or mutate provider state

## Twilio Safety Rules

- keep production Twilio checks read-only during signoff when possible
- do not submit new A2P packets solely for signoff
- do not buy numbers solely for signoff
- do not attach parent-account numbers solely for signoff
- do not start a live Stripe checkout solely for signoff
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
- the sourced `twinevia-saas-dbdoctor --doctor` check exits `0`
- required SaaS services and timers are active
- the signoff tenant has no unresolved onboarding or billing worker failures relevant to the current milestone

Current health response detail:

- the app returns plain `OK`, not JSON
