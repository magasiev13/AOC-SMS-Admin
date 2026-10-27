# Recipient permission and directory membership

**AOC correction, October 27, 2026:** AOC's owner confirmed that its newsletter, event and in-person sign-ups all authorize community texts. AOC subscribers are eligible under that confirmed sign-up contract without per-person re-approval. Missing Twinevia evidence does not mean missing consent. The earlier REL-09 conclusion is retracted for AOC. Explicit opt-outs still take precedence. See [the corrected AOC contract](aoc-consent-clarification.md). The purpose-evidence rules below apply where an organization has not established that broader sign-up contract.

Twinevia stores contacts/bookings separately from permission to send SMS. SaaS broadcasts require a verified grant for their purpose. Missing evidence is `unknown` and is excluded; it is not silently converted to consent. The legacy `SAAS_MODE=0` import/local compatibility runtime retains its existing behavior.

## Evidence contract

`RecipientPermissionEvidence` retains organization, normalized phone, purpose, scope, granted/denied/unknown decision, source, idempotent source evidence key, verification, proof metadata, occurrence time and recording time. Evidence is appended through `recipient_permission_service.record_permission_evidence` in the caller's transaction. The service does not commit or perform network operations. Replaying a source identity reuses its original evidence; conflicting phone/decision/verification/timestamp is rejected.

A positive grant requires `verified=True` and a nonempty text `metadata['proof']`. Record the actual proof/source and actor, rather than assuming that a directory row or paid booking establishes general SMS permission. Source identity is an idempotency key, not a substitute for proof.

The permission service and tenant ORM helpers scope every query to its organization. The service also binds queries explicitly when an intentional platform operation disables automatic ORM criteria. Attempting to use another organization from an active tenant context is rejected.

## Purposes and precedence

- `community` authorizes the organization's general community SMS audience.
- `event` is scoped to one event ID. It does not authorize other events or general community broadcasts.
- `all` is a revocation/transport scope for STOP/START; it cannot be used as a global affirmative grant.
- Auth alerts, manual conversational replies, keyword/survey responses and protected internal test sends use their existing explicit send kinds. They do not acquire community-broadcast permission by implication and remain subject to unsubscribe, suppression and transport revocation.

The latest revision for each source determines that source's current state. A denial requires a strictly later verified purpose grant; an unknown update cannot resurrect a grant predating that denial. Equal-timestamp grant/denial ordering is treated conservatively. Current transport STOP blocks all sends regardless of a newer purpose grant. START clears that transport revocation but does not revive purpose grants older than STOP. A retained later verified purpose grant is required before broadcasts resume.

Workers/schedulers set `permission_context('event', event_id)` only for an event-targeted send; an ordinary or automatic community reminder stays in community scope. The default provider gate is community scope for `send_kind='blast'`. `manual_live_test` is the explicit kind for protected saved test recipients. This context does not bypass tenant, billing, provider, unsubscribe or suppression rules.

## Event-sync signals

Signed booking synchronization keeps legitimate directory and event-registration records even when permission is false or unknown:

- `sms_consent: true` records verified permission for the particular event only.
- `community_sms_consent: true` separately records verified general-community permission.
- Explicit false is retained as denied; false booking SMS consent without a separate community signal also excludes that source from general broadcasts.
- Missing flags are retained as unknown, not inferred true.
- Metadata retains the actual received flags separately from the applied community decision so the fallback rule is explainable.
- Phone changes withdraw the old phone's source-bound event/community eligibility; both directory records remain available for review.
- Booking cancellation withdraws event-specific eligibility. It preserves an independent community grant unless an explicit general withdrawal signal is received. A later consent update is recorded even if that registration has already been removed.

Unknown or denied imported contacts cannot bypass the gate through an old frozen recipient snapshot. Permission is checked again at the final provider boundary. Composition and scheduled execution should use the same batch filter to explain exclusions before dispatch.

## Owner review

Community contacts and event registrations display granted, denied, or unknown permission alongside any unsubscribe/suppression. Their sendable counts and event CSV eligibility use the same purpose checks as sending. Staff can see these states; only an organization owner can record a review. Platform administrators retain their separate platform boundary.

The **Review permission** link opens a dedicated owner action for that phone and purpose. A grant requires a named evidence source, retained proof notes, the UTC time permission was actually received, and an explicit verification attestation. Reviewing old evidence today does not change its occurrence time or override a newer refusal. Event reviews are limited to that event and cannot create a general-community grant. Ordinary contact/registration saves and CSV imports remain grant-free.

Reviews append `owner_review` evidence with the actor and source notes. The form has CSRF protection plus a signed, session-bound identity tying the submission to its tenant, contact phone, purpose and event. Identical submissions reuse one evidence record; conflicting replays and a phone changed since the form opened are rejected. The owner sees the resulting effective permission and recent evidence, including when STOP or another later refusal still blocks a grant. A review never clears transport opt-outs or suppressions.

## Migration and release boundary

SaaS migration 020 adds the evidence table and indexes. It does not modify, delete, or fabricate permission for existing contacts, registrations, or schedules. Existing records without evidence become ineligible under the new SaaS runtime until reviewed; the rows and their other business uses remain intact. This is an intentional sending behavior change and must be included in the reviewed rollout/backfill plan. No live customer permission was changed during local implementation.

Use source records and verified customer evidence when planning a backfill; where evidence is unavailable, retain unknown. Review frozen schedules, operational audience counts and owner guidance before a production release. Do not bulk grant consent, delete contacts or clear STOP merely to restore a prior recipient count.

## Verification

`tests/test_recipient_permissions.py` covers no-evidence/unknown/denied states, verified proof, purpose isolation, source ordering, independent grants, STOP/START cutoff, identity conflicts, tenant boundaries, execution-time withdrawal and the real provider gate with a mocked provider client. `tests/test_aoc_event_sync.py` includes signed true/false/unknown signal, update/replay, phone-change and canceled-booking cases while retaining the existing hotfix and DST tests.

The permission suite also exercises owner review validation, actual evidence-time ordering, replay conflicts, cross-tenant and staff/platform denial, CSRF, phone-bound form identities, unchanged directory-save behavior, STOP precedence, and rendered/export eligibility. `tests/browser/recipient-permission-review.spec.js` exercises the desktop validation/keyboard path and mobile event-versus-community scope using synthetic local data.

The same tests can run against disposable PostgreSQL databases by setting the explicitly guarded `AUDIT_PERMISSION_DATABASE_ROOT=postgresql://audit@127.0.0.1:55432/postgres`; they refuse any other PostgreSQL root and create/drop a separate UTF8 test database per case. The normal run uses fresh SQLite databases. No test uses a real provider, production database or shared queue.
