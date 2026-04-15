# tests/ — Test Suite

This repo uses pytest for both backend and browser validation.

## TEST SURFACES

- Python tests under `tests/test_*.py`
- browser tests under `tests/browser/`
- wrappers in `run/test.sh` and `run/test_browser.sh`

## WHAT THE SUITE COVERS

- auth and password hardening
- billing and Stripe webhook behavior
- provider and A2P flows
- workspace routes and tenant isolation
- scheduled sends, logs, inbox, keywords, and surveys
- schema tooling and migrations
- demo seed and runtime bootstrap behavior
- browser smoke flows for the SaaS UI

## CONVENTIONS

- prefer isolated app contexts and fresh DB setup per test case or class
- use mocked external providers; do not hit live Twilio or Stripe in normal tests
- keep browser coverage deterministic and artifact-friendly
- when testing SaaS behavior, assert tenant isolation and role gating explicitly

## USEFUL COMMANDS

```bash
./run/test.sh
./run/test.sh tests/test_billing_webhooks.py
./run/test.sh --cov=app
./run/test_browser.sh
```

## ANTI-PATTERNS

- **DO NOT** remove failing tests to “fix” a regression.
- **DO NOT** add tests that depend on shared external state.
- **DO NOT** assume the legacy runtime is the default test subject; most current behavior is SaaS-first.
