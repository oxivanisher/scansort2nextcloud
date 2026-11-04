"""Single-user auth: argon2 password hash, signed session cookie, CSRF token, login backoff."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

log = logging.getLogger(__name__)

COOKIE_NAME = "scansort_session"
_hasher = PasswordHasher()


class LoginRequired(Exception):
    pass


class CsrfError(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def load_or_create_secret(configured: str, data_dir: Path) -> str:
    if configured:
        return configured
    path = data_dir / "session_secret"
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret)
    log.warning("webui.session_secret not set; generated one and stored it in data_dir")
    return secret


class SessionManager:
    def __init__(self, secret: str, max_age_hours: int, secure_mode: str = "auto"):
        self._ser = URLSafeTimedSerializer(secret, salt="scansort-session")
        self.max_age = max_age_hours * 3600
        self.secure_mode = secure_mode

    def load(self, request: Request) -> tuple[dict, bool]:
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            try:
                data = self._ser.loads(raw, max_age=self.max_age)
                if isinstance(data, dict) and data.get("csrf"):
                    return data, False
            except BadSignature:
                pass
        return {"csrf": secrets.token_urlsafe(24)}, True

    def dump(self, data: dict) -> str:
        return self._ser.dumps(data)

    def secure(self, request: Request) -> bool:
        if self.secure_mode == "always":
            return True
        if self.secure_mode == "never":
            return False
        return request.url.scheme == "https"


class LoginThrottle:
    """In-memory exponential backoff per client after repeated failures."""

    FREE_ATTEMPTS = 3
    MAX_DELAY = 900

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        with self._lock:
            _, until = self._state.get(key, (0, 0.0))
        return max(0, int(until - time.monotonic() + 0.999))

    def failure(self, key: str) -> None:
        with self._lock:
            n, _ = self._state.get(key, (0, 0.0))
            n += 1
            delay = 0 if n <= self.FREE_ATTEMPTS else min(self.MAX_DELAY, 2 ** (n - self.FREE_ATTEMPTS))
            self._state[key] = (n, time.monotonic() + delay)

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


def check_credentials(username: str, password: str, expected_user: str, password_hash: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), expected_user.encode())
    pw_ok = verify_password(password_hash, password)
    return user_ok and pw_ok
