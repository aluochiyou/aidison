"""Application-layer authentication for the single-owner deployment."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import bcrypt
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from aidison.api.errors import ApiErrorCode, error_response
from aidison.config import ApplicationAuthSettings

SESSION_COOKIE_NAME = "__Host-aidison_session"
_PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("GET", "/api/auth/session"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    username: str
    expires_at: int


class LoginRateLimiter:
    """Small in-process limiter appropriate for the single API process."""

    def __init__(self, *, attempts: int = 5, window_seconds: int = 60) -> None:
        self._attempts = attempts
        self._window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        failures = self._failures[key]
        cutoff = now - self._window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if len(failures) < self._attempts:
            return None
        return max(1, int(self._window_seconds - (now - failures[0])))

    def record_failure(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


class ApplicationAuthenticator:
    def __init__(self, settings: ApplicationAuthSettings) -> None:
        self.settings = settings
        password_hash = settings.admin_password_hash.get_secret_value()
        self._password_hash = password_hash.encode("utf-8")
        self._credential_version = hashlib.sha256(self._password_hash).hexdigest()[:20]
        self.rate_limiter = LoginRateLimiter()

    def verify_credentials(self, username: str, password: str) -> bool:
        password_bytes = password.encode("utf-8")
        if len(password_bytes) > 72:
            return False
        password_ok = bcrypt.checkpw(password_bytes, self._password_hash)
        return password_ok and hmac.compare_digest(username, self.settings.admin_user)

    def establish_session(self, request: Request) -> AuthPrincipal:
        now = int(time.time())
        principal = AuthPrincipal(
            username=self.settings.admin_user,
            expires_at=now + self.settings.session_ttl_seconds,
        )
        request.session.clear()
        request.session.update(
            {
                "sub": principal.username,
                "exp": principal.expires_at,
                "credential_version": self._credential_version,
            }
        )
        return principal

    def principal(self, request: Request) -> AuthPrincipal | None:
        session = request.session
        username = session.get("sub")
        expires_at = session.get("exp")
        credential_version = session.get("credential_version")
        if (
            not isinstance(username, str)
            or not isinstance(expires_at, int)
            or expires_at <= int(time.time())
            or not isinstance(credential_version, str)
            or not hmac.compare_digest(credential_version, self._credential_version)
            or not hmac.compare_digest(username, self.settings.admin_user)
        ):
            return None
        return AuthPrincipal(username=username, expires_at=expires_at)

    def origin_is_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is not None and hmac.compare_digest(
            origin.rstrip("/"), self.settings.public_origin or ""
        )


class ApplicationAuthMiddleware(BaseHTTPMiddleware):
    """Fail-closed guard for every API route except the explicit public set."""

    def __init__(self, app: ASGIApp, authenticator: ApplicationAuthenticator) -> None:
        super().__init__(app)
        self.authenticator = authenticator

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        settings = self.authenticator.settings
        if not settings.enabled:
            return await call_next(request)

        route = (request.method, request.url.path)
        is_public = route in _PUBLIC_ROUTES
        if request.method in _UNSAFE_METHODS and not self.authenticator.origin_is_allowed(request):
            denied_response = error_response(
                403,
                ApiErrorCode.POLICY_DENIED,
                "request origin is not allowed",
            )
            denied_response.headers["Cache-Control"] = "no-store"
            return denied_response

        principal = self.authenticator.principal(request)
        request.state.principal = principal
        if not is_public and principal is None:
            unauthenticated_response = error_response(
                401,
                ApiErrorCode.UNAUTHENTICATED,
                "authentication required",
            )
            unauthenticated_response.headers["Cache-Control"] = "no-store"
            return unauthenticated_response

        response = await call_next(request)
        if request.url.path.startswith("/api/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response
