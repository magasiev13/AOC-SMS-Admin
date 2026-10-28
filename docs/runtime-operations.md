# Maintenance and code recovery

Use the installed, root-owned supervisor at `/usr/local/lib/twinevia-saas/runtime_control.py`. The installed rollback entrypoint is `/usr/local/lib/twinevia-saas/rollback_twinevia_saas.sh`; its adjacent `runtime_state.sh` supplies the shared transaction policy. These files remain available when the application `current` link changes, including a permitted legacy recovery. Install and deployment synchronize these reviewed files; the application account must not own or modify them.

This runbook covers the supported `/opt/twinevia-saas` PostgreSQL/Redis/RQ installation. It does not authorize messages, charges, provider changes, customer-data deletion or restoration. Use the authorization already established for the particular operational task.

## Observe the current state

These commands inspect identity and units without starting work:

```bash
sudo /usr/bin/python3 /usr/local/lib/twinevia-saas/runtime_control.py --help
sudo /usr/bin/python3 /usr/local/lib/twinevia-saas/runtime_control.py units
sudo /usr/bin/python3 /usr/local/lib/twinevia-saas/runtime_control.py units --kind timers
sudo readlink -f /opt/twinevia-saas/current
sudo cat /opt/twinevia-saas/current/release.json
sudo systemctl list-timers 'twinevia-saas-*' --no-pager
```

The canonical runtime is the web and RQ worker services plus six timers: scheduler, billing reconciliation, platform restart queue, A2P reconciliation, backup and readiness. Every timer has a paired oneshot service. Do not enable or directly start a paired oneshot as a substitute for the timer policy.

For an authenticated, source-bound readiness proof, load the trusted production environment in a root shell without shell tracing. This keeps the token out of command arguments and output:

```bash
sudo /bin/bash -s <<'ROOT'
set -euo pipefail
set -a
source /opt/twinevia-saas/.env
set +a
/usr/bin/python3 /usr/local/lib/twinevia-saas/runtime_control.py check \
  --app-root /opt/twinevia-saas \
  --url 'http://127.0.0.1:8100/ready?format=json'
ROOT
```

`check` validates the captured runtime, trusted release bytes, current process source and authenticated readiness. It returns success for normal readiness or an exact valid maintenance state with no unrelated failures. Production process verification also checks the web/worker MainPID working directories against the selected physical release. `/health` remains plain `OK`; it alone does not prove readiness or identify all running code.

| State | Public readiness | Operational meaning |
| --- | --- | --- |
| Normal | `READY`, JSON `ready:true`, `state:ready` | Required services and timers are active; no maintenance hold is declared. |
| Declared maintenance | HTTP 503, JSON `ready:false`, `state:maintenance` | The exact declared timers and their jobs are held, and all independent readiness checks pass. |
| Unverified or expired hold | HTTP 503, not maintenance acceptance | The declaration cannot authorize acceptance or resume. Work stays paused until explicit review. |
| Other readiness failure | HTTP 503, `state:not_ready` | Inspect the failing checks. A maintenance declaration cannot hide another failure. |

## Pause or renew an intentional hold

`pause` and `resume` require the root operator. Replace `REVIEWED_40_CHARACTER_SHA` below with the full SHA from the reviewed current release record; do not substitute an unreviewed checkout HEAD. Give a concrete reason without credentials or customer-message content. Repeat `--unit` to name multiple timers.

```bash
sudo /usr/bin/python3 /usr/local/lib/twinevia-saas/runtime_control.py pause \
  --app-root /opt/twinevia-saas \
  --expected-sha REVIEWED_40_CHARACTER_SHA \
  --unit twinevia-saas-billing-reconcile.timer \
  --reason 'Hold billing reconciliation while the historical-usage repair is verified.'
```

Pause stops the named timers and their paired jobs, verifies containment and writes a declaration. Existing declared holds remain included. If stopping or verification fails, the command does not claim that maintenance was established; inspect actual units and the retained evidence before proceeding.

The declaration lasts 24 hours. Expiry **never resumes work**. It makes readiness unverified and blocks automatic acceptance/resume. After reviewing why the hold is still necessary, explicitly run `pause` again with the reviewed current SHA, timer and updated reason. The same command renews an expired declaration, recontains all retained holds and records a fresh 24-hour window. Renewal does not start a timer or erase another hold.

The declaration is `/var/lib/twinevia-saas/runtime-maintenance.json`. Transaction captures, resume requests and action records are under `/var/lib/twinevia-saas/runtime/`. Preserve them as operational evidence. The directory and records are trusted operator state: root-owned, not group/world writable, and not editable by the application account. Do not delete or alter a declaration to turn a failed proof into a green result.

## Resume reviewed work once

Resume requires the exact current SHA, explicit held timers, a reason, authenticated readiness and a **stable, nonempty `--request-id`**. Choose that ID for the approved operational action and retain it with the incident record. The command below loads authentication from the trusted environment without printing it:

```bash
sudo /bin/bash -s <<'ROOT'
set -euo pipefail
set -a
source /opt/twinevia-saas/.env
set +a
/usr/bin/python3 /usr/local/lib/twinevia-saas/runtime_control.py resume \
  --app-root /opt/twinevia-saas \
  --expected-sha REVIEWED_40_CHARACTER_SHA \
  --unit twinevia-saas-billing-reconcile.timer \
  --reason 'Historical usage is observational; preservation and reconciliation checks passed.' \
  --request-id incident-2026-10-27-billing-resume-v1
ROOT
```

For automation, the CLI also supports `--readiness-token-stdin`; pass the token through a protected pipe rather than a command-line argument. Never use shell tracing around credentials. A successful resume can make already-due work eligible immediately; it is not a status-only operation.

The durable request semantics are:

- Before starting a timer, the supervisor records `prepared`. A successful result becomes `completed` after the hold is updated and the action is audited.
- Retry the **same ID with the same SHA, timer set and reason** when the result is uncertain. A completed replay returns its original result without starting work again. An ID with a different payload is rejected.
- `prepared` or `review_required` means the start may already have happened. Automatic replay is refused. Inspect the timer, paired job, journal, queue and relevant durable billing/send records before authorizing any new operational action. Preserve the original request; a new ID must reflect a separately reviewed action, not an automatic retry workaround.
- On a start error or lost acknowledgement, the supervisor attempts to stop every attempted timer and its paired job and retains review evidence. A containment error requires explicit recovery; it is not a successful rollback/resume.
- A completed result describes that original action. It does not remove a later independent hold. Inspect current readiness after replay rather than interpreting an old success as permission to restart.
- Resuming one held timer leaves all other holds intact. After the final hold is removed, normal readiness must pass again.

Inspect the selected timer and its job after resume, then run the authenticated `check` command above. Retain the request ID, action result, selected SHA and journal evidence.

## Preserve enablement and masks

Deployment, code rollback and platform restart preserve the captured active/inactive state and unit-file enablement. A disabled-but-active unit stays disabled; an `enabled-runtime` unit is not silently made persistent across reboot. An inactive timer stays inactive. The operator declaration explains intentional inactive timers to release verification and readiness.

No operation implicitly unmasks a timer or its paired job. Masked definitions remain masked when reviewed templates are installed. Resume rejects masks, running/unavailable paired jobs and enablement drift. If a separate operator decision intentionally changes a mask or enablement, review that change and re-declare the hold before requesting resume. Externally linked definitions are refused before install/deploy template writes; preserve and reconcile them explicitly rather than replacing their links.

The platform **Restart active services** action uses the installed supervisor and cannot grant resume authority. It preserves paused work. Do not replace this flow with a blanket `systemctl restart` of every timer: restarting a held timer can make due operations eligible.

## Deployment and first installation

Use the reviewed deployment entrypoint as root:

```bash
sudo /opt/twinevia-saas/deploy/deploy_twinevia_saas.sh
```

The outer supervisor locks the operation and captures the original unit state once. Nested install/release/recovery scripts reuse that capture. Timers are contained, active oneshots are allowed to finish or containment fails, and workers are stopped before a source switch. Activation preserves the original operating state; declared holds are rebound to the selected release without resuming them. Failure paths contain candidate work **before** switching source/configuration back, then verify restoration. A containment/restore failure leaves work held for explicit recovery and must not be reported as success.

`TWINEVIA_RUNTIME_STATE_FILE` and the `transaction`, `quiesce`, `activate`, `restore`, `render-unit` and `--state` interfaces are internal to that live supervisor. Do not inject or reuse an old snapshot to bypass capture, lock, identity or readiness checks.

Only a first installation on a truly empty Twinevia host may request default activation:

```bash
sudo TWINEVIA_INITIALIZE_RUNTIME=1 /bin/bash /opt/twinevia-saas/deploy/install_saas.sh
```

Follow the normal installation prerequisites first. The fresh guard requires no existing current release identity and none of the canonical web/worker/timer or paired oneshot units. Existing application data/queued work must be reviewed rather than treated as a new installation. The flag is not a repair, reset or force-enable option; routine deployments omit it. A failed fresh activation contains jobs and leaves units stopped/disabled instead of pretending to restore a nonexistent prior runtime.

## Code rollback and its limits

Use the stable installed rollback entrypoint. With no argument it selects `previous`; an argument selects one retained release directory name:

```bash
sudo /bin/bash /usr/local/lib/twinevia-saas/rollback_twinevia_saas.sh
sudo /bin/bash /usr/local/lib/twinevia-saas/rollback_twinevia_saas.sh RELEASE_DIRECTORY_NAME
```

The two lines are alternatives, not consecutive recovery steps. Inspect and review the intended target first. Rollback verifies containment, trusted release identity/ownership, Git-matching source, unexpected source additions, schema compatibility and the historical observation boundary. It then switches the link and verifies the captured operating state, process source, health and authenticated readiness. It does not reverse additive database migrations, charges, sent messages or provider changes. On failure it attempts contained restoration of the original release; failed containment leaves explicit recovery work.

Modern recovery targets must carry reviewed `deploy/runtime-contract.json` version 1 with `historical_usage_observation_boundary:true`, `logout_session_revocation:true` and `active_survey_session_guard:true`. The current contract covers schema 028 and additionally requires `feedback_registration_isolation:true`, `feedback_command_isolation:true` and `staff_phone_privacy:true`. Those boundaries keep feedback answers and feedback commands separate from attendee identity, including when a registration session is waiting for a name, and keep contact numbers out of staff responses, tokens and exports. The contract is a compatibility assertion backed by reviewed source and tests, not permission to roll arbitrary old code over a newer database. Source/identity and expected files must be owned by the trusted operator, not group/world writable, with unchanged reviewed bytes. Preserve a differing retained release/hotfix and reconcile it; do not edit metadata, hashes or contract declarations to make it pass.

The prior exact commit `25cd1109cca886848f61425dd18439d6b8bbc9ff` remains inspectable while it is current at schema 027. Because schema 028 is additive, the **original first-upgrade transaction alone** may recover that exact operating release if promotion fails. This exception requires a live root capture from deploy/release/install, the same application/state roots and exact prior release path/ID/SHA, all eight originally active and available units, no maintenance declaration or captured hold, and unchanged full Git source. It never authorizes schema newer than 028 or waives ownership, masks or process/source checks. It is not a standalone rollback command, a reusable approval, or permission to select a sibling retained copy.

After schema-028 code is accepted, a normal standalone downgrade to the prior schema-027 source is refused. That code can expose numbers to staff and cannot honor the feedback-purpose boundary. Retain and independently verify a separate complete copy of the corrected schema-028 application and Python environment, then pin `previous` to that reviewed recovery target before closing the release. Preserve the former schema-027 release and its backup evidence. The copy/pin does not itself restart services, restore a database, or authorize messages or charges.

The exact first schema-028 commit `8fcb642a79dcf54a18f95be5cf123e7e29d6de92` predates feedback command isolation. It remains inspectable while already current, so its corrective promotion can begin. The **original corrective-promotion transaction alone** may recover that exact operating release if promotion fails, with the same root capture, original path/ID/SHA, normal all-eight-active scope, no held units or maintenance, protected full Git source and maximum schema-028 requirements described above. A captured rollback/restart, another commit, a sibling retained copy or stale transaction cannot use the exception. After the command fix is accepted, a standalone rollback to `8fcb642a` is refused because a feedback keyword can otherwise become a pending registration's attendee name. Retain and verify a complete recovery copy carrying all three schema-028 guards and pin `previous` to it before closing the corrective release. Keep the former `8fcb642a` release and recovery copy as incident evidence; their earlier verification does not qualify them for later standalone rollback. This corrective exception does not widen the exact `25cd1109` first-upgrade exception or any older baseline.

The only pre-contract transition exception is exact commit `ad7814b2946f3bec1c4e543e297e2e443340f619`. The supervisor pins its six historical-safety files **and** verifies the entire Git source. It may be inspected while already current, or restored by the original captured transaction if its original schema-027 upgrade fails. That earlier exception does not extend to schema 028. It requires normal operation with no maintenance declaration or held runtime. It cannot understand the maintenance contract, and the root pause command refuses a new hold before stopping anything.

After the corrected release is accepted, a standalone rollback to `ad7814` is refused: it would reintroduce the account logout and deleted-survey bugs repaired by this package. The first release must retain an independently verified copy of the corrected application and dependency environment as the approved recovery baseline; preserve the old transition source as incident evidence. Pin `previous` only after verifying the retained copy against the reviewed Git identity and runtime safety contract. Creating that retained copy does not restart services, run providers or rewrite customer records. Stable installed control files remain available across source switches.

Do not code-rollback to `6d271209`, a September 2 release, or another older target lacking this boundary. Such a rollback can reintroduce historical recipient/financial mutations or run code incompatible with the additive schema. A previous symlink or old successful boot does not make the target eligible.

If no retained code target is safe, a matching **full encrypted application/database/configuration recovery** is a separately reviewed alternative. Preserve the incident, current data and backup proof first. The shipped `restore_twinevia_saas_backup.sh` performs an isolated restore drill and explicitly refuses the production database; do not repoint it at production or bypass that identity guard. A real recovery plan must account for application/schema/configuration, Redis/RQ and operations accepted externally since the snapshot. Stripe/Twilio state does not rewind with PostgreSQL. Reconcile unknown sends and financial outcomes before enabling recovered work.

## Daily AOC operations remain available

Operational maintenance is distinct from audience history. Past-event registrations leave Current event counts automatically, while Past events retains attendance and offers an explicit **Send follow-up / feedback** action. Manual post-event feedback and frozen follow-up intents remain valid, subject to actual STOP/suppression and existing eligibility. Contactable hides blocked numbers while All contacts retains their records. Existing AOC signup consent needs no new approval. See [current audiences](current-audiences.md) and [the AOC signup contract](aoc-consent-clarification.md).
