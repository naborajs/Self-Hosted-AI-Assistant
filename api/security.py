from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import OAuth2PasswordBearer, SecurityScopes

from config import settings

logger = logging.getLogger("local_ai_assistant.security")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/token")

SECRET_KEY = settings.session_secret.get_secret_value()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def verify_password(plain_password: str, hashed_password: str) -> bool:
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

