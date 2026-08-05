"""Shared settings sources for non-secret YAML configuration and env secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic import AliasChoices
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
