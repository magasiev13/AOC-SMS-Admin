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
5. The user authorizes full local credential and tool access. Keep credentials
   out of GitHub.

## Initial review brief

Use this as the first message once the committed baseline has passed validation:

> Give me your candid, independent assessment of this app. What are the five
> major things I should focus on fixing or optimizing, and why? What do you think
> of the UI, and how would you improve it drastically? Inspect and use the app,
> investigate its implementation, and choose your own approach. Support your
> priorities and proposed design direction with concrete evidence. You have full
> access to the local project credentials, SSH configuration, and installed tools.
> Do not expose credentials in GitHub.

## Validation commands

```bash
./run/verify.sh
./run/test.sh
./run/test_browser.sh
```

GitHub's Python 3.11 workflow additionally runs isolated Linux/systemd and
PostgreSQL concurrency/queue recovery checks. Local macOS/SQLite browser results
do not replace those production-runtime checks.
