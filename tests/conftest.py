from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url

_LOCAL_COMPOSE_DATABASES = {
    ("localhost", 5432, "aidison"),
    ("127.0.0.1", 5432, "aidison"),
    ("::1", 5432, "aidison"),
}


def _database_endpoint(value: str) -> tuple[str, int, str]:  # pragma: no cover - pure helper
    url = make_url(value)
    return (
        (url.host or "").lower(),
        url.port or 5432,
        url.database or "",
    )


def _fail_if_runtime_database(endpoint: tuple[str, int, str]) -> None:
    """Fail unless the endpoint is isolated from the runtime database."""
    if os.getenv("AIDISON_ALLOW_SHARED_TEST_DATABASE") == "1":
        return

    if endpoint in _LOCAL_COMPOSE_DATABASES:
        pytest.fail(
            "TEST_DATABASE_URL points at the Aidison runtime database. "
            "Integration tests truncate tables; use the isolated localhost:55432 database, "
            "or set AIDISON_ALLOW_SHARED_TEST_DATABASE=1 only for an intentionally disposable DB."
        )

    runtime_database_url = os.getenv("DATABASE_URL")
    if runtime_database_url is not None and endpoint == _database_endpoint(runtime_database_url):
        pytest.fail(
            "TEST_DATABASE_URL matches the runtime DATABASE_URL. "
            "Integration tests truncate tables; use the isolated localhost:55432 database, "
            "or set AIDISON_ALLOW_SHARED_TEST_DATABASE=1 only for an intentionally disposable DB."
        )


@pytest.fixture(scope="session", autouse=True)
def _guard_integration_test_database(request: pytest.FixtureRequest) -> None:
    """Fail before any integration test connects to the runtime database."""
    marker = getattr(request.node, "closest_marker", lambda _: None)("integration")
    if marker is None:
        return

    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    _fail_if_runtime_database(_database_endpoint(database_url))


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail before collection if destructive tests target the local runtime DB."""
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if test_database_url is None:
        return
    if os.getenv("AIDISON_ALLOW_SHARED_TEST_DATABASE") == "1":
        return

    test_endpoint = _database_endpoint(test_database_url)
    runtime_database_url = os.getenv("DATABASE_URL")
    same_as_runtime = runtime_database_url is not None and test_endpoint == _database_endpoint(
        runtime_database_url
    )
    if test_endpoint in _LOCAL_COMPOSE_DATABASES or same_as_runtime:
        raise pytest.UsageError(
            "TEST_DATABASE_URL points at the Aidison runtime database. Integration tests "
            "truncate tables; use the isolated localhost:55432 database, or set "
            "AIDISON_ALLOW_SHARED_TEST_DATABASE=1 only for an intentionally disposable DB."
        )
