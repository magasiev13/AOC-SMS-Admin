# app/services/ — Business logic

- Reuse services from routes for provider operations, billing, and messaging rules. Keep them usable from both RQ worker and oneshot timer contexts.
- Twilio operations belong in `twilio_service.py`; A2P lifecycle work belongs in `twilio_a2p_service.py`. Reuse existing provider audit helpers when changing provider state.
- Customer-managed credentials must pass through `provider_secret_service.py`; never store raw secrets in a second location.
- Treat billing readiness and provider readiness as separate checks. Preserve recipient unsubscribe/suppression checks when changing send eligibility.
- Keep tenant-agnostic platform operations explicitly separate from tenant-scoped workspace behavior.
- For restart orchestration, inspect `platform_operations_service.py` and its existing request queue. For legacy data imports, inspect `legacy_import_service.py`.
