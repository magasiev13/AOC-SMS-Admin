# Registration integrations

Twinevia accepts registration updates from an organization's connected applications.
WordPress is one adapter; a company, community or client can connect its commerce,
CRM, membership or registration system through the same booking ledger and inventory
contract. A booking is one transaction, `spaces` is its reserved-place count and a
valid eligible phone is a messaging destination. None of these proves attendance.

## Connect an application

An organization owner enables **Application** in event sync settings and generates
its encrypted integration secret. The endpoint is
`/webhooks/event-sync/<organization-slug>/app`. The credential is scoped to that
organization. Reuse the delivery ID and identical body on retries.

Send UTF-8 JSON with these headers:

- `Content-Type: application/json`
- `X-Twinevia-Timestamp`: current Unix timestamp in seconds
- `X-Twinevia-Signature`: `sha256=` followed by the hexadecimal HMAC-SHA256 of
  `timestamp + "." + exact_body_bytes`, keyed by the integration secret
- `X-Twinevia-Delivery-ID`: a stable unique identifier for this delivery

The application protocol requires `protocol_version: 2`. Choose a source slug
such as `commerce` and stable `booking.source_scope` such as `store:42`. Twinevia
namespaces the source under the authenticated integration as
`app:<integration-id>:commerce`. External clients cannot write manual, WordPress
or another integration's source records by supplying those names.
Source slugs contain at most 40 characters; the full namespaced source must fit
the existing 50-character event identity. A long connection ID may require a
shorter slug. The endpoint rejects an oversized identity before writing records.

```json
{
  "protocol_version": 2,
  "action": "booking_upsert",
  "source": "commerce",
  "event": {
    "event_id": "spring-open-house",
    "title": "Spring Open House",
    "start_at": "2027-04-10T10:00:00-06:00",
    "timezone": "America/Denver",
    "modified_at": "2026-10-29T18:00:00Z"
  },
  "booking": {
    "booking_id": "order-100",
    "source_scope": "store:42",
    "source_revision": 1,
    "updated_at": "2026-10-29T18:00:00Z",
    "status": "confirmed",
    "active": true,
    "spaces": 10,
    "name": "Primary contact",
    "phone": "+17205550101",
    "sms_consent": true,
    "community_sms_consent": false
  }
}
```

Actions are `event_upsert`, `event_deleted`, `booking_upsert`, `booking_deleted`,
`inventory_page` and `inventory_complete`. Timestamps include a timezone. Booking
source revisions are nonnegative monotonically increasing integers. A changed,
blank or invalid phone is an explicit update to the same booking; it never erases
its reserved-place evidence. Cancellations remain as revisioned tombstones.
A distinct booking ID always represents a distinct transaction, even when two
transactions share a destination. A booking identity cannot move to another event.
Connecting an application does not create message schedules.

To attach another source to an existing event, the owner first configures that
source and scope on the event. The application can then send
`event.target_event_id` with the existing Twinevia event ID. The exact authenticated
source/scope/event binding must exist. This explicit link combines independent
bookings into one event; it does not deduplicate transactions by phone. Linked
sources cannot replace the event's timing or details. Explicit duplicate-booking
links are a separate operation after reviewing whether two representations are the
same transaction.

## Complete source inventories

An active-record replay is not a complete roster. To repair missed cancellations,
produce a consistent event-specific snapshot of **all** source records, including
cancelled and contactless bookings. Send one manifest for that source scope:

```json
{
  "id": "one-stable-inventory-uuid",
  "source_scope": "store:42",
  "watermark": "2026-10-29T19:00:00Z",
  "pages": 2,
  "records": 75
}
```

Each `inventory_page` has that manifest in `inventory`, the usual source/event,
one-based `page`, up to 500 `bookings`, and `records_json`. `records_json` is the
exact JSON encoding of that page's bookings array; the parsed array must equal
`bookings`. Compute each page SHA256 from the UTF-8 `records_json` bytes. Send
`inventory_complete` with `completion_proof` equal to SHA256 of those lowercase
page hashes joined with a newline, in page order, without a final newline.
An empty roster still has one page containing `[]`, `pages: 1` and `records: 0`.

Pages and completion may arrive in any order. No booking or contact projection
changes until every page, record count and completion proof is validated. Reusing
an inventory/page identity with different contents fails. Later booking revisions
and mutations received after inventory staging began survive an older snapshot.
Only absent identified bookings from that exact source/event/scope can be cancelled;
legacy evidence is preserved. Distinct inventories with a tied watermark cause a
coverage hold and require a strictly newer complete capture. A booking revision at
the exact absence watermark is retained with an ambiguity hold. Inventory absence
tombstones reject delayed pre-snapshot reactivation, including higher per-booking
revision numbers whose source timestamp does not prove they occurred later. Past-event inventories do not create old reminders.

A complete inventory proves source coverage, not operator approval. The owner must
compare it with the source and deliberately enable roster authority. Every active
source must be configured and reviewed; a new source or an interrupted newer
inventory holds scheduled registrant invitations. Manual/import/survey ledgers have
an explicit local review action. Unresolved legacy identities always hold authority.
Community audience selection requires its own deliberate review.

## WordPress adapter

Install the complete `integrations/wordpress/` directory, including the inventory
helper. Forms retain their first event binding and entries retain that binding;
changing the mapping cannot move historical registrations. Use a new form for a
new event. A party-size field can be selected with the
`twinevia_event_sync_wpforms_party_size_fields` filter. Separate forms remain
separate booking sources.

Run an event-specific complete inventory, including a past event, with:

```sh
wp twinevia-event-sync reconcile --event-post-id=EVENT_POST_ID
wp twinevia-event-sync reconcile --event-post-id=EVENT_POST_ID --form-id=FORM_ID
```

The existing bounded future-event replay remains compatible but its heartbeat
cannot prove a complete roster. A complete inventory stages signed pages and proof
through the same durable delivery hook. Failed page delivery or a source-read error
cannot remove records. WordPress source queries use a consistent database snapshot;
source revisions advance when normalized booking content changes, and replays retain
their previous revision. Revision capture uses a per-booking database advisory lock
and a current locking read. Inventory records use the capture-start watermark with
microsecond precision; a newer persisted source revision aborts that capture instead
of assigning a fresh revision to old snapshot content.

The optional AOC bridge is an isolated compatibility adapter for the installed MU
integration. It requires explicit `TWINEVIA_AOC_BRIDGE_ENABLED=true`, preserves the
`aoc-wordpress` namespace, reuses encrypted-on-server settings through the installed
accessors, removes its superseded active messaging hooks, retains Events Manager
status inclusion, completed live payment eligibility, ticket quantities, selections
and comments, and maps the verified free RSVP group-size fields. Do not enable it
until source comparison and release review. Existing queued legacy payloads remain
compatible; numeric source revisions protect bookings from older unversioned replays.

## Portable client and offline preview

`integrations/app_sync_client.py` uses only Python's standard library. Its default
is an offline dry run that prints delivery IDs, body hashes and byte counts without
printing contacts, signatures or the secret:

```sh
python3 integrations/app_sync_client.py \
  --url https://TWINEVIA_HOST/webhooks/event-sync/ORGANIZATION/app \
  --payload captured-booking.json --delivery-id order-100-revision-1
```

To submit, provide `TWINEVIA_SYNC_SECRET` through the calling environment/secret
manager and add `--send`. The secret is never a command-line argument. Supply the
same file bytes and delivery ID after a timeout. Redirects are rejected, and
remote submissions require HTTPS. Loopback HTTP is supported for isolated tests.

For a complete inventory, the input file contains `source`, `event`,
`source_scope`, `watermark` and `bookings`. The caller must capture this input from
one consistent source snapshot; the client cannot infer source completeness.

```sh
python3 integrations/app_sync_client.py \
  --url https://TWINEVIA_HOST/webhooks/event-sync/ORGANIZATION/app \
  --payload captured-inventory.json --inventory-id inventory-20261029-001
```

This creates bounded page bodies and exact completion proof in memory and prints
only their identities and hashes. Add `--send` to submit pages followed by the
completion message. An interruption prevents later messages from being submitted;
rerunning with the same input and inventory identity safely reuses the staged pages.
