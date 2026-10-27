# Reviewing unresolved provider acceptance

An ambiguous or sending attempt is retained evidence of a possible provider submission. A timeout is not proof that nothing was sent. Do not clear the attempt, change its logical key, recreate the request, or send a replacement to test what happened.

The `app.outbound_review` CLI defaults to inspecting one explicitly selected organization and request. Inspection reads only Twinevia's saved evidence. It does not contact Twilio, enqueue work, write an audit entry, or commit a database change. Output masks recipient phones and omits message bodies and provider secrets.

An authorized server operator runs this CLI; the platform-admin actor ID attributes the review and does not create a browser login session. Run commands with the intended SaaS environment loaded and Python 3.11. Confirm the database and queue target before any resolution or subsequent recovery. The following IDs are illustrative; select the actual retained request and attempt after inspection.

```bash
python -m app.outbound_review --organization-id 12 --request-id 345
```

An explicit resolution requires an existing platform administrator's user ID, an evidence reference, the exact attempt ID, and an existing Twilio message SID:

```bash
python -m app.outbound_review --organization-id 12 --request-id 345 \
  --resolve --attempt-id 678 \
  --message-sid SM0123456789abcdef0123456789abcdef \
  --actor-user-id 7 --evidence-reference 'support-case-2026-123/provider-response'
```

The operator must establish that the SID belongs to this particular submission, using retained response/log evidence and provider timestamps. An identical body sent previously to the same recipient is not enough to select a SID. Retain the evidence outside the CLI and supply its durable reference. The CLI then fetches that message through the existing usage-reconciliation client and checks its SID, organization provider account, outbound API direction, normalized recipient and exact personalized body SHA-256 against the saved attempt. The provider creation time must fall within 120 seconds of the saved submission time, and within any retained resolution-time bound. Missing timestamps or an older/later identical message fail closed for manual investigation. Two identical submissions within that window still require the operator's original submission evidence; the clock window does not prove a unique association by itself. The CLI never calls a provider create/update/delete method.

Accepted, queued, sending, sent, delivered and read provider records establish known acceptance. Failed, undelivered and canceled records establish a known terminal failure. The SID remains attached in either case. The `sent` attempt state means provider acceptance, not confirmed recipient delivery. Provider statuses outside the reviewed set remain unresolved.

Resolution locks the message log, request and attempt in that order, refreshes their state, and rechecks ownership, actor, account and immutable identity. It refuses another tenant's request, a different request's attempt, a SID already linked to an attempt, a conflicting saved SID, and a request/attempt already finalized while the lookup was in flight. It preserves an already accepted request. Current provider-account changes fail closed; historical accounts require separate retained evidence and manual review.

The transaction appends an `outbound_attempt_review` provider audit entry and an `operator_reviews` reference in the saved request result. It resolves that attempt to `sent` or `failed`, and returns the same immutable request to `pending` for deterministic finalization. It does not enqueue anything itself. Existing recovery can then process the original request and reuse its original logical keys: a known attempt is replayed from the database, and another unresolved attempt stays unresolved. Recovery may continue other original, unsubmitted recipients that remain eligible; it does not authorize a new audience or replacement message. `app.outbound_recover` scans due nonterminal requests across organizations, so run it only under the intended operational scope or let the configured recovery path do that work.

If the request aggregate is still ambiguous but **every** original recipient already has a known `sent` or `failed` attempt, no provider lookup is needed. This can occur when a duplicate worker observes `sending` and finishes first, while the original worker later saves the accepted SID but exits before aggregate finalization. Inspect the original request, retain the complete-attempt evidence, then explicitly authorize known-outcome recovery:

```bash
python -m app.outbound_review --organization-id 12 --request-id 345 \
  --finalize-known --actor-user-id 7 \
  --evidence-reference 'support-case-2026-123/complete-saved-attempts'
```

`--finalize-known` is mutually exclusive with `--resolve` and rejects attempt/SID arguments because it reviews the whole frozen request. It requires the platform-admin actor and evidence reference, locks log → request → attempts, and verifies the saved intent digest, exact original logical keys, complete frozen audience, normalized phones, personalized body digests, send kinds and organization ownership. Missing, extra, queued, sending or ambiguous attempts reject the operation. Accepted attempts must retain their provider SIDs; known failed attempts may have no SID. It preserves the attempt outcomes, records `outbound_known_finalize` audit evidence and returns only the same request to pending for deterministic finalization. It does not construct/read/write a provider or enqueue a job. A request already finalized as accepted or failed is preserved.

If no verified SID is available, leave the attempt unresolved and continue provider/support investigation. There is deliberately no “assume not sent” or automatic resend operation. New submission/resolution timestamps are explicitly stored as UTC without an offset, matching the schema contract even when PostgreSQL's session timezone differs. Older attempts persisted under a non-UTC session may have shifted naive timestamps; those fail the strict provider time match and require manual investigation. This tool does not guess their timezone or backfill existing data. It does not reconcile standalone scheduled sends, which retain their separate cancellation and per-recipient attempt history.

Local verification uses mocked provider reads, fresh isolated databases, tenant and actor rejection cases, invalid evidence, acceptance/failure outcomes, terminal-state races, retained audit history, and recovery of the same known attempt with zero new provider submissions. No production review or provider resource mutation was performed during implementation.
