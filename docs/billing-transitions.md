# Billing transition ownership

An organization has one current Checkout request on its existing subscription
record. Migration 022 adds nullable request identity, frozen request parameters,
state, known session identity and reconciliation incident fields. Issued Checkout
sessions retain their original plan, prices, offer version and transition key.
Existing Stripe customer/subscription IDs, payment proofs, invoices and checkout
history are not rewritten by the migration.

## Starting and changing Checkout

The service locks the organization and then its subscription. It commits a random
request key and the exact Stripe parameters before calling Stripe. A retry of the
same pending request uses those saved parameters and key, including the original
return URLs and prices. Losing a create response or a database acknowledgement
does not create a replacement request or expire a potentially completed payment.

An existing open compatible session is reused. Switching monthly/annual plans or
offers first reconciles every outstanding issued session, expires the superseded
ones, and confirms remote expiration. A lost expiration response is followed by a
read: only a matching expired Stripe object closes that path. A complete or unknown
payment blocks the competing change; paid evidence is reconciled first. A newly
selected plan therefore cannot leave an older independent payment URL behind.

Stripe can prune idempotency keys after at least 24 hours. Twinevia stops creating
or replaying an unconfirmed request at **23 hours**. It retains the original key
and payload for review. A known remote session or verified webhook can still
resolve it. Absence of a local response is never treated as proof that no remote
session exists, and an expired provider idempotency key is never blindly reused.
See [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests) and
[Checkout expiration](https://docs.stripe.com/api/checkout/sessions/expire).

## Payment, complimentary access and conflicts

Complimentary conversion uses the same lock and closes all outstanding payable
sessions first. An existing customer/subscription identity, verified payment,
complete unpaid session, or unconfirmed create/expire result prevents conversion
until reconciled. Clearing complimentary access does not create a Stripe resource.

Setup payment proof is retained once. A later legitimate subscription checkout
omits setup after its payment is verified. Duplicate checkout/webhook processing
does not replace the original setup PaymentIntent or reset its payment timestamp.
A second distinct setup payment, a different existing subscription, contradictory
customer/workspace ownership, or a paid retired offer creates a visible incident.
The current subscription is preserved; the application does not cancel, replace,
refund or otherwise "fix" remote billing automatically. Ordinary later webhooks
cannot erase a conflict requiring operator review.

The incident is shown on owner billing/setup and platform access pages. A saved
unknown checkout offers recovery within its safe retry window. Unresolved payment
or ownership conflicts suppress a replacement checkout action. Operators should
inspect the retained session, customer, subscription, invoice and PaymentIntent
identities in Stripe and the application before choosing a resolution. Clearing
an incident or changing a remote resource requires an intentional reviewed
operational action; this change adds no automatic conflict-resolution command.

## Webhooks and reconciliation

Webhook events are retained by unique Stripe event ID. Concurrent deliveries
share that ledger entry. Authoritative reads and billing transitions use the same
organization-first lock order. Resource ownership takes precedence over metadata,
and older events cannot regress newer applied state. Local reconciliation IDs are
bounded hashes when their descriptive source exceeds the existing database field.
Billing timestamps are explicitly stored as naive UTC.

The existing configured events remain supported:

- `checkout.session.completed`
- `customer.subscription.created`, `.updated`, `.deleted`
- `invoice.payment_succeeded`, `invoice.payment_failed`

Completed unpaid Checkout is acknowledged without granting paid access. The billing
reconciliation timer and billing refresh poll known pending sessions. A paid
invoice/PaymentIntent linked to the issued terms can prove payment even when the
Checkout payment flag lags. `invoice.payment_succeeded` also resumes a pending
initial checkout. These paths work with the existing event subscription set.

Handlers additionally support `checkout.session.async_payment_succeeded`,
`checkout.session.async_payment_failed`, `checkout.session.expired`, and
`invoice.paid`. If asynchronous methods are enabled, subscribe the Stripe endpoint
to their lifecycle events as well; periodic reconciliation remains the recovery
path for missed notifications. No payment-method availability, endpoint settings,
API version, dependency version or tax configuration is changed by this package.
Future Stripe Tax configuration still needs its own registration review; this
change does not enable `automatic_tax`.

## Usage settlement safety

Provider delivery completion is separate from verified economic facts. Before
posting a usage period, billing calls `usage_period_billing_block_reason`. Missing
cost/units, currency mismatch, missing underlying evidence or retained historical
review flags keep the period in review. Existing invoice/item identities and
already settled amounts are preserved. Reopening historical provider records for
read-only reconciliation cannot automatically create a new historical charge.

Once a settlement version starts, aggregation freezes its amount, state and remote
identities. Migration 026 adds a nullable `settlement_started_at`; new attempts set
it before the first provider operation. Retries keep that version, its frozen
amount and original invoice/item keys. An accepted item whose response was lost
can be recovered by reading the original invoice's items. Recovery verifies the
customer, organization, period, version, amount and currency; a current invoice
read must also prove its total before collection. A finalized invoice is resumed
from its current provider state without collecting a replacement invoice.

Creating an unconfirmed invoice or item is blocked at 23 hours from the retained
attempt start. Legacy attempts with an unknown start cannot replay unconfirmed
creation. Verified existing invoice/item objects can still resume after that
window, because their identity is known. Unresolved cases retain their IDs and
version in `review_required`, with a `usage_settlement_review` provider audit event.
Migration 026 deliberately leaves old start values unknown; it does not infer a
financial attempt time from a period's age or its mutable last-update timestamp.

## Local verification

`tests/test_billing_transitions.py` uses mocked Stripe API boundaries and fresh
databases. Setting `BILLING_TEST_POSTGRES_ROOT` to exactly
`postgresql://audit@127.0.0.1:55432/postgres` enables unique UTF-8 PostgreSQL
databases and real separate-session lock races. Other database roots are rejected.
Normal tests make no live Stripe calls or charges.

The cases cover monthly/annual replacement, stable retry after lost acknowledgements,
committed request snapshots, expiration/payment races, complimentary conversion,
pending and paid invoice recovery through existing events, duplicate/reordered
webhooks, tenant ownership, setup once, visible incidents, additive migration
preservation, StripeClient request options, and settlement evidence gates.
