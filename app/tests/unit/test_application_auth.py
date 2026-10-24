from __future__ import annotations

import bcrypt
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aidison.api.app import create_app
from aidison.config import ApplicationAuthSettings

ORIGIN = "https://aidison.test"
PASSWORD = "a-long-test-password"


def _settings(*, password: str = PASSWORD, secret: str = "s" * 48) -> ApplicationAuthSettings:
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    return ApplicationAuthSettings(
        enabled=True,
        admin_user="aidison",
        admin_password_hash=SecretStr(password_hash),
        session_secret=SecretStr(secret),
        public_origin=ORIGIN,
        session_ttl_seconds=3_600,
    )


def _client(settings: ApplicationAuthSettings) -> TestClient:
    return TestClient(create_app(auth_settings=settings), base_url=ORIGIN)


def test_login_sets_hardened_cookie_and_unlocks_protected_api() -> None:
    with _client(_settings()) as client:
        assert client.get("/api/integration-health").status_code == 401

        response = client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "aidison", "password": PASSWORD},
        )

        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        normalized_cookie = cookie.lower()
        assert "__host-aidison_session=" in normalized_cookie
        assert "httponly" in normalized_cookie
        assert "secure" in normalized_cookie
        assert "samesite=strict" in normalized_cookie
        assert "path=/" in normalized_cookie
        assert client.get("/api/integration-health").status_code == 200


def test_authentication_rejects_wrong_password_and_cross_site_writes() -> None:
    with _client(_settings()) as client:
        wrong = client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "aidison", "password": "wrong-password"},
        )
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "UNAUTHENTICATED"

        cross_site = client.post(
            "/api/auth/login",
            headers={"Origin": "https://attacker.test"},
            json={"username": "aidison", "password": PASSWORD},
        )
        assert cross_site.status_code == 403
        assert cross_site.json()["error"]["code"] == "POLICY_DENIED"


def test_session_probe_and_logout_are_idempotent() -> None:
    with _client(_settings()) as client:
        assert client.get("/api/auth/session").json() == {"authenticated": False}
        client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "aidison", "password": PASSWORD},
        )
        assert client.get("/api/auth/session").json()["authenticated"] is True

        logout = client.post("/api/auth/logout", headers={"Origin": ORIGIN})
        assert logout.status_code == 204
        assert client.get("/api/auth/session").json() == {"authenticated": False}
        assert client.post("/api/auth/logout", headers={"Origin": ORIGIN}).status_code == 204


def test_cookie_is_invalid_after_password_hash_changes() -> None:
    original_settings = _settings()
    with _client(original_settings) as original:
        original.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "aidison", "password": PASSWORD},
        )
        cookie = original.cookies.get("__Host-aidison_session")
        assert cookie

    changed_password = _settings(password="a-different-test-password")
    with _client(changed_password) as changed:
        response = changed.get(
            "/api/integration-health",
            headers={"Cookie": f"__Host-aidison_session={cookie}"},
        )
        assert response.status_code == 401


def test_disabled_auth_preserves_local_development_behavior() -> None:
    settings = ApplicationAuthSettings(enabled=False)
    with _client(settings) as client:
        assert client.get("/api/integration-health").status_code == 200
        assert client.get("/api/auth/session").json() == {
            "authenticated": True,
            "username": "local-development",
        }
