"""Authentication and password hashing utilities for Cliper SaaS."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional, Dict, Any

from fastapi import Request, HTTPException, Security, status
from fastapi.security import APIKeyCookie, APIKeyHeader

from . import db

COOKIE_NAME = "cliper_session"
cookie_sec = APIKeyCookie(name=COOKIE_NAME, auto_error=False)
header_sec = APIKeyHeader(name="X-Cliper-Session", auto_error=False)


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    iterations = 100_000
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations
    ).hex()
    return pwd_hash, salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    new_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(new_hash, password_hash)


async def get_current_user(
    request: Request,
    cookie_token: Optional[str] = Security(cookie_sec),
    header_token: Optional[str] = Security(header_sec)
) -> Dict[str, Any]:
    token = cookie_token or header_token
    if not token:
        # Check authorization header (Bearer token)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in."
        )

    user = db.get_session_user(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please log in again."
        )
    return user


async def get_optional_user(
    request: Request,
    cookie_token: Optional[str] = Security(cookie_sec),
    header_token: Optional[str] = Security(header_sec)
) -> Optional[Dict[str, Any]]:
    token = cookie_token or header_token
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()

    if not token:
        return None
    return db.get_session_user(token)
