"""Loopback token-exchange stub for SDKs that insist on minting GCP tokens.

OpenShell v0.0.116-rhaiv.8+ injects Vertex credentials into the sandbox as
an opaque placeholder env var and swaps it for the real access token in
the supervisor proxy. Claude Code can send that placeholder directly as a
bearer token, but OpenCode's ``@ai-sdk/google-vertex`` always asks
``google-auth-library`` for a token first, and that library has no static
token mode: every credential type exchanges something over HTTP.

The cheapest credential to satisfy is ``external_account``: its
``token_url`` is read verbatim from the ADC JSON, so pointing it at this
stub makes ``getAccessToken()`` resolve to the placeholder without any
network access. The SDK then sends ``Authorization: Bearer <placeholder>``
and the proxy does the rest.

The stub binds loopback only and answers every POST with the same token.
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN_ENV_VAR = "AGENTIC_CI_VERTEX_TOKEN"
DEFAULT_PORT = 8175


def token_response(token: str) -> bytes:
    """Render the OAuth token-exchange response body for *token*."""
    return json.dumps(
        {
            "access_token": token,
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    ).encode()


class _Handler(BaseHTTPRequestHandler):
    token = ""

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = token_response(self.token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        sys.stderr.write("vertex-token-stub: " + format % args + "\n")


def make_server(token: str, port: int = DEFAULT_PORT) -> HTTPServer:
    """Create the loopback HTTP server. ``port=0`` picks a free port."""
    handler = type("TokenHandler", (_Handler,), {"token": token})
    return HTTPServer(("127.0.0.1", port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="agentic-ci vertex-token-stub",
        description="Serve a GCP token-exchange stub on loopback (OpenShell sandbox use).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV_VAR, "")
    if not token:
        print(f"{TOKEN_ENV_VAR} is not set; refusing to serve an empty token", file=sys.stderr)
        return 1
    server = make_server(token, args.port)
    print(f"vertex-token-stub: listening on 127.0.0.1:{args.port}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0
