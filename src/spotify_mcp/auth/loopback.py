"""One-shot HTTP listener for the PKCE redirect.

Used only by the interactive `login` CLI command — never imported by the
running MCP server. Blocks the calling thread until the browser redirects
back with `?code=&state=`, or until `timeout_s` elapses.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

_SUCCESS_HTML = b"""<!doctype html>
<html><head><title>spotify-mcp</title></head>
<body style="font-family: sans-serif; text-align: center; margin-top: 4rem;">
<h2>Signed in.</h2><p>You can close this tab and return to the terminal.</p>
</body></html>"""

_ERROR_HTML = b"""<!doctype html>
<html><head><title>spotify-mcp</title></head>
<body style="font-family: sans-serif; text-align: center; margin-top: 4rem;">
<h2>Sign-in failed.</h2><p>Return to the terminal for details. You can close this tab.</p>
</body></html>"""


@dataclass
class CallbackResult:
    code: str | None
    state: str | None
    error: str | None


class _CallbackHandler(BaseHTTPRequestHandler):
    result: CallbackResult | None = None
    expected_path: str = "/callback"

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return

        query = parse_qs(parsed.query)
        error = query.get("error", [None])[0]
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        _CallbackHandler.result = CallbackResult(code=code, state=state, error=error)

        self.send_response(200 if code else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_SUCCESS_HTML if code else _ERROR_HTML)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence BaseHTTPRequestHandler's default stderr access log


def await_callback(port: int, path: str, timeout_s: float = 300.0) -> CallbackResult:
    """Bind 127.0.0.1:port, serve exactly one request, and return its result."""
    _CallbackHandler.result = None
    _CallbackHandler.expected_path = path
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout_s
    server.handle_request()
    server.server_close()
    if _CallbackHandler.result is None:
        return CallbackResult(code=None, state=None, error="timeout")
    return _CallbackHandler.result
