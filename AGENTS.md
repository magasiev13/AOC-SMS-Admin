# AGENTS.md

This repository welcomes AI agents. Use this guide to work safely, predictably, and with high quality.

## 1) Mission and constraints
- **Primary goal:** deliver correct, maintainable changes that match the request.
- **Be precise:** prefer small, verifiable changes over sweeping rewrites.
- **Honor intent:** if requirements are unclear, surface assumptions in the final summary.
- **Never fabricate results:** if you did not run a test or confirm behavior, say so.

## 2) Safety and responsibility (Codex principles)
- **Sandbox first:** avoid actions that affect external systems unless explicitly requested.
- **Least privilege:** do not access secrets or credentials; do not exfiltrate data.
- **No silent risks:** flag security, privacy, or data-loss concerns when you spot them.
- **Human-in-the-loop:** prefer changes that can be reviewed; avoid irreversible actions.

## 3) Understanding the codebase
- Read relevant files before modifying them.
- Prefer existing patterns and utilities.
- Match established style, naming, and architecture.
- Avoid introducing new dependencies unless necessary.

## 4) Implementation workflow
1. **Clarify scope**: identify affected files and user-visible behavior.
2. **Plan**: outline steps mentally; keep changes minimal and cohesive.
3. **Implement**: make incremental edits; keep diffs small.
4. **Validate**: run targeted tests or checks where practical.
5. **Document**: summarize changes, tests, and any limitations.

## 5) Quality bar
- Write code that is easy to review.
- Keep behavior deterministic and reproducible.
- Add or update tests when behavior changes.
- If you can’t run tests, explain why.

## 6) Communication standards
- Use clear, structured summaries.
- List commands you ran.
- Call out open questions or follow-ups.
- Avoid speculation—separate observations from assumptions.

## 7) What not to do
- Don’t introduce unused code, dead branches, or speculative features.
- Don’t change unrelated files.
- Don’t overfit to a single happy-path scenario.

## 8) Deliverables checklist
- [ ] Changes are minimal and scoped.
- [ ] Tests or checks run (or skipped with reason).
- [ ] Summary is concise and accurate.
- [ ] No hidden side effects.

## 9) Quick ops guide (Codex)
- **Unit tests:** run `pytest`. If none exist, add focused tests in `tests/` and document how to run them.
- **DB doctor & migrations:** run `python app/dbdoctor.py` and `python app/migrate.py` (apply migrations before serving).
- **Systemd services:** expected units are `sms`, `sms-worker`, and `sms-scheduler.timer`; all load environment from `/opt/sms-admin/.env`.
- **Scheduler:** uses systemd timer (`sms-scheduler.timer`) that triggers `sms-scheduler.service` (Type=oneshot) every 60 seconds. Do NOT enable `sms-scheduler.service` directly—enable only the timer.
- **Definition of done (prod):**
  - [ ] No 500s in logs after deploy.
  - [ ] Migrations run **before** serving traffic.
  - [ ] Logs are clear of errors for `sms` and `sms-scheduler`.
