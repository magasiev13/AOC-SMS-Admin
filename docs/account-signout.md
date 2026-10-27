# Account sign-out

The workspace navigation uses **Sign out everywhere**. Explicit sign-out rotates
the account's existing `session_nonce`, clears the browser session and expires the
configured remembered-login cookie. This ends the account's sessions on other
devices as well as the current browser.

The account-wide behavior is deliberate. Background requests can finish after a
sign-out response and restore an older signed session cookie. Clearing browser
cookies alone cannot revoke that cookie. The existing nonce check rejects those
earlier sessions and remembered tokens, including cookies from a delayed response.
The sign-out result is not reported as successful before nonce revocation commits.

Password changes and administrator password resets retain their existing nonce
invalidation. Password/contact/setup/tenant gates remain in place. The shared
session-clearing helper also preserves Flask-Login's remembered-cookie expiry
instruction when a tenant becomes unavailable or a password change requires a new
login. Signing out does not delete account, organization or messaging records.

This implementation does not add a per-device session registry. If independent
device management becomes a demonstrated need, it requires a separately reviewed
session model rather than silently weakening sign-out revocation.

Verification covers configured cookie names, a fresh client after sign-out,
replaying cookies captured by an in-flight request, password changes, missing or
suspended tenants, and actual Chromium remembered login followed by a fresh
browser context. All external providers are mocked in these checks.
