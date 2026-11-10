@AGENTS.md
@app/AGENTS.md
@tests/AGENTS.md

# Claude Code workspace

Read `PRODUCT.md` and `.impeccable/design.json` before making UI decisions.
Read the nearest nested `AGENTS.md` before editing services, migrations, or deployment tooling.

The user authorizes local project work using the existing credentials, SSH access,
and installed tools. Machine-specific access details are in `CLAUDE.local.md`.
Keep credentials in ignored local files and avoid printing them in logs or commits.

## UI and issue review

- Improve the real Flask/Jinja application using its shared templates, CSS tokens, and existing JavaScript. Preserve the Twinevia identity and connected workflows.
- Inspect the rendered owner, staff, and platform experiences before proposing changes. Cover desktop and a narrow mobile viewport, navigation, setup, composing, events, inbox, forms, tables, empty states, errors, keyboard access, and visible focus.
- Preserve tenant boundaries, staff contact privacy, consent, billing and provider gates, duplicate-send protection, and the separation of event feedback from registration.
- Use `./run/claude_preview.sh` or the configured desktop preview for local UI work. It creates disposable demo data and fake providers. Restarting it resets that preview data.
- Verify changed flows in the browser and run the relevant existing tests. For shared navigation, CSS, or template changes, run the full browser suite. Fix meaningful regressions; keep behavioral assertions intact.
- Record any additional issues with reproduction steps and evidence. Distinguish verified defects from suggestions and untested assumptions.
- Keep changes on the dedicated working branch. Commit completed, verified work. Production deployment and merging into `main` are separate tasks.

## Commands

```bash
./run/verify.sh
./run/test.sh
./run/test_browser.sh
./run/claude_preview.sh
```

See `docs/claude-code.md` for preview accounts, setup, and the initial review brief.
