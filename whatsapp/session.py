from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _create_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class EncryptedSessionStore:
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
