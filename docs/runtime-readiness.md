# Runtime readiness during maintenance

The canonical runtime is defined in `deploy/runtime_control.py`: web and RQ worker
services plus scheduler, billing reconciliation, platform restart, A2P
reconciliation, backup and readiness timers. Readiness checks all eight units.
Shortening `READINESS_REQUIRED_SYSTEMD_TIMERS` cannot remove a canonical check.
Use the reviewed maintenance operation to declare an intentional pause.

## Public operational status

`/health` continues to return plain `OK`. The authenticated `/ready` endpoint
keeps its existing plain response and HTTP contract:

- Ready: HTTP 200, `READY`.
- Maintenance or failure: HTTP 503, `NOT READY`.
- Responses are not cached. The existing readiness token is still required.

Request `Accept: application/json` for structured diagnostics. In addition to
`ready`, `checked_at`, `release_id` and individual `checks`, the response includes:

- `state`: `ready`, `maintenance` or `not_ready`.
- `release_sha`: the immutable source identity supplied by `.release.env`.
- `units`: all eight canonical names mapped to raw active, sub, load and unit-file
  states, plus whether each unit belongs to the declared hold.
- `maintenance`: operator, reason, declaration/expiry times, reviewed source and
  held units from the trusted receipt, when valid.
- `warnings`: for example, an active but disabled unit may not start on reboot.

An active-but-disabled unit is still reported as active. Its enablement is not
silently changed or mislabeled as an inactive service.

## A maintenance declaration is not a readiness waiver

`RUNTIME_MAINTENANCE_FILE` defaults to
`/var/lib/twinevia-saas/runtime-maintenance.json`. The core reader requires a
root-owned receipt that is not writable by group or others. The file must be
readable by the application, contain valid timestamps, and name the exact current
`APP_RELEASE_SHA`. Application code only reads it. It cannot declare, resume,
enable or unmask units.

The JSON state is `maintenance` only when the current inactive timers exactly
match that receipt, their recorded states still match, and every unrelated check
passes. Database, schema, Redis, queue, worker, configuration, backup, restore or
unexpected unit failures produce `not_ready`, even during a declared pause. A
missing, malformed, untrusted, expired or source-mismatched receipt cannot make
an inactive timer healthy. Expiry never resumes work automatically.

Private deployment acceptance is separate from public operational readiness. The
root runtime controller verifies the exact declaration, source and independently
observed unit state before accepting a candidate with maintenance retained. It
does not change the public response to `READY`. The `--infrastructure-only` CLI
mode remains an isolated diagnostic and is not a maintenance acceptance shortcut.

## Operator feedback and monitoring

The platform page shows a compact maintenance notice and a detailed record beside
the restart controls. It retains the actor, reason, UTC dates and reviewed source.
The action is labeled **Restart active services**; it does not imply that paused
work resumes or that the platform becomes ready. Invalid maintenance state has an
explicit explanation and disables that control until host review.

The scope is **billing reconciliation paused**, rather than all billing stopped.
Stripe renewals, webhook handling and user-initiated Checkout may continue while
that timer is held.

Both host and GitHub monitoring retain non-readiness during maintenance. The host
does not send its healthy heartbeat. GitHub monitoring continues independent
product/security checks and cannot close the uptime incident unless the job
succeeds and readiness is actually `ready`. Maintenance diagnostics include the
operator, reason, held units, dates and reviewed source. Monitoring is not disabled
to make deployment pass.

## Verification

`tests/test_maintenance_readiness.py` covers exact holds, stale/invalid receipts,
all eight raw unit facts, independent failures, enablement changes, active-disabled
diagnostics, authenticated plain/JSON responses, platform output escaping and
disabled controls. Its shell fixtures execute the actual host monitor and GitHub
incident scripts with fake commands, verifying zero healthy heartbeat or issue
closure during maintenance. The existing `tests/test_readiness.py` is preserved.

Desktop and mobile browser evidence lives under
`output/maintenance-recovery-2026-10-27/readiness/browser/`. It uses a private
loopback stack, fake providers and a disposable database. Receipt ownership is
injected at the local browser-fixture boundary; production still requires root.
Real systemd promotion/rollback and root-provenance verification belong to the
separate runtime-controller acceptance suite.

See [runtime operations](runtime-operations.md) for the first-upgrade recovery exception and the required corrected recovery baseline. Account sign-out behavior is documented in [account sign-out](account-signout.md).
