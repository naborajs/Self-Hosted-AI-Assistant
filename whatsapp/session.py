"""Encrypted session persistence store for WhatsApp credentials in Project EDITH v2.0.

Provides authenticated encryption at rest using Fernet (AES-128-CBC with HMAC-SHA256):
- Key Derivation: Derives a 32-byte urlsafe-base64 key from `SESSION_SECRET` via SHA-256.
- Tamper Resistance: Authenticated decryption rejects corrupt or modified session files.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _create_key(secret: str) -> bytes:
    """Derive a URL-safe base64-encoded 32-byte key from the provided secret string."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class EncryptedSessionStore:
    """Manages encrypted file storage of session credentials and authentication tokens."""
    def __init__(self, storage_path: Path, secret: str) -> None:
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(_create_key(secret))

    def save(self, session_id: str, data: dict[str, str]) -> None:
        encrypted = self.fernet.encrypt(json.dumps(data).encode("utf-8"))
        with open(self.storage_path / f"{session_id}.session", "wb") as handle:
            handle.write(encrypted)

    def load(self, session_id: str) -> dict[str, str] | None:
        path = self.storage_path / f"{session_id}.session"
        if not path.exists():
            return None
        with open(path, "rb") as handle:
            try:
                payload = self.fernet.decrypt(handle.read())
            except InvalidToken:
                return None
        return json.loads(payload.decode("utf-8"))

    def list_sessions(self) -> list[str]:
        return [p.stem for p in self.storage_path.glob("*.session")]
