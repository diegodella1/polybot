"""Simple session-based auth for admin panel."""

import hmac
import os
import secrets
import time
from datetime import datetime, timezone

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

COOKIE_NAME = "polybot_session"
SESSION_MAX_AGE = 86400 * 7  # 7 days

# In-memory sessions (survives across requests, cleared on restart)
_sessions: dict[str, datetime] = {}

# Rate limiting: ip → [timestamps of failed attempts]
_login_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300  # 5 min


def _get_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "polybot")


def verify_password(password: str) -> bool:
    return hmac.compare_digest(password, _get_password())


def check_rate_limit(ip: str) -> bool:
    """Return True if IP is rate-limited (too many failed attempts)."""
    now = time.monotonic()
    attempts = _login_attempts.get(ip, [])
    # Prune old attempts outside window
    attempts = [t for t in attempts if now - t < WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def record_failed_attempt(ip: str):
    """Record a failed login attempt for rate limiting."""
    now = time.monotonic()
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    _login_attempts[ip].append(now)


def clear_attempts(ip: str):
    """Clear failed attempts after successful login."""
    _login_attempts.pop(ip, None)


def create_session(response: Response) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.now(timezone.utc)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=False,
        samesite="lax",
    )
    return token


def check_session(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token or token not in _sessions:
        return False
    created = _sessions[token]
    if (datetime.now(timezone.utc) - created).total_seconds() > SESSION_MAX_AGE:
        del _sessions[token]
        return False
    return True


def delete_session(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token and token in _sessions:
        del _sessions[token]
    response.delete_cookie(COOKIE_NAME)
