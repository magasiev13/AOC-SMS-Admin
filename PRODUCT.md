# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Organization owners and administrative staff who need to communicate with members, customers, event attendees, or other opted-in communities by SMS. This audience is inferred from the approved managed-pilot plan and the existing workspace workflows.
- Platform administrators at IT Wingman LLC who review pilot requests, provision organizations, manage billing and provider readiness, and support compliance onboarding.
- Workspace staff who send messages, manage audiences, review replies, and operate within permissions established by an organization owner.

## Product Purpose

Twinevia is a multi-tenant messaging workspace that lets organizations send immediate or scheduled SMS messages, manage contacts and event audiences, receive and organize replies, run keyword and survey automations, and maintain consent, suppression, delivery, and usage records. Success means an approved organization can move safely from invitation and payment through compliant provider setup to reliable day-to-day messaging without needing its own messaging engineering or compliance operations team.

## Positioning

Twinevia launches as a managed service rather than unrestricted self-service software. Its inferred distinguishing position is a single operational relationship for the messaging workspace, customer onboarding, A2P and sender-registration assistance, provider readiness, billing, suppression enforcement, and recoverable delivery operations. The public promise is assistance and operational stewardship; external provider approval is never guaranteed.

## Operating Context

- A prospective customer requests a pilot. A platform administrator reviews the application, creates an organization only after approval, and issues a single-use owner invitation.
- The owner accepts current legal policies, completes Stripe Checkout, and works through messaging-provider and A2P setup before chargeable messaging becomes available.
- Owners and staff use a shared web workspace for contacts, community and event audiences, composing, scheduling, inbox replies, automations, suppression, logs, and reports.
- Twilio supplies phone-number, messaging, callback, delivery, and A2P infrastructure. Stripe supplies setup-fee, subscription, portal, invoice, and overage collection.
- Some customers cannot add required messaging-policy content to their existing websites, so Twinevia can host compliant public policy and opt-in pages for the customer organization.

## Capabilities and Constraints

- The supported production runtime is Flask in SaaS mode with PostgreSQL, Redis, and RQ. The original single-tenant SQLite runtime is compatibility-only.
- Twinevia is multi-tenant. Organization data, provider resources, webhooks, replies, suppressions, billing, and outbound actions must remain tenant-bound.
- Launch access is a controlled pilot for a small number of approved customers. Anonymous pilot requests must not create organizations, Stripe customers, Twilio numbers, or provider resources.
- Chargeable actions require an active paid entitlement and verified setup-fee state unless the organization is explicitly complimentary.
- Launch pricing is a one-time $149.99 setup fee plus either $59.99 monthly or $600 annually. Each plan includes 1,000 outbound SMS segments per calendar month; additional segments are billed at $0.03 each through monthly settlement.
- Twinevia absorbs normal first-time Low Volume Standard registration and routine usage costs within the stated commercial model. Rejected resubmissions, appeals, customer-caused retries, higher-volume upgrades, special vetting, and requested additional numbers may be customer-paid.
- Provider registration and message delivery depend on external systems and cannot be promised. Ambiguous provider outcomes must not be blindly retried.
- Public marketing and policy content must not fabricate customers, testimonials, awards, usage metrics, certifications, or provider endorsements.
- Formal accessibility conformance level remains an open decision. Keyboard access, visible focus, readable contrast, responsive layouts, clear labels, and reduced-motion support are required implementation qualities.

## Brand Commitments

- Product name: Twinevia.
- Public identity: "Twinevia, a service of IT Wingman LLC."
- Primary public action: "Request a pilot."
- Existing visual assets include the Twinevia paper-plane mark and a navy, blue, indigo, and white product palette. Preserving the recognizable mark and product identity is confirmed; the exact public-site visual world remains a design decision.
- The inferred voice is direct, calm, capable, and transparent about compliance, provider approval, pricing, and operating responsibility.

## Evidence on Hand

- A functional Flask application with tenant workspaces, platform administration, messaging, contacts, event audiences, inbox, automations, surveys, suppressions, billing, provider setup, A2P onboarding, logs, and operational tooling.
- Existing UI tokens, templates, a Twinevia brand mark, browser coverage, backend integration tests, release tooling, and managed-pilot runbooks in this repository.
- Exact Stripe launch prices and operational billing rules are documented in `docs/pricing.md`.
- No approved customer logos, testimonials, case studies, public delivery metrics, awards, or legal-review endorsements are currently on hand. Future public work must not imply them.

## Product Principles

- Managed before scaled: prove the complete customer path with protected pilots before broad access.
- Consent and tenant safety are product behavior, not policy-page decoration.
- Make the operator's next action and current state obvious.
- Keep commercial and provider expectations explicit, including costs, limits, and uncertain approvals.
- Prefer auditable, recoverable operations over hidden automation or blind retries.
