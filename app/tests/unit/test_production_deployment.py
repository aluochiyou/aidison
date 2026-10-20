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

    networks = compose["networks"]
    assert isinstance(networks, dict)
    assert networks["app-internal"] == {"internal": True}


def test_public_connector_reaches_only_authenticated_gateway() -> None:
    compose = _production_compose()
    services = compose["services"]
    assert isinstance(services, dict)
    cloudflared = services["cloudflared"]
    assert isinstance(cloudflared, dict)
    assert cloudflared["depends_on"] == {"gateway": {"condition": "service_healthy"}}
    assert cloudflared["networks"] == ["edge"]
    assert "--token" not in cloudflared["command"]
    assert cloudflared["environment"] == {
        "TUNNEL_TOKEN": "${CLOUDFLARE_TUNNEL_TOKEN:?set CLOUDFLARE_TUNNEL_TOKEN}"
    }

    caddyfile = (APP_ROOT / "ops" / "Caddyfile.production").read_text(encoding="utf-8")
    assert "basic_auth" in caddyfile
    assert "reverse_proxy web:3000" in caddyfile
    assert "X-Frame-Options \"DENY\"" in caddyfile
