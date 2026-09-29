"""Tests for the loopback GCP token-exchange stub."""

import json
import threading
import urllib.request

from agentic_ci import vertex_token_stub


def test_server_returns_placeholder_token_for_any_post():
    server = vertex_token_stub.make_server("openshell:resolve:env:abc", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/token",
            data=b"grant_type=urn:ietf:params:oauth:grant-type:token-exchange",
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.load(response)
    finally:
        server.shutdown()
        server.server_close()

    assert body["access_token"] == "openshell:resolve:env:abc"
    assert body["token_type"] == "Bearer"
    assert body["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert body["expires_in"] == 3600


def test_server_binds_loopback_only():
    server = vertex_token_stub.make_server("t", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_main_refuses_empty_token(monkeypatch, capsys):
    monkeypatch.delenv(vertex_token_stub.TOKEN_ENV_VAR, raising=False)
    assert vertex_token_stub.main(["--port", "0"]) == 1
    assert "AGENTIC_CI_VERTEX_TOKEN is not set" in capsys.readouterr().err
