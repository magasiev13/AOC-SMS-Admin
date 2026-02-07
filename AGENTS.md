# AGENTS.md

This repository welcomes AI agents. Use this guide to work safely, predictably, and with high quality.

## 1) Mission and quality bar
- Primary goal: deliver correct, maintainable changes that match the user request.
- Prefer small, verifiable diffs over broad rewrites.
- Never fabricate results. If a test or behavior was not verified, say so.
- Honor intent and make assumptions explicit when requirements are incomplete.
- For design work, pursue clarity and inevitability: remove non-essential elements, preserve hierarchy, and avoid decorative complexity.

## 2) Safety and responsibility
- Sandbox first. Avoid actions that affect external systems unless explicitly requested.
- Least privilege. Do not access, reveal, or exfiltrate secrets.
- Flag security, privacy, or data-loss risks as soon as they are found.
- Keep a human in the loop for risky or irreversible actions.

## 3) Scope discipline
- Preserve existing functionality unless the user explicitly requests behavior changes.
- Do not add speculative features, dead code, or new dependencies without approval.
- Do not change unrelated files.
- If a design recommendation requires a feature or backend change, call it out explicitly as out of scope for design.

## 4) Working modes

### Build mode (default)
- Implement requested code and content changes end to end.
- Validate with targeted checks or tests where practical.

### Design mode (when user asks for UI/UX design or audit)
- Focus only on visual design, layout, spacing, typography, color, motion, responsiveness, and accessibility.
- Do not change app logic, API behavior, data models, state management semantics, or backend architecture.
- Keep all visual decisions anchored to design system tokens. Do not introduce hardcoded one-off values without proposing token updates.

## 5) Required design context (read before proposing UI changes)
For design requests, gather and internalize these first:
1. `DESIGN_SYSTEM.md` (or equivalent) for tokens and component language.
2. `FRONTEND_GUIDELINES.md` for implementation constraints and structure.
3. `APP_FLOW.md` for routes, screens, and journeys.
4. `PRD.md` for feature intent and functional boundaries.
5. `TECH_STACK.md` for capabilities and limitations.
6. `progress.txt` for current build status.
7. `LESSONS.md` for prior design pitfalls and corrections.
8. The live app across mobile, tablet, and desktop, in that order.
   - Use screenshots only as fallback if a live walkthrough is blocked.
   - Responsiveness must be fluid across sizes, not merely acceptable at fixed breakpoints.

If any key document is missing, say what is missing and proceed with explicit assumptions.

## 6) Design audit protocol

### Step 1: Full audit across every screen
Evaluate each screen for:
- Visual hierarchy and clarity in 2 seconds.
- Spacing rhythm and whitespace quality.
- Typography hierarchy and consistency.
- Color restraint, purpose, and contrast.
- Alignment and grid precision.
- Component consistency and interaction states (default, hover, focus, disabled).
- Icon consistency (style, weight, size, source set).
- Motion quality and purpose.
- Empty, loading, and error states.
- Theme quality (if dark mode/theming exists).
- Information density and redundancy.
- Responsive behavior across all viewport sizes.
  - Confirm touch target sizing and ergonomics for thumb use on mobile.
- Accessibility: keyboard flow, focus visibility, ARIA, and readable contrast.

### Step 2: Jobs-style filter
For each element, ask:
- Would a user need to be told this exists?
- Can this be removed without losing meaning?
- Does this feel inevitable rather than arbitrary?
- Is hidden craftsmanship as refined as visible UI?

If the answer is weak, redesign or remove.

### Step 3: Produce a phased plan before implementation
Output recommendations in this structure:
- `DESIGN AUDIT RESULTS`
- `Overall Assessment` (1 to 2 sentences)
- `PHASE 1 - Critical` (hierarchy, usability, responsiveness, consistency blockers)
- `PHASE 2 - Refinement` (spacing, typography, color, alignment, iconography)
- `PHASE 3 - Polish` (motion, empty/loading/error states, subtle finishing details)
- `DESIGN_SYSTEM updates required` (new or changed tokens/components)
- `Implementation notes for build agent` (exact file, component, property, old value -> new value)
- Within each phase, format items as:
  - `[Screen/Component]: [What is wrong] -> [What it should be] -> [Why this matters]`
  - `Review: [Why this phase order is correct]`

### Step 4: Approval gate
- Do not implement design changes until the user approves the phase.
- Implement only approved items, then present results before moving on.
- If output quality still feels off, propose a focused refinement pass before continuing.

## 7) Design principles (non-negotiable)
- Simplicity is architecture, not decoration.
- Consistency is mandatory; avoid creating third variants of existing patterns.
- Hierarchy must reflect functional importance.
- Each screen should have one clear primary action that does not compete with secondary actions.
- Whitespace is structural, not empty.
- Alignment errors of even 1 to 2 pixels matter.
- Mobile is the baseline; tablet and desktop are enhancements.
- Every change needs a design reason, not a personal preference.

## 8) Design handoff and post-implementation
- If design tokens/components are missing, propose additions to `DESIGN_SYSTEM.md` and get approval before use.
- If intended user behavior for a screen is unclear in `APP_FLOW.md`, ask before designing for an assumed flow.
- If a design improvement requires functional change, state:
  - `This design improvement requires a functional change and is out of scope for this design pass.`
- After implementing approved design work:
  - Update `progress.txt` with what changed.
  - Update `LESSONS.md` with reusable patterns and mistakes to avoid.
  - Confirm instruction files are aligned (`AGENTS.md` for Codex, `CLAUDE.md`, `GEMINI.md`, `.cursorrules` as applicable).
  - Note any approved phases still pending.
  - Provide before/after evidence when possible.

## 9) Repo quick map
- App entry: `wsgi.py` (Flask app factory in `app/__init__.py`).
- Core code: `app/` (routes, models, services, utils, migrations).
- Tests: `tests/` (pytest runs unittest-based tests too).
- Docs: `docs/` (architecture, DB, API, services, config, deployment).

## 10) Build, lint, and test commands
- Install deps: `python3 -m venv venv` then `pip install -r requirements.txt`.
- Run dev server: `flask --app wsgi:app run --debug`.
- Run all tests: `pytest`.
- Coverage: `pytest --cov=app`.
- Single pytest test:
  - `pytest tests/test_utils.py::TestNormalizePhone::test_us_number_without_country_code`
- Single unittest test:
  - `python -m unittest tests.test_utils.TestNormalizePhone.test_us_number_without_country_code`
- Lint/format: no enforced repo linter currently; do not add one without explicit request.

## 11) Environment and runtime notes
- `.env` is loaded via python-dotenv; start from `.env.example`.
- Required env vars: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `SECRET_KEY`.
- SQLite DB is in `instance/` and created automatically.
- Redis is required for RQ background jobs (default queue: `sms`).
- Scheduler:
  - Dev: APScheduler when `SCHEDULER_ENABLED=1`.
  - Prod: systemd timer based scheduling.
  - Never enable `sms-scheduler.service` directly.
  - Enable only `sms-scheduler.timer`.

## 12) Implementation workflow
1. Clarify scope and user-visible impact.
2. Plan small, cohesive edits.
3. Implement incrementally.
4. Validate with targeted tests/checks.
5. Summarize changes, validation, and limitations.

## 13) Code style and conventions

### Imports
- Group imports in this order: standard library, third-party, local `app.*`.
- Prefer explicit imports; avoid wildcard imports.
- Keep imports at module top unless a local import is required to avoid cycles.

### Formatting and naming
- Use 4-space indentation and no tabs.
- Follow existing local file style; avoid unnecessary reformatting.
- `snake_case` for functions and variables, `CamelCase` for classes, `UPPER_SNAKE_CASE` for constants.

### Types and datetime handling
- Use type hints on public functions when practical.
- Prefer existing helper types and patterns in the codebase.
- Store UTC in DB and convert at boundaries.
- Use `utc_now()` or `timezone.utc` consistently.
- For scheduled timestamps, convert to UTC and strip tzinfo before DB storage where required.

### Error handling and logging
- Catch expected exceptions narrowly.
- Roll back `db.session` on SQLAlchemy exceptions before continuing.
- In routes, use safe user feedback (`flash`) and safe redirects/renders.
- Do not log credentials or full sensitive message bodies.

### Domain-specific rules
- Normalize/validate numbers with `app.utils.normalize_phone` and `app.utils.validate_phone`.
- Use `parse_recipients_csv` and `parse_phones_csv` for CSV imports.
- Respect unsubscribe and suppression checks via `app.services.recipient_service`.
- Register routes through blueprints (`app.routes.bp`, `app.auth.bp`).
- Guard protected views with `@login_required` and `@require_roles`.
- Use `current_app` for request-time config/logging access.
- Keep models in `app/models.py` with `db.Model` and explicit `__tablename__`.
- Add schema changes under `app/migrations/` and apply through `dbdoctor`.
- Keep template logic minimal; compute display data in routes/services.
- Queue background work via `app.queue.get_queue()` and jobs in `app/tasks.py`.
- Do not spawn long-lived threads from routes.

## 14) Tests
- Add focused tests in `tests/` for new behavior.
- Keep tests deterministic and offline.
- Prefer local fixtures unless reuse justifies shared fixtures.
- If tests are skipped, explain why.

## 15) Quick ops guide (Codex)
- Unit tests: `pytest`.
- DB doctor inspect: `python -m app.dbdoctor --print`.
- DB doctor apply: `python -m app.dbdoctor --apply` (run before serving traffic).
- Expected systemd units: `sms`, `sms-worker`, `sms-scheduler.timer`.
- Services load env from `/opt/sms-admin/.env`.
- Scheduler runs via `sms-scheduler.timer` every 30 seconds and triggers oneshot `sms-scheduler.service`.
- Do not enable `sms-scheduler.service` directly.

## 16) Communication standards
- Use concise, structured summaries.
- List commands run.
- Separate observations from assumptions.
- Call out open questions and follow-ups.

## 17) Deliverables checklist
- [ ] Changes are minimal and scoped.
- [ ] Functionality is preserved unless explicitly requested otherwise.
- [ ] Tests/checks were run, or skip reason is documented.
- [ ] Summary is concise and accurate.
- [ ] No hidden side effects.

## 18) Definition of done (production-oriented)
- [ ] No 500 errors in logs after deploy.
- [ ] Migrations applied before serving traffic.
- [ ] Logs are clear of errors for `sms` and `sms-scheduler`.
