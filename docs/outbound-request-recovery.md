# Durable outbound requests

SaaS dashboard broadcasts and manual inbox replies persist one immutable `OutboundRequest` before queue or provider submission. The request belongs to an organization and actor, and binds its form key to the normalized content and intended purpose/event/test audience. Its payload retains the exact deduplicated, eligible recipient snapshot. A replay cannot add current directory members or replace the content.

A fresh rendered form issues a new key. Repeating a key returns the existing result; changing its content is a conflict. A new form cannot bypass an identical pending, queued, processing or ambiguous intent. Once the earlier operation has a known outcome, a deliberate new send can use a new form key. Already-open pre-upgrade forms without keys use a stable compatibility identity, so an old browser retry cannot manufacture a new send.

## States and boundaries

| State | Meaning | Recovery |
|---|---|---|
| pending | Intent committed; queue acknowledgement not yet established | Enqueue the same saved request |
| queue_unknown | Enqueue began but its response was lost | Inspect deterministic RQ identity; retain original payload |
| queued | Queue acknowledged this request | Wait for the worker or recover a missing job |
| processing | Execution began | Inspect/replay original recipient attempts after interruption |
| accepted | All recorded eligible submissions completed without known failure | Provider acceptance, not recipient delivery |
| failed | Known rejection, blocked/empty audience, or incomplete operation failure | Review partial results before a deliberate replacement |
| ambiguous | A provider submission may have occurred without a known result | Automatic resend blocked; use evidence review |

The RQ job ID is `twinevia-send-{organization_id}-{request_id}`. A database lease prevents concurrent enqueuers. Queue acknowledgement failure never deletes the request/log or creates a replacement. A worker starting late cannot overwrite a terminal request. Every broadcast recipient retains the `blast-log:{log_id}` attempt namespace, and each saved reply uses `reply-request:{request_id}`. Replaying an accepted or unresolved attempt does not invoke the provider.

Finalization locks the log and request, refreshes persisted attempts, and commits counts/result together. A delayed duplicate cannot downgrade accepted history or replace a known SID with a current permission exclusion. Reply result and its one inbox entry commit together; recovery can fill a missing entry from a known attempt even if billing/provider readiness or recipient opt-out changed afterward. Those current gates still block a new submission.

Permission is checked during audience preview, snapshot preparation, queued/scheduled execution and final provider submission. Internal tests are limited to the intersection of the frozen recipients and the current protected saved test list. Later additions do not widen an earlier request. Opt-outs and transport revocations apply to tests and replies too. See [recipient-permissions.md](recipient-permissions.md).

## Recovery operation

The existing SaaS systemd scheduler's `deploy/run_scheduler_once.sh` now runs outbox recovery before scheduled work. It uses the configured PostgreSQL database, Redis and `twinevia-saas` queue, with no request-handler thread or new long-running service. Missing/failed jobs can be recreated under the deterministic identity; active queued/started/deferred/scheduled jobs are left in place. Provider calls remain in RQ workers. Recovery leases expire after 60 seconds; normal queued/processing inspection is due after two minutes, and enqueue errors are due again after 30 seconds.

The explicit operational equivalent is:

```bash
python -m app.outbound_recover
```

This scans due requests across organizations and enqueues work. Confirm the intended environment/database/queue first. It is an operational action, not a read-only diagnostic. To inspect or reconcile ambiguity, use the separately guarded workflow in [outbound-review.md](outbound-review.md); never clear an attempt or mint a replacement key to test whether a provider accepted it.

Normal validation/provider 4xx rejection is known failure. Verified Twilio HTTP 429 rejection is safe to retry under the same attempt identity, with a four-call ceiling; RQ/scheduler recovery supplies backoff. Timeouts, 5xx responses and untyped transport exceptions retain ambiguity and are not automatically resubmitted. This distinction follows [Twilio's 20429 contract](https://www.twilio.com/docs/api/errors/20429). A retry cap is not a promise of exactly-once handset delivery.

The safe retry classification requires the actual Twilio HTTP 429 exception retained as the transient error's cause; matching an error string is insufficient. The first three confirmed rejections return that same attempt to queued. A fourth confirmed rejection is retained as failed with `rejected_rate_limit` evidence and no provider SID. Replaying that exhausted identity cannot submit a fifth request. Earlier recipients' accepted SIDs remain intact when a later recipient is rate limited.

## Compatibility and release

Migration 021 adds requests and indexes without rewriting old jobs, schedules or attempts. The worker still accepts explicit organization payloads and older log/recipient/body payloads; the latter resolve ownership from the persisted log and re-enter tenant scope. Existing durable attempt keys remain unchanged. Logs linked to requests/attempts, active schedules and retained schedule dispatch history cannot be cleared as ordinary display history. Unresolved reply conversations cannot be deleted.

No historical audience permission is inferred. Existing SaaS directories and frozen schedules without proof will be excluded until their source evidence is reviewed. Review those operational audiences before releasing migrations 020/021. Keep the captured production hotfix semantics and existing DST/capacity/contact regressions.

The local candidate is a source snapshot, not a deployment authorization. Production release still requires a clean reviewed source commit, hotfix preservation review, verified current-release backup and explicit release approval. Expand-only schema rollback should retain the new evidence/request/attempt tables; returning to older runtime code would restore its unsafe sending behavior. A code rollback is therefore an operational decision, not a reason to delete the new ledgers.

The package does not settle the audit's separate billing-transition and delivery-finality findings. Those remain customer-rollout gates. It also does not automatically reconcile standalone scheduled-message ambiguity or prove provider/recipient delivery in production.
