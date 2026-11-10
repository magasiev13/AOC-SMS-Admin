@AGENTS.md
@app/AGENTS.md
@tests/AGENTS.md

# Claude Code workspace

Read `PRODUCT.md` and `.impeccable/design.json` before making UI decisions.
Read the nearest nested `AGENTS.md` before editing services, migrations, or deployment tooling.

The user grants full freedom to inspect, research, experiment, and act on the
requested work using existing credentials, SSH access, and installed tools.
Use your judgment and take ownership of the outcome. Machine-specific access
details are in `CLAUDE.local.md`. The only additional access restriction is:
never publish, push, or commit credentials to GitHub.

## UI and issue review

- The current application uses Flask/Jinja, shared CSS tokens, and JavaScript. Its architecture and product context are available for you to inspect and evaluate.
- The desktop preview runs `./run/claude_preview.sh`, an optional local environment with disposable demo data and fake providers. Restarting it resets that preview data.
- Verify actual behavior and support conclusions with evidence. Distinguish verified defects, design judgment, and untested assumptions.
- Choose the approach, scope, and workflow that best achieve the user's request.

## Commands

```bash
./run/verify.sh
./run/test.sh
./run/test_browser.sh
./run/claude_preview.sh
```

See `docs/claude-code.md` for preview accounts, setup, and the initial review brief.
