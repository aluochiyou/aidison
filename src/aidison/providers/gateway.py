from __future__ import annotations

from enum import StrEnum

from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr

from aidison.config import AidisonSettings


class ProviderName(StrEnum):
    BAILIAN = "bailian"


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderSettings(AidisonSettings):
    yaml_section = "model"

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
        default="deepseek-v4-pro",
        validation_alias="DASHSCOPE_MODEL",
    )


def build_chat_model(settings: ProviderSettings | None = None) -> ChatOpenAI:
    resolved = settings or ProviderSettings()
    if resolved.dashscope_api_key is None:
        raise ProviderUnavailableError("DASHSCOPE_API_KEY is required for Bailian")
    return ChatOpenAI(
        model=resolved.dashscope_model,
        api_key=resolved.dashscope_api_key,
        base_url=resolved.dashscope_base_url,
        temperature=0,
        max_retries=0,
    )
