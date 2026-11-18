# Production hardening checklist

This is an owner-run checklist. Preparing this document does not apply any server
changes. Record the date, operator, result and evidence reference for each check;
keep credentials and private operational evidence outside the repository.

- [ ] **Off-server backup and restore.** Configure an off-server backup destination
  using the supported backup workflow. Keep a copy of the decryption key in a
  separate secure location. Restore an off-server archive into a disposable,
  isolated PostgreSQL environment using only the off-server materials. Verify
  application schema, tenant records, opt-outs and suppression records. Record
  the archive identity and restore result without copying contacts or keys into
  the evidence. A local backup or a successful upload alone does not prove restore.
- [ ] **Alert destination.** Set the alert destination through the private server
  configuration and verify that the intended operator receives a test alert.
  During an approved maintenance window, exercise a controlled readiness failure
  and confirm an alert arrives, then restore readiness. Check that a failed billing
  reconciliation reports non-readiness. An unset destination must produce an
  explicit journal warning; it cannot count as a delivered alert.
- [ ] **Host firewall.** Inventory the services that must remain reachable. Limit
  public inbound access to SSH and web traffic. Preserve a working administrative
  access path while applying rules, check both IPv4 and IPv6, and verify the rules
  from a separate connection. Validate the host rules and upstream network rules
  independently.
- [ ] **Old environment-file copies.** Identify stale environment-file copies in
  the application root without displaying their contents. Preserve the active
  private configuration and any required encrypted recovery material; remove
  only confirmed stale copies. Verify ownership and restrictive permissions on
  the retained configuration. Keep values out of terminal transcripts and git.
- [ ] **Failed diagnostic units.** Inspect the failed-unit inventory privately.
  Distinguish obsolete one-off diagnostic units from unresolved service failures.
  Clear only the confirmed obsolete failures. Avoid copying command lines, which
  can contain recipient data. Recheck the failed-unit inventory afterward.
- [ ] **Dedicated server.** Prepare a separate Twinevia server with the supported
  PostgreSQL, Redis/RQ and systemd runtime. Plan migration, encrypted backups,
  provider callbacks, access controls and rollback before cutover. Validate a
  restored copy and the complete readiness checks before approving the move.
- [ ] **Web-server static files.** After reviewing and deploying the static-serving
  template from P1.4, verify that scripts, styles and fonts are served directly by
  the web server. Confirm release-versioned asset URLs, text compression and
  long-lived caching. Confirm that the web server can read public assets while
  private configuration and application files retain their restricted permissions.
  Test the send review and phone navigation with third-party asset hosts blocked.

Use the [SaaS operations guide](saas-operations.md),
[deployment guide](deployment.md) and [public readiness signoff](public-readiness-signoff.md)
for the supported procedures. Schedule and authorize production work separately.
