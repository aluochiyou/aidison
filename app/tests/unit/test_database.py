from configparser import ConfigParser

from aidison.infrastructure.database import alembic_config_url


def test_alembic_config_url_preserves_percent_encoded_database_password() -> None:
    database_url = "postgresql+asyncpg://aidison:abc%40example.com@localhost:5432/aidison"
    parser = ConfigParser()
    parser.add_section("alembic")

    parser.set("alembic", "sqlalchemy.url", alembic_config_url(database_url))

    assert parser.get("alembic", "sqlalchemy.url") == database_url
