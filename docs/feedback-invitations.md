# Explicit event feedback invitations

Feedback is optional for every event in every workspace. Creating or editing an
event or feedback form creates no invitation or schedule. Existing event reminders
remain separate and retain their configured audiences and times.

Open **Events → event → Feedback → feedback form → Review invitation**. The
composer requires an explicit audience and either **Send now** or a date, time and
timezone. Review shows the audience rule, current estimate, message and timing.
Only confirmation persists a `feedback_invitations` approval and its existing
`scheduled_messages` transport record. The encrypted browser review expires after
15 minutes and is not scheduler authority. Repeating confirmation with the same
approval key returns the original invitation. Each initial approval and later
review is retained with its actor, form version, context digest and time; reviewing
a hold does not erase the original approval lineage.

Choose **registered contacts** only after complete source reconciliation establishes
verified event roster coverage. Historical contact rows alone do not establish a
complete roster. A coverage hold never falls back to community. Choosing
**contactable community** requires its own explicit review and retains existing
organization consent, STOP and suppression rules. The suggested invitation says
“If you joined …”, so it does not claim that all community members attended.

When an approved invitation becomes due, the scheduler checks its event timing,
message and feedback form/version against the approval. It resolves current
eligible contacts at that point, including legitimate later registrations, and
commits the exact recipient snapshot before claiming execution. Every subsequent
attempt uses that snapshot. New contacts cannot enter during retries or recovery.
A verified event with no current contacts can have an explicitly scheduled future
invitation; it holds if there are still no eligible recipients when due.

Every provider boundary rechecks material approval context, frozen snapshot
integrity, recipient membership, and the current registration projection for a
registrant invitation. The existing provider service rechecks messaging readiness,
permissions and opt-outs. A cancelled booking removes only its contribution to the
registration projection; another eligible booking sharing its destination can
keep that destination eligible. Existing durable per-recipient dispatch identities
prevent duplicate submission after worker interruption. Ambiguous provider
acceptance is retained for review and is never automatically resubmitted.

A material change or loss of roster coverage places the invitation on **Held**.
Feedback detail explains the hold and links to **Review held invitation**. This
review can acknowledge changed event/form context while preserving the original
frozen audience, outcome evidence and dispatch identities. Once execution has
begun, changed message content must be restored to its original approved body
before resuming; a replacement invitation needs a separate explicit review.

Feedback detail lists invitation identity, chosen audience/timezone, status, frozen
recipient denominator and accepted/failed/unresolved outcomes, with a link to its
message log. Provider acceptance and confirmed delivery remain separate evidence.
No response-rate percentage is invented from an unverified or unfrozen roster.
Feedback answers continue to live only in feedback sessions and responses: they
never write attendee names, bookings, party sizes or registrations.

All records are tenant scoped. Owners and authorized staff use the same review
flow. Staff views retain opaque record identities and never expose saved recipient
snapshots, raw contact numbers, or readable phone-derived tokens.
