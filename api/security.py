"""Authentication, authorization, and cryptographic token verification for Project EDITH v2.0.

This module provides the core security mechanisms for human administrators and machine clients:
- Administrator Authentication: Validates credentials against `ADMIN_PASSWORD_HASH` via bcrypt.
  Strictly forbids using `SESSION_SECRET` as a login password.
- JWT Access Tokens: Issues and verifies short-lived HS256 signed JSON Web Tokens for CLI
  and dashboard administrative sessions.
- Machine Client Authentication (`get_machine_client`):
  - Authenticates external software via `X-API-Key` using timing-safe comparisons (`secrets.compare_digest`).
  - Automatically isolates client data using full 256-bit SHA-256 digests (`client_<sha256>`).
  - Gracefully maps legacy 8-character token identities to prevent orphaned data.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, Request, Security, status
from fastapi.security import OAuth2PasswordBearer, SecurityScopes

from config import settings

logger = logging.getLogger("local_ai_assistant.security")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/token")

SECRET_KEY = settings.session_secret.get_secret_value()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt-hashed password in constant time."""
    if not plain_password or not hashed_password:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception as exc:
        logger.debug("Password verification error: %s", exc)
        return False


def get_password_hash(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def authenticate_admin(username: str, password: str) -> dict | None:
    allowed = [item.strip() for item in settings.admin_list if item.strip()]
    if not allowed or username not in allowed:
        return None

    # Strictly reject if user tries to authenticate using the token signing secret
    if password == SECRET_KEY:
        logger.warning("Authentication rejected: attempt to use SESSION_SECRET as password.")
        return None

    # Administrator password must be securely configured via ADMIN_PASSWORD_HASH
    if not settings.admin_password_hash:
        logger.warning("Admin authentication rejected: ADMIN_PASSWORD_HASH is not configured.")
        return None

    admin_hash = settings.admin_password_hash.get_secret_value()
    if not admin_hash or not verify_password(password, admin_hash):
        return None

    return {"username": username, "scopes": ["admin"]}


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": subject, "exp": int(expire.timestamp())}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(security_scopes: SecurityScopes, token: str = Depends(oauth2_scheme)) -> dict:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception
    user = {"username": username, "scopes": ["admin"]}
    return user


async def get_machine_client(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> dict[str, Any]:
    """Authenticate machine clients via X-API-Key header or Bearer JWT token."""
    # 1. Check API Key
    if x_api_key:
        configured_keys = settings.valid_api_keys
        for configured_entry in configured_keys:
            expected_secret = configured_entry
            if ":" in configured_entry:
                client_id, expected_secret = configured_entry.split(":", 1)
                full_hash = hashlib.sha256(expected_secret.strip().encode()).hexdigest()
                client_id = client_id.strip() or f"client_{full_hash}"
                legacy_client_id = None
            else:
                full_hash = hashlib.sha256(expected_secret.strip().encode()).hexdigest()
                client_id = f"client_{full_hash}"
                legacy_client_id = f"client_{full_hash[:8]}"
            if secrets.compare_digest(x_api_key.strip(), expected_secret.strip()):
                return {
                    "client_id": client_id,
                    "auth_type": "api_key",
                    "legacy_client_id": legacy_client_id,
                }

    # 2. Check Bearer Token
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            raw_sub = payload.get("sub")
            if not raw_sub or not isinstance(raw_sub, str) or not raw_sub.strip():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token subject",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            client_id = raw_sub.strip()
            return {"client_id": client_id, "auth_type": "bearer", "scopes": payload.get("scopes", [])}
        except jwt.PyJWTError:
            pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide a valid X-API-Key header or Bearer token.",
        headers={"WWW-Authenticate": "Bearer"},
    )

