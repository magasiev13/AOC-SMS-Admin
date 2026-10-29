# Event bookings and messaging contacts

Twinevia keeps registration transactions in `event_bookings`. A transaction can
represent a single person or a group and can have no usable SMS destination.
`event_registrations` remains the current deduplicated messaging-contact
projection. A family booking for ten people therefore means one booking, ten
reserved places and one reachable contact when its supplied destination is
eligible. Reserved places are not evidence of attendance.

The ledger is provider neutral. A website, booking product, company CRM,
community application or manual operator can use the same projection service.
The WordPress adapter preserves its existing provider-prefixed booking IDs for
compatibility. Other applications supply their own stable transaction IDs.

## Transaction identity and revision contract

The identity is organization, source, source scope and source booking ID.
Different forms or application datasets use distinct source scopes. A phone
number is contact information and never identifies a transaction. Reusing an
identity for another event is rejected; changed form mappings cannot move its
retained history. Distinct representations of one transaction need an explicit
same-event duplicate link. No automatic phone-based booking deduplication runs.

`upsert_booking(event, payload, source, received_at, source_scope='',
source_revision=None, deleted=False)` accepts these normalized adapter fields:

| Field | Meaning |
| --- | --- |
| `booking_id` | Stable source transaction identity; maximum 120 characters |
| `source_scope` argument | Stable form/dataset scope; maximum 160 characters |
| `source_revision` | Optional monotonic nonnegative integer revision |
| `updated_at` | Source modification time; signed integration timestamps include a timezone |
| `status`, `active` | Source status and its adapter's inclusion decision |
| `spaces` | Nonnegative reserved-place count; missing count defaults to one |
| `name`, `phone` | Booking contact; missing/invalid phone preserves the transaction and seats |
| `sms_consent` | Explicit event-purpose permission signal, when provided |
| `community_sms_consent` | Separate community-purpose permission signal, when provided |

Adapters map their own lifecycle states to `active=false` when a booking should
leave the audience. Cancelled, rejected, deleted and inactive canonical statuses
also remove its contribution. Pending and submitted records remain distinguishable
from confirmed bookings. Unknown source status labels remain visible and preserve
the existing inclusion behavior unless the adapter supplies `active=false`.

A source upsert describes its current contact representation: omitting the phone
removes an unsupported SMS destination while retaining the booking. A minimal
`booking_deleted` payload is instead a cancellation tombstone. It retains omitted
name, phone/contact state, party size, person ID, selections, comment and consent
evidence. Explicitly supplied blank fields still clear those details. Both paths
retain the cancellation history and remove only the cancelled contribution from
the current contact projection.

Newer numeric revisions take precedence within their source identity. Otherwise,
source modification timestamps order updates. Duplicate or older versions do not
replace accepted state. A retained cancellation cannot be reactivated by an
unversioned replay. Cancellation history remains in the ledger, and later valid
reactivation requires newer source evidence.
An inventory absence also retains its own source-time watermark. A later-arriving
per-record numeric revision cannot reactivate that booking unless its modification
time is strictly after the absence watermark; another delayed cancellation cannot
erase this retained boundary.

Explicit duplicate links retain both source records but count only the primary
transaction. Chains and cycles are rejected. Unlinking restores the independent
contribution. A legacy representation explicitly linked to a recovered primary
can resolve its identity hold; unlinking restores its original unverified state.

## Projection and permission

Projection rebuilds serialize on the event row in PostgreSQL. Several active
bookings can contribute to one current SMS destination. Cancelling or correcting
one transaction removes only that contribution; another active booking preserves
the shared contact. A blank or invalid replacement number removes the unsupported
destination while retaining the booking and reserved places.
Inventory batches seed old evidence once and rebuild the projection once after
their complete records and absence decisions have been applied. Ordinary single
transaction updates continue rebuilding immediately in the same transaction.

Source-scoped projection rows use an opaque ledger identity because the older
contact table's external-ID uniqueness does not include source scope. Ledger
source identities remain available independently. Staff views use local record
IDs and existing contact privacy rules; source IDs are not safe display redactions.

Community subscription history and event eligibility remain separate. Booking
cancellation withdraws event eligibility without assuming withdrawal from general
community communications. Global STOP and transport suppression still gate sends.
Organization-specific signup contracts apply only where explicitly configured.
Reachable-contact counts apply event-purpose permission as well as transport
restrictions. Supplied contacts lacking permission remain separate from missing
phone details and are shown as restricted contacts.

Manual writes and contact imports use `create_local_booking`. Registration surveys
use their flow and opaque conversation IDs so restarting the same RSVP updates its
existing contribution; a different form retains a separate contribution. Event
feedback never enters this registration path. Legacy snapshot imports go through
`project_legacy_registration` and remain unverified historical evidence.

## Migration and authority

SaaS migration 029 adds the ledger and copies existing contact rows as legacy
evidence while preserving the old registration IDs. It does not infer missing
transactions, invent historical completeness or rewrite old messages. Legacy
SQLite migration 028 is a compatibility entry point for the same additive schema.

An exact source identity recovered by a newer signed update can replace its
legacy evidence. Unresolved legacy rows stay visible and keep coverage unverified.
Complete inventories, explicit source comparison and source-of-record approval
are handled by the inventory service. Ledger updates alone never authorize a
historically complete roster or release a scheduled invitation hold.
