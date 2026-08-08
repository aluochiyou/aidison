from __future__ import annotations

from enum import StrEnum

from langchain_openai import ChatOpenAI
from pydantic import AliasChoices, Field, SecretStr

from aidison.config import AidisonSettings


class ProviderName(StrEnum):
    DEEPSEEK = "deepseek"


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderSettings(AidisonSettings):
    yaml_section = "model"

    provider: ProviderName = Field(
        default=ProviderName.DEEPSEEK,
        validation_alias="AIDISON_MODEL_PROVIDER",
    )
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "DASHSCOPE_BASE_URL"),
    )
    deepseek_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "DASHSCOPE_MODEL"),
    )


def build_chat_model(settings: ProviderSettings | None = None) -> ChatOpenAI:
    resolved = settings or ProviderSettings()
    if resolved.deepseek_api_key is None:
        raise ProviderUnavailableError("DEEPSEEK_API_KEY is required for DeepSeek")
    return ChatOpenAI(
        model=resolved.deepseek_model,
        api_key=resolved.deepseek_api_key,
        base_url=resolved.deepseek_base_url,
        temperature=0,
        max_retries=0,
    )
