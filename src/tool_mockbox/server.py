"""A loopback-only fixture protocol, not MCP or a production HTTP mock server."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket

from .core import MockToolbox, MockToolError, ScenarioError, UnexpectedCall, parse_json, validate_request


MAX_BODY_BYTES = 65_536
READ_TIMEOUT_SECONDS = 5


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, toolbox: MockToolbox, port: int):
        self.toolbox = toolbox
        super().__init__(("127.0.0.1", port), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ToolMockbox/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT_SECONDS)

    def log_message(self, format, *args):
        pass  # Do not log tool arguments or request data to the terminal.

    def _json(self, status, payload):
        self.close_connection = True
        data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # A disconnected local client cannot consume an HTTP response.

    def _error(self, status, code, message):
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def send_error(self, code, message=None, explain=None):
        self._error(code, "http_error", "The HTTP request could not be processed")

    def _boundary(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1 or host_values[0].lower() not in hosts:
            self._error(403, "host_rejected", "Host must identify this loopback server and port")
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] not in {"http://" + host for host in hosts}):
            self._error(403, "origin_rejected", "Origin must identify this loopback server and port")
            return False
        return True

    def do_GET(self):
        if not self._boundary():
            return
        if self.path == "/catalog":
            self._json(200, self.server.toolbox.catalog())
        elif self.path == "/events":
            self._json(200, self.server.toolbox.report())
        else:
            self._error(404, "not_found", "Use GET /catalog, GET /events, or POST /call")

    def do_OPTIONS(self):
        self._error(403, "cors_disabled", "Cross-origin browser access is not supported")

    def do_POST(self):
        if not self._boundary():
            return
        if self.path != "/call":
            self._error(404, "not_found", "Use POST /call")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(400, "transfer_encoding_rejected", "Transfer-Encoding is not supported")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(411, "length_required", "One Content-Length header is required")
            return
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            self._error(400, "invalid_length", "Content-Length must be one nonnegative integer")
            return
        # Bound the decimal token itself before converting unusually long values.
        if len(lengths[0]) > 8:
            self._error(413, "body_too_large", "Request body exceeds 65536 bytes")
            return
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            self._error(413, "body_too_large", "Request body exceeds 65536 bytes")
            return
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._error(415, "json_required", "Content-Type must be application/json")
            return
        try:
            body = self.rfile.read(length)
        except (TimeoutError, socket.timeout):
            self._error(408, "read_timeout", "Request body read timed out")
            return
        if len(body) != length:
            self._error(400, "incomplete_body", "Request body was shorter than Content-Length")
            return
        try:
            request = validate_request(parse_json(body.decode("utf-8")))
        except (ScenarioError, UnicodeError, RecursionError):
            self._error(400, "invalid_request", "Expected a UTF-8 JSON object with tool and arguments")
            return
        try:
            value = self.server.toolbox.call(request["tool"], request["arguments"])
        except UnexpectedCall as error:
            self._json(409, {"ok": False, "call_id": error.call_id,
                             "error": {"code": error.code, "message": error.message}})
        except MockToolError as error:
            # A fixture's tool-level error is a valid protocol response, not a
            # claim that a real HTTP service or network connection failed.
            self._json(200, {"ok": False, "call_id": error.call_id,
                             "error": {"code": error.code, "message": error.message}})
        except ScenarioError:
            self._error(400, "invalid_request", "Request arguments are unsupported")
        else:
            self._json(200, {"ok": True, "result": value})


def create_server(toolbox: MockToolbox, *, port: int = 0) -> FixtureServer:
    """Create an IPv4 loopback fixture server; call serve_forever yourself."""
    if not isinstance(toolbox, MockToolbox):
        raise TypeError("toolbox must be a MockToolbox")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    return FixtureServer(toolbox, port)
