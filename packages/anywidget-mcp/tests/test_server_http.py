from __future__ import annotations

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from anywidget_mcp.server import AnyWidgetMCP


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1", "http://127.0.0.1:7878"),
        ("localhost", "http://localhost:8082"),
        ("::1", "http://[::1]:7878"),
    ],
)
def test_local_browser_can_initialize_and_use_http_session(
    host: str, origin: str
) -> None:
    server = AnyWidgetMCP("test")
    app = server.streamable_http_app(host=host, json_response=True)
    with TestClient(app, base_url="http://127.0.0.1:8010") as client:
        for method in ("POST", "HEAD"):
            preflight = client.options(
                "/mcp",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": (
                        "authorization,content-type,mcp-protocol-version,mcp-session-id"
                    ),
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == origin
            assert method in preflight.headers["access-control-allow-methods"]
            assert "mcp-session-id" in preflight.headers["access-control-allow-headers"]
        head = client.head("/mcp", headers={"Origin": origin})
        assert head.status_code == 200
        assert head.headers["access-control-allow-origin"] == origin

        headers = {
            "Origin": origin,
            "Accept": "application/json, text/event-stream",
        }
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "browser", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.headers["access-control-allow-origin"] == origin
        assert "Mcp-Session-Id" in initialized.headers["access-control-expose-headers"]
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        headers["Mcp-Protocol-Version"] = initialized.json()["result"][
            "protocolVersion"
        ]
        ready = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert ready.status_code == 202
        tools = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert tools.status_code == 200
        assert tools.headers["access-control-allow-origin"] == origin
        assert "anywidget_state" in {
            tool["name"] for tool in tools.json()["result"]["tools"]
        }
        deleted = client.delete("/mcp", headers=headers)
        assert deleted.status_code == 200
        assert deleted.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://host.example.com",
        "null",
        "http://localhost.example.com:7878",
        "http://127.0.0.1:7878@host.example.com",
    ],
)
def test_default_cors_rejects_untrusted_browser_origins(origin: str) -> None:
    with TestClient(AnyWidgetMCP("test").streamable_http_app()) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_explicit_cors_origins_replace_local_defaults() -> None:
    server = AnyWidgetMCP("test", cors_origins=["http://localhost:8082"])
    with TestClient(server.streamable_http_app()) as client:
        for origin, status in (
            ("http://localhost:8082", 200),
            ("http://localhost:7878", 400),
        ):
            response = client.options(
                "/mcp",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.status_code == status
            assert response.headers.get("access-control-allow-origin") == (
                origin if status == 200 else None
            )


def test_disabled_cors_keeps_the_http_endpoint_available() -> None:
    app = AnyWidgetMCP("test", cors_origins=()).streamable_http_app()
    with TestClient(app) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "http://localhost:7878",
                "Access-Control-Request-Method": "POST",
            },
        )
        head = client.head("/mcp", headers={"Origin": "http://localhost:7878"})
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers
    assert head.status_code == 200
    assert "access-control-allow-origin" not in head.headers
    assert head.headers["allow"] == "GET, POST, DELETE, HEAD"


def test_network_bind_requires_explicit_cors_origins() -> None:
    app = AnyWidgetMCP("test").streamable_http_app(host="0.0.0.0")
    with TestClient(app) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "http://localhost:7878",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


def test_cors_preserves_transport_security_origin_validation() -> None:
    app = AnyWidgetMCP("test").streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*"],
            allowed_origins=["http://localhost:8082"],
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8010") as client:
        response = client.post(
            "/mcp",
            headers={"Origin": "http://localhost:7878"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code == 403
