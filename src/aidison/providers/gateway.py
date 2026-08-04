from __future__ import annotations

from enum import StrEnum

from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderName(StrEnum):
    BAILIAN = "bailian"
    OPENAI = "openai"


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    provider: ProviderName = Field(
        default=ProviderName.BAILIAN,
        validation_alias="AIDISON_MODEL_PROVIDER",
    )
    dashscope_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="DASHSCOPE_API_KEY",
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="DASHSCOPE_BASE_URL",
    )
    dashscope_model: str = Field(
        default="qwen-plus",
        validation_alias="DASHSCOPE_MODEL",
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
    )
    openai_model: str = Field(
        default="gpt-5.2",
        validation_alias="OPENAI_MODEL",
    )


def build_chat_model(settings: ProviderSettings | None = None) -> ChatOpenAI:
    resolved = settings or ProviderSettings()
    if resolved.provider is ProviderName.BAILIAN:
        if resolved.dashscope_api_key is None:
            raise ProviderUnavailableError("DASHSCOPE_API_KEY is required for Bailian")
        return ChatOpenAI(
            model=resolved.dashscope_model,
            api_key=resolved.dashscope_api_key,
            base_url=resolved.dashscope_base_url,
            temperature=0,
            max_retries=0,
        )
    if resolved.openai_api_key is None:
        raise ProviderUnavailableError("OPENAI_API_KEY is required for OpenAI")
    return ChatOpenAI(
        model=resolved.openai_model,
        api_key=resolved.openai_api_key,
        temperature=0,
        max_retries=0,
    )
