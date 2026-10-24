"""Shared settings sources for non-secret YAML configuration and env secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class AidisonYamlSettingsSource(YamlConfigSettingsSource):
    """Allow readable field names in YAML even when env aliases are uppercase."""

    def __call__(self) -> dict[str, object]:
        values = super().__call__()
        for field_name, field in self.settings_cls.model_fields.items():
            if field_name not in self.yaml_data:
                continue
            alias = field.validation_alias
            if isinstance(alias, str):
                values[alias] = self.yaml_data[field_name]
            elif isinstance(alias, AliasChoices) and alias.choices:
                values[str(alias.choices[0])] = self.yaml_data[field_name]
            else:
                values[field_name] = self.yaml_data[field_name]
        return values


class AidisonSettings(BaseSettings):
    """Load YAML defaults, then let environment and secrets override them."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
    yaml_section: ClassVar[str | None] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_path = Path(os.getenv("AIDISON_CONFIG_FILE", "config.yaml"))
        yaml_settings = AidisonYamlSettingsSource(
            settings_cls,
            yaml_file=config_path if config_path.is_file() else None,
            yaml_config_section=cls.yaml_section if config_path.is_file() else None,
        )
        # Explicit constructor values > environment > .env > YAML defaults.
        return (init_settings, env_settings, dotenv_settings, yaml_settings, file_secret_settings)


class DraftSettings(AidisonSettings):
    """Non-secret controls for durable DIY draft interaction."""

    yaml_section = "draft"

    adjustment_batch_window_seconds: float = Field(
        default=2.0,
        validation_alias="AIDISON_ADJUSTMENT_BATCH_WINDOW_SECONDS",
        gt=0,
        le=30,
    )


class ConversationSettings(AidisonSettings):
    """Non-secret controls for project-scoped conversation."""

    yaml_section = "conversation"

    max_turns: int = Field(
        default=20,
        validation_alias="AIDISON_CONVERSATION_MAX_TURNS",
        ge=1,
        le=100,
    )
    max_tokens_per_turn: int = Field(
        default=2_000,
        validation_alias="AIDISON_CONVERSATION_MAX_TOKENS_PER_TURN",
        ge=100,
        le=16_384,
    )
    model_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
    )


class ApplicationAuthSettings(AidisonSettings):
    """Single-owner application session settings.

    Authentication is disabled for local development and tests. Production
    Compose enables it explicitly and validation fails closed when any secret
    or the canonical HTTPS origin is absent.
    """

    enabled: bool = Field(default=False, validation_alias="AIDISON_AUTH_ENABLED")
    admin_user: str = Field(
        default="aidison",
        validation_alias="AIDISON_ADMIN_USER",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    admin_password_hash: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="AIDISON_ADMIN_PASSWORD_HASH",
    )
    session_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="AIDISON_SESSION_SECRET",
    )
    public_origin: str | None = Field(
        default=None,
        validation_alias="AIDISON_PUBLIC_ORIGIN",
    )
    session_ttl_seconds: int = Field(
        default=28_800,
        validation_alias="AIDISON_SESSION_TTL_SECONDS",
        ge=300,
        le=604_800,
    )

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> ApplicationAuthSettings:
        if not self.enabled:
            return self
        password_hash = self.admin_password_hash.get_secret_value()
        session_secret = self.session_secret.get_secret_value()
        if not password_hash.startswith(("$2a$", "$2b$", "$2y$")):
            raise ValueError("enabled application auth requires a bcrypt password hash")
        if len(session_secret) < 43:
            raise ValueError("enabled application auth requires a 32-byte URL-safe secret")
        if self.public_origin is None or not self.public_origin.startswith("https://"):
            raise ValueError("enabled application auth requires an HTTPS public origin")
        if self.public_origin.rstrip("/") != self.public_origin:
            raise ValueError("AIDISON_PUBLIC_ORIGIN must not end with a slash")
        return self
