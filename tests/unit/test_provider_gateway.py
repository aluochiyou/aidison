import pytest
from pydantic import SecretStr

from aidison.providers.gateway import (
    ProviderName,
    ProviderSettings,
    ProviderUnavailableError,
    build_chat_model,
)


def test_deepseek_fails_closed_without_key() -> None:
    with pytest.raises(ProviderUnavailableError, match="DEEPSEEK_API_KEY"):
        build_chat_model(
            ProviderSettings(
                provider=ProviderName.DEEPSEEK,
                deepseek_api_key=None,
            )
        )


def test_deepseek_build_uses_official_route() -> None:
    model = build_chat_model(ProviderSettings(deepseek_api_key=SecretStr("test-key")))
    assert model.model_name == "deepseek-v4-pro"
    assert model.openai_api_base == "https://api.deepseek.com"
    assert model.max_retries == 0


def test_deepseek_model_uses_explicit_openai_compatible_route() -> None:
    model = build_chat_model(
        ProviderSettings(
            provider=ProviderName.DEEPSEEK,
            deepseek_api_key=SecretStr("test-key"),
            deepseek_base_url="https://example.invalid/v1",
            deepseek_model="qwen-plus",
        )
    )
    assert model.model_name == "qwen-plus"
    assert model.openai_api_base == "https://example.invalid/v1"
    assert model.max_retries == 0
