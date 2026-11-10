# Claude Code desktop setup

Use a clean, dedicated branch based on the current GitHub `main`. Preserve any
other checkout's uncommitted work before setting up the workspace. Keep credentials
in ignored local files.
The GitHub connector alone does not set the local project folder or install its runtime.

## Local runtime

Requirements: Python 3.11, Node/npm, and `redis-server` on `PATH`.

```bash
python3.11 -m venv venv
./venv/bin/python -m pip install -r requirements.txt pytest pytest-cov
npm ci
npm run playwright:install
```

The UI preview does not require an `.env` file, production database, Stripe CLI,
or live provider credentials. Run:

```bash
./run/claude_preview.sh
```

Open `http://127.0.0.1:5011/login`. The launcher uses the existing browser fixtures
in SaaS mode, a dedicated disposable SQLite database, private Redis/RQ processes,
and fake Stripe/Twilio behavior. SQLite here is for local acceptance only;
PostgreSQL remains the supported production database. Every start resets the
preview database. The browser suite uses its own database and port 5010.

| Experience | Login page | Username | Demo password |
| --- | --- | --- | --- |
| Workspace owner | `/login` | `owner-browser` | `Owner-pass1!` |
| Workspace staff | `/login` | `staff-browser` | `Staff-pass1!` |
| Platform administrator | `/platform/login` | `platform-admin` | `Platform-pass1!` |

These are public test fixture accounts, suitable only for the local preview.
More setup and billing states are defined in `tests/browser/seed_saas_browser_db.py`.
Stop the launcher with Ctrl-C; it also stops its worker and private Redis process.

## Credentials and tools

The prepared local workspace also has the user's existing project `.env`, outside
version control, and local instructions pointing to the original checkout and
existing SSH/tool configuration. Claude may use those credentials for authorized
project work. The demo launcher deliberately creates its own fixture environment
so UI testing stays deterministic even when real credentials are available.

Machine-specific Claude settings and `CLAUDE.local.md` are ignored by Git.
Connected-service logins are configured in Claude itself; a Codex connector login
does not create a Claude connector login.

## Desktop configuration

1. Open the **Code** tab and select **This computer**.
2. Select the prepared isolated project folder. If that folder is already a Git
   worktree, leave Claude's extra **Worktree** option off.
3. `CLAUDE.md` imports the shared project instructions and adds the UI review workflow.
4. The **Twinevia demo** preview is defined in `.claude/launch.json`, with automatic
   visual verification enabled. Port 5011 is fixed; stop another preview before starting it.
5. Keep the existing permission settings. Review the local diff and test results
   before merging or deploying any changes.

## Initial review brief

Use this as the first message once the committed baseline has passed validation:

> Read CLAUDE.md, PRODUCT.md, and the design context. Improve the visual quality
> and usability of Twinevia as a polished, coherent messaging workspace. Start
> by running the configured local demo and auditing the owner, staff, and
> platform experiences at desktop and mobile sizes. Review setup, navigation,
> dashboard/composing, community, events/registrations/messages/feedback, inbox,
> scheduled messages, logs, and common forms/tables. Identify the most valuable
> improvements and any reproducible functional defects, then present a concrete
> design direction and prioritized findings before implementation. Preserve
> working integrations, tenant isolation, staff privacy, consent, billing gates,
> and message-delivery safeguards. Use the existing Flask/Jinja architecture.
> After implementation, exercise the changed flows, run the relevant backend
> tests and full browser suite for shared UI changes, and commit verified work
> on the working branch. Report what changed, evidence, and remaining issues.
> Do not merge or deploy as part of this review.

## Validation commands

```bash
./run/verify.sh
./run/test.sh
./run/test_browser.sh
```

GitHub's Python 3.11 workflow additionally runs isolated Linux/systemd and
PostgreSQL concurrency/queue recovery checks. Local macOS/SQLite browser results
do not replace those production-runtime checks.
