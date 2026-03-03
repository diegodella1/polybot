"""Simple session-based auth for admin panel."""

import hashlib
import os
import secrets
from datetime import datetime, timezone

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

COOKIE_NAME = "polybot_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days

# In-memory sessions (survives across requests, cleared on restart)
_sessions: dict[str, datetime] = {}


def _get_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "polybot")


def verify_password(password: str) -> bool:
    return password == _get_password()


def create_session(response: Response) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.now(timezone.utc)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return token


def check_session(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return token in _sessions


def delete_session(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token and token in _sessions:
        del _sessions[token]
    response.delete_cookie(COOKIE_NAME)
