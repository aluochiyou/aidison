from pathlib import Path

from aidison.providers.gateway import ProviderSettings
from aidison.tools.web_search import TavilyMcpSearchSettings


def test_yaml_defaults_and_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n  provider: openai\n  openai_model: yaml-model\n"
        "research:\n  mcp_url: https://example.test/mcp\n  search_depth: basic\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(config))
    monkeypatch.setenv("OPENAI_MODEL", "env-model")

    provider = ProviderSettings()
    research = TavilyMcpSearchSettings()

    assert provider.provider.value == "openai"
    assert provider.openai_model == "env-model"
    assert research.mcp_url == "https://example.test/mcp"
    assert research.search_depth == "basic"


def test_settings_work_without_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIDISON_CONFIG_FILE", str(tmp_path / "missing.yaml"))

    provider = ProviderSettings()

    assert provider.openai_model == "gpt-5.2"
