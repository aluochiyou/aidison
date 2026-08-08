from pathlib import Path

from aidison.application.shopping import ShoppingSettings
from aidison.providers.gateway import ProviderSettings
from aidison.tools.web_search import TavilyMcpSearchSettings


def test_yaml_defaults_and_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # The developer shell may export TAVILY_* values; keep the YAML section
    # assertions deterministic by removing any ambient overrides.
    monkeypatch.delenv("TAVILY_MCP_URL", raising=False)
    monkeypatch.delenv("TAVILY_SEARCH_DEPTH", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n  provider: deepseek\n  deepseek_model: yaml-model\n"
        "research:\n  mcp_url: https://example.test/mcp\n  search_depth: basic\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(config))
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")

    provider = ProviderSettings()
    research = TavilyMcpSearchSettings()

    assert provider.provider.value == "deepseek"
    assert provider.deepseek_model == "env-model"
    assert research.mcp_url == "https://example.test/mcp"
    assert research.search_depth == "basic"


def test_effect_approval_ttl_is_non_secret_yaml_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "shopping:\n  effect_approval_ttl_seconds: 1200\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(config))

    assert ShoppingSettings().effect_approval_ttl_seconds == 1200


def test_settings_work_without_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(tmp_path / "missing.yaml"))

    provider = ProviderSettings()

    assert provider.deepseek_model == "deepseek-v4-pro"


def test_legacy_dashscope_env_alias_backcompat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "legacy-key")

    provider = ProviderSettings()

    assert provider.deepseek_api_key is not None
    assert provider.deepseek_api_key.get_secret_value() == "legacy-key"


def test_deepseek_env_wins_over_legacy_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "legacy-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "primary-key")

    provider = ProviderSettings()

    assert provider.deepseek_api_key is not None
    assert provider.deepseek_api_key.get_secret_value() == "primary-key"
