# Twinevia Managed-Pilot Operations

These runbooks are the production operating contract for the first 3–5 managed customers. Production changes, live Stripe changes, Twilio submissions, DNS/certificate changes, AOC cancellation, commits, and deployments remain separate approval gates.

## Required Production State

- Source checkout is clean and points to the reviewed commit.
- `/opt/twinevia-saas/current` resolves to an immutable directory under `/opt/twinevia-saas/releases`.
- `/opt/twinevia-saas/previous` resolves to the last known-good release after the second deployment.
- `/health` returns the exact plain-text body `OK`.
- Token-protected `/ready` returns `READY`.
- PostgreSQL, Redis, the RQ worker, scheduler, billing reconciliation, platform restart queue, A2P reconciliation, backup, and readiness units are active.
- The configured AOC cancellation record proves every dispatchable launch send present at maintenance time was captured and canceled.
- A recent encrypted off-host backup and isolated restore drill are recorded.

## AOC Scheduled-Send Freeze

Effect: records the message content, schedule, target, event information, automation metadata, and audience rule; then changes exactly two AOC rows from `pending` or `processing` to `cancelled`. The scheduler timer remains stopped. The scheduler sends these rows synchronously, so there is no corresponding RQ send job.

After explicit approval:

```bash
sudo APP_ROOT=/opt/twinevia-saas/current \
  TWINEVIA_ENV_FILE=/opt/twinevia-saas/.env \
  /opt/twinevia-saas/current/deploy/cancel_aoc_scheduled_sends.sh \
  --expected-count 2 \
  --confirm-organization-slug armenians-of-colorado
```

Verify:

```bash
sudo systemctl is-active twinevia-saas-scheduler.timer
sudo -u twinevia bash -lc '
  set -a
  source /opt/twinevia-saas/.env
  source /opt/twinevia-saas/current/.release.env
  set +a
  cd /opt/twinevia-saas/current
  ./venv/bin/python -m app.aoc_scheduled_guard \
    --organization-slug armenians-of-colorado \
    --expect-dispatchable-count 0
'
```

The first command must report `inactive`; the guard must show two recorded rows with `cancelled` status and zero dispatchable rows. Do not restart the scheduler manually. A reviewed release enables it only after repeating this guard.

Rollback is recreation, not an automatic status flip. Use the private record at `AOC_SCHEDULED_CANCELLATION_RECORD_FILE` to recreate the content, target, and intended schedule only after explicit approval.

## Reconcile Local and Production Source

Do not reset either dirty tree. Before release review, capture both states:

```bash
git rev-parse HEAD
git status --short --untracked-files=all
git --no-pager diff --binary
git ls-files --others --exclude-standard
```

Store the production output and patch in a restricted maintenance evidence directory. Compare commit IDs and classify each path as reviewed local work, reviewed production hotfix, generated state, or unrelated user work. Bring production-only code back into the local review tree with a patch or an explicit file copy, resolve overlaps manually, and rerun the full suite. A release is intentionally refused until `git status --porcelain --untracked-files=all` is empty.

## Release

Effect: archives the reviewed Git commit, builds a per-release Python 3.11 environment, checks dependencies and expand-only migrations, runs verification, creates an encrypted pre-migration backup, restores and forward-migrates it in the isolated drill database, verifies the AOC freeze, applies production migrations, validates live Stripe configuration, switches the `current` symlink atomically, restarts services, and verifies health/readiness.

```bash
sudo /opt/twinevia-saas/deploy/deploy_twinevia_saas.sh
```

Verify:

```bash
readlink -f /opt/twinevia-saas/current
sudo -u twinevia /opt/twinevia-saas/current/venv/bin/python -m app.saas_db --doctor
curl --fail --silent --show-error -H 'Host: app.twinevia.com' http://127.0.0.1:8100/health
sudo systemctl --no-pager --full status twinevia-saas twinevia-saas-worker
sudo systemctl list-timers 'twinevia-saas-*' --no-pager
```

Expected health body: `OK`. Inspect `/opt/twinevia-saas/current/release.json` for the release ID and source SHA. Retain at least the current and previous releases.

Database changes during the pilot must be additive. `deploy/check_expand_only_migrations.sh` rejects destructive migration statements. Contract/removal migrations require a later, separately reviewed maintenance stage.

## Rollback

Effect: changes only the release symlinks and service processes. Additive database changes remain in place so the previous application release must remain compatible with the expanded schema.

```bash
sudo /opt/twinevia-saas/current/deploy/rollback_twinevia_saas.sh
```

To select a retained release explicitly:

```bash
sudo /opt/twinevia-saas/current/deploy/rollback_twinevia_saas.sh RELEASE_DIRECTORY_NAME
```

The command validates the target schema, atomically switches `current`, restarts units, and requires `OK` from `/health`. If target health fails, it restores the original release automatically.

## Backup and Restore Drill

The passphrase file must be root-readable, stored outside the repository, and escrowed separately. Production uses the scheduled GitHub Actions backup workflow: it creates the encrypted archive on the host, uploads only the encrypted archive and HMAC sidecar as a retained off-host artifact, and records the workflow run as proof. Mounted mode remains available only for supported remote filesystems; local disks are rejected.

Manual backup:

```bash
sudo systemctl start twinevia-saas-backup.service
sudo systemctl --no-pager --full status twinevia-saas-backup.service
sudo jq . /var/lib/twinevia-saas/backup-status.json
```

The archive contains a PostgreSQL custom dump, encrypted provider data from PostgreSQL, the application environment, Nginx, systemd units, the deployed application, metadata, and a SHA-256 manifest. It is encrypted with AES-256-CBC and PBKDF2 before the off-host copy.

Isolated restore only:

```bash
sudo APP_ROOT=/opt/twinevia-saas/current \
  TWINEVIA_ENV_FILE=/opt/twinevia-saas/.env \
  /opt/twinevia-saas/current/deploy/restore_twinevia_saas_backup.sh \
  --archive /absolute/path/to/twinevia-TIMESTAMP-RELEASE.tar.enc \
  --passphrase-file /etc/twinevia-saas/backup-passphrase
```

The target comes from `RESTORE_DRILL_DATABASE_URL` and is matched against `RESTORE_DRILL_DATABASE_NAME`. The script compares live database identities and refuses the configured production database. It decrypts, verifies the manifest, restores with `pg_restore`, applies additive migrations, runs the schema doctor, and records proof in `RESTORE_DRILL_STATUS_FILE`.

## Stripe Incident

1. Do not promise payment state from the browser return URL. Use verified webhook, Checkout Session, Invoice, and PaymentIntent records.
2. Inspect the organization subscription, `stripe_checkout_sessions`, and webhook delivery ledger before replaying anything.
3. Confirm the live account is `acct_1TCY8xEksbf3Q3Fg`, the setup price is `price_1U9Bl6Eksbf3Q3FgcJ0YRJ05`, monthly is `price_1TYtNuEksbf3Q3FgN2B1VqGN`, and annual is `price_1TYtO4Eksbf3Q3FgHzXB9S5b`.
4. Confirm the dedicated endpoint URL is `https://app.twinevia.com/webhooks/stripe`, its required events are enabled, and its signing secret matches production.
5. Re-deliver the exact Stripe event only after checking its event ID and creation timestamp. Idempotency and ordering prevent stale events from overwriting newer state.
6. For payment failure, leave entitlement blocked and let Stripe’s subscription/invoice state reconcile. Do not manually mark the setup fee paid.
7. For an overage issue, inspect the usage settlement, late carry-forward, invoice ID, invoiced units, and period boundaries before creating or voiding external invoices.

Rollback for a bad application release is the release rollback command. Stripe object deletion, refund, invoice voiding, subscription cancellation, or price/portal changes require separate approval in Stripe.

## Twilio Incident

1. Identify the organization and provider mode before inspecting credentials or remote resources.
2. Suspend sending for the affected organization when duplicate or unsafe traffic is possible; do not delete the sender.
3. Inspect `message_dispatch_attempts`. `ambiguous` means Twilio may have accepted the message; never retry it blindly.
4. Match callbacks and Event Streams resources to the same organization, subaccount, Messaging Service, and stored sink/subscription identifiers.
5. Resume A2P provisioning or customer-managed cutover from the persisted checkpoint. List and match remote resources before creating replacements.
6. Respect STOP, unsubscribe, and suppression state even for test traffic.
7. Re-enable only after an entitlement check, sender check, protected test recipient, callback verification, and usage accounting all pass.

Provider approval is never guaranteed. Twinevia absorbs normal first-time Low Volume Standard registration and routine included usage; rejected resubmissions, appeals, customer-caused retries, high-volume upgrades, special vetting, and requested extra numbers are customer-paid.

## Failed or Ambiguous Send

1. Locate the logical send key and per-recipient attempt.
2. `queued` may be claimed once; `sending` requires stale-worker investigation; `sent` is terminal; `failed` may be retried only according to the operation’s bounded policy; `ambiguous` requires provider reconciliation.
3. Compare the Twilio SID, error code, timestamp, message log, scheduled message, and usage candidate.
4. Do not bulk-reset attempts or enqueue the same recipient under a new key to bypass idempotency.
5. Record the incident and customer-visible outcome before any approved resend.

## Customer Offboarding

1. Confirm the organization, requester authority, effective date, retention requirement, and whether the Twilio account is platform- or customer-managed.
2. Disable invitations and outbound entitlement; stop pending and processing sends for that tenant.
3. Export customer data within configured row limits and record the export recipient.
4. Reconcile final usage and any late carry-forward before canceling billing.
5. Cancel or transition Stripe only after explicit approval and verify the resulting webhook state.
6. Release or transfer Twilio resources only after explicit approval. Preserve compliance and audit records required by policy.
7. Rotate customer-managed credentials, invalidate sessions/invitations, and record completion.
8. Data deletion is a separate destructive operation and must follow the reviewed retention/legal decision.

## Domain, Certificate, and Monitoring

After DNS and certificate approval, create A/AAAA records for `twinevia.com`, `www.twinevia.com`, and `app.twinevia.com`, obtain one certificate containing all three SANs, then run:

```bash
sudo /opt/twinevia-saas/current/deploy/install_nginx_twinevia.sh \
  --confirm-dns-and-certificate-ready
```

The installer validates all SANs, tests Nginx, reloads atomically, and restores the prior configuration on failure. Existing `www` webhook, invitation, compliance, billing, and setup callback paths continue to reverse proxy to Flask.

The scheduled GitHub Actions monitor checks `https://app.twinevia.com/health`, the authenticated readiness endpoint, public product identity, and security headers from outside the host. It opens or updates a repository incident on failure and closes it after recovery. The internal readiness timer independently checks PostgreSQL, migrations, Redis, queue round-trip, RQ heartbeat, backup proof, restore proof, the AOC send freeze, and required systemd units.

## Launch Approval Package

Before requesting commit/deploy approval, provide:

- clean diff and source SHA;
- migration list and expand-only result;
- dependency audit result;
- full backend, browser, verification, and readiness results;
- AOC private-record location and zero-dispatchable proof;
- backup archive hash, off-host path, and restore-drill proof;
- current/previous release paths and rollback command;
- live Stripe endpoint/portal/price validation;
- monitor and alert proof;
- external legal review confirmation for public policies;
- protected internal full-flow result.
