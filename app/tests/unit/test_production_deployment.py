from __future__ import annotations

from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[2]


def _production_compose() -> dict[str, object]:
    value = yaml.safe_load((APP_ROOT / "compose.production.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_production_compose_exposes_only_loopback_gateway() -> None:
    compose = _production_compose()
    services = compose["services"]
    assert isinstance(services, dict)

    for service_name in ("postgres", "api", "worker", "web"):
        service = services[service_name]
        assert isinstance(service, dict)
        assert "ports" not in service

    gateway = services["gateway"]
    assert isinstance(gateway, dict)
    assert gateway["ports"] == ["127.0.0.1:${AIDISON_EDGE_PORT:-8080}:8080"]
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["cap_add"] == ["NET_BIND_SERVICE"]

    networks = compose["networks"]
    assert isinstance(networks, dict)
    assert networks["app-internal"] == {"internal": True}


def test_public_connector_reaches_only_authenticated_gateway() -> None:
    compose = _production_compose()
    services = compose["services"]
    assert isinstance(services, dict)
    cloudflared = services["cloudflared"]
    assert isinstance(cloudflared, dict)
    assert cloudflared["profiles"] == ["cloudflare"]
    assert cloudflared["depends_on"] == {"gateway": {"condition": "service_healthy"}}
    assert cloudflared["networks"] == ["edge"]
    assert "--token" not in cloudflared["command"]
    assert cloudflared["environment"] == {"TUNNEL_TOKEN": "${CLOUDFLARE_TUNNEL_TOKEN:-}"}

    caddyfile = (APP_ROOT / "ops" / "Caddyfile.production").read_text(encoding="utf-8")
    assert "basic_auth" in caddyfile
    assert "reverse_proxy web:3000" in caddyfile
    assert "X-Frame-Options \"DENY\"" in caddyfile


def test_caddy_keeps_the_only_business_route_behind_origin_authentication() -> None:
    caddyfile = (APP_ROOT / "ops" / "Caddyfile.production").read_text(encoding="utf-8")

    # The loopback health probe is intentionally the only unauthenticated path.
    assert "@edge_health path /edge-health" in caddyfile
    assert "handle @edge_health" in caddyfile
    assert caddyfile.count("\thandle ") == 2
    assert caddyfile.count("reverse_proxy ") == 1

    business_handler = caddyfile.split("\thandle {", maxsplit=1)[1]
    assert business_handler.index("basic_auth") < business_handler.index("reverse_proxy web:3000")


def test_production_initializer_requires_an_explicit_access_mode_choice() -> None:
    initializer = (APP_ROOT / "ops" / "initialize-production.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("CloudflareAccess", "OriginBasicAuth")]' in initializer
    assert '[string]$AccessMode = "CloudflareAccess"' in initializer
    assert 'if ($AccessMode -eq "CloudflareAccess")' in initializer
    assert "OriginBasicAuth mode is for a single trusted operator only" in initializer


def test_production_scripts_support_private_tailscale_without_a_tunnel_token() -> None:
    initializer = (APP_ROOT / "ops" / "initialize-production.ps1").read_text(encoding="utf-8")
    deployer = (APP_ROOT / "ops" / "deploy-production.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("Tailscale", "CloudflareTunnel")]' in initializer
    assert 'if ($PublishMode -eq "CloudflareTunnel")' in initializer
    assert '[ValidateSet("Tailscale", "CloudflareTunnel")]' in deployer
    assert '$requiredKeys += "CLOUDFLARE_TUNNEL_TOKEN"' in deployer


def test_deployer_resolves_relative_files_from_the_app_directory() -> None:
    deployer = (APP_ROOT / "ops" / "deploy-production.ps1").read_text(encoding="utf-8")

    assert '$appRoot = Split-Path -Parent $PSScriptRoot' in deployer
    assert '$composeFile = Join-Path $appRoot "compose.production.yaml"' in deployer
    assert '$EnvFile = Join-Path $appRoot $EnvFile' in deployer
