from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet() -> Fernet:
    raw_key = (current_app.config.get("TWILIO_CREDENTIAL_ENCRYPTION_KEY") or "").strip()
    if not raw_key:
        raise RuntimeError("TWILIO_CREDENTIAL_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(raw_key.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - invalid env only
        raise RuntimeError(
            "TWILIO_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key."
        ) from exc


def encrypt_provider_secret(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("Provider secret cannot be empty.")
    return _fernet().encrypt(normalized.encode("utf-8")).decode("utf-8")


def decrypt_provider_secret(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return _fernet().decrypt(normalized.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Stored provider secret could not be decrypted.") from exc
