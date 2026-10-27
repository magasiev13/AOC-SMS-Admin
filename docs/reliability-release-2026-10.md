# October 2026 reliability release

This release consolidates the preserved production hotfixes and the locally
verified reliability and usability work. The original working checkout and audit
evidence are retained separately. Production release identity, executed validation
results, backup/restore proof and post-release observations belong in the release
record; this document describes the lasting product and operational contracts.

## Changed behavior

| Area | User-visible outcome | Audit references |
|---|---|---|
| Submission recovery | A saved send/reply keeps its audience, content and request identity across queue loss, duplicate submissions and lost acknowledgements. Unknown provider acceptance requires review instead of another send. | REL-01/08 |
| Cancellation | Cancel cooperates with dispatch and reports partial submission truthfully. Known attempts and usage survive schedule/log cleanup. | REL-03/06 |
| Inbound consent | Authenticated STOP/START is retained even when outbound provider or billing readiness is unavailable. | REL-04 |
| Billing transitions | One organization-owned Checkout transition; incompatible payable sessions must be authoritatively closed before a replacement or complimentary conversion. Unknown payment ownership is visible and blocks conflicting transitions. | ARCH-01/02 |
| Delivery and cost | API acceptance, carrier receipt, segment completeness and provider price are separate facts. Late receipts update operator surfaces without sending again. Unknown costs and historical recovery cannot silently post an invoice. | REL-02/07 |
| Automatic replies | New inbound state and pending replies commit together. Bounded background recovery uses the same keys; survey ordering, expiry and STOP gates survive interruptions. Historical replies are never backfilled. | REL-05 |
| Daily work | Reply drafts and their send identities survive refresh/Back and incoming updates. Contact and survey errors preserve entered text. New composer submissions have an explicit signed audience/timing review. | UX-01/02/03/04 |
| Readiness and access | Billing, A2P approval and sender readiness are distinguished. Pilot pages point at the current invitation; repeated same-email reissues preserve historical records. Forbidden pages provide a workspace recovery path. | ARCH-03/04, UX-06 |
| Scale and accessibility | Community lists use 50-row server pages and one preview; conversations initially show the latest 100 messages with cursor history. Pilot query growth is bounded. Preview controls have accessible names and keyboard dismissal. | ARCH-05/06/07, UX-05 |
| Release recovery | Running-release hotfix preservation is checked. Encrypted backups capture the actual running code, and a downloaded off-host copy must match the export before confirmation. An isolated authenticated restore/forward-migration drill precedes production migration. | PROD-01/02/03 |

## AOC consent correction

REL-09's inference of absent or event-only AOC consent was unsupported and is
retracted. The organization confirmed that newsletter, event and in-person
sign-ups include community text consent. Existing AOC sign-ups do not require
individual reapproval. Explicit opt-outs, suppressions, address corrections and
tenant boundaries remain effective. No fabricated person-level consent records
or historical sends are created. See [the clarified contract](aoc-consent-clarification.md).

## Operations and migration

Use PostgreSQL, Python 3.11, Redis/RQ and the supported `twinevia-saas` deployment
family. Migrations 020–026 add the permission/request/automation ledgers and
billing/delivery evidence. Migration 025 replaces the historical invitation status
constraint with pending-only uniqueness on PostgreSQL, preserving rows, tokens
and references. Migration 026 retains the start of each usage settlement so interrupted invoice
creation keeps its original identity and safe retry window. Existing SQLite
databases keep their legacy historical uniqueness
restriction; SQLite is a compatibility/test path.

Historical economically incomplete usage is marked for billing review. Recovering
provider facts does not authorize a historical charge, new invoice, refund or
change to a settled period. Receipt polling uses bounded batches, backoff and
network timeouts. New automation replies expire after 30 minutes by default;
unsent replies are cancelled after a real opt-out. Unknown external acceptance is
retained for evidence review rather than automatically retried.

Operational detail:

- [Billing transitions](billing-transitions.md)
- [Delivery reconciliation](delivery-reconciliation.md)
- [Automatic reply recovery](automation-reply-recovery.md)
- [Saved request recovery](outbound-request-recovery.md)
- [Explicit outbound review](outbound-review.md)
- [Managed pilot runbooks](managed-pilot-runbooks.md)

## Recommended next decisions

Continue the managed pilot direction with targeted simplification. The observed
failures justify strengthening a few shared state transitions and daily workflows;
they do not establish a need for a rewrite or broad self-service expansion.

1. Validate later A2P revocation/resource-drift detection with a lost-callback
   experiment. Approved records currently depend on Event Streams rather than
   regular polling. Do not broaden a provisioning job to approved customers
   without proving that its recovery path cannot alter provider resources.
2. Make retained billing/usage/submission review states operable from one clear
   support workflow. The safety holds now preserve evidence; operators still
   need a reviewed decision for ambiguous acceptance, payment ownership or
   historical cost variance. Start with the incidents actually seen in pilots.
3. Validate event-operator needs before extending surveys into typed bookings.
   Contact registration and party-size/ticket booking are different outcomes.
   Preserve the existing zero-capacity additional-contact behavior and external
   event integration while observing a real operator with synthetic attendees.
4. Run focused physical-mobile, assistive-technology and privileged-account
   security sessions. Automated Chrome viewport and tenant/auth tests are useful
   evidence, but do not establish Safari/device coverage or security/WCAG certification.
5. Measure the next 3–5 managed pilots: recurring use, operator minutes, support,
   verified delivery, opt-outs, provider costs and contribution margin. These
   outcomes should decide customer focus, pricing and self-service scope.

Actual carrier delivery and live payment behavior require controlled authorized
operational evidence. Mocked provider tests must never be described as real
delivery or a completed financial transaction.
