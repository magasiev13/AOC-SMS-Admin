# Staff contact privacy

SaaS organization membership is authoritative. Owners have contact-number and
export authority; staff do not inherit it from an old global admin role. Platform
accounts retain their separate platform boundary. Legacy compatibility allows
its existing admin role only. Unknown or missing authority fails closed.

Staff can find contacts by name, send to existing community/event audiences,
reply through thread IDs, collect event feedback, and record an event contact's
actual opt-out. Full numbers and number editing, imports and dataset exports
require an owner/admin. Direct export URLs are denied before a query or stream
is built. Phone-based directory lookup is unavailable to staff; name/text lookup
is retained. Saved internal test contacts are selected by a tenant-scoped record
ID; the server resolves the address. A forged ID or raw-number selector fails.

Contact numbers are omitted from desktop/mobile HTML, attributes, previews,
survey DOM IDs and staff response DTOs. Human display text also redacts phone-like
sequences, including message/error text. This never rewrites stored content or
provider send payloads. An existing editor containing protected phone text is
owner-only on both GET and POST, preventing a redacted save from destroying it.
The native HTML send review applies the same display policy as the JavaScript
review. A saved feedback invitation containing a protected number requires an
owner to prepare it; staff see that explanation instead of a misleading masked
draft or a link to an unavailable action. Summary cards, question tooltips,
keyword labels and suppression metadata follow the same display policy.

The fifteen-minute send-review snapshot is encrypted and authenticated with a
purpose-derived application key, bound to the actor/session, form identity,
audience and intent. Signing alone was insufficient because its payload was
browser-readable. Legacy clear reviews require refresh for staff. Owners retain
the short upgrade compatibility window. Machine tokens, IDs and counts are
never passed through display redaction. The existing total HTTP request cap is
retained; composer confirmation supports a bounded sealed snapshot before CSRF
parses multipart data, while other multipart form-memory limits remain intact.

Authenticated dynamic responses are private/no-store and vary by Cookie. Staff
flash text is redacted before Flask serializes its readable signed session.
Already copied information cannot be retracted by changing an access policy;
these controls govern current requests and subsequent responses. No customer
phone values, attendance, consent, opt-outs or provider resources are changed by
the rollout. AOC's established signup consent remains honored.
