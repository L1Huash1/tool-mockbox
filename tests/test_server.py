import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tool_mockbox import MockToolbox
from tool_mockbox.server import create_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.box = MockToolbox({"schema_version": 1, "tools": [{"name": "lookup", "steps": [
            {"arguments": {"id": "demo"}, "status": "ok", "result": {"title": "Synthetic"}},
            {"arguments": {"id": "demo"}, "status": "error", "error": {"code": "temporary", "message": "Synthetic failure"}}
        ]}]}, no_wait=True)
        self.server = create_server(self.box)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.body = b'{"tool":"lookup","arguments":{"id":"demo"}}'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def request(self, method="GET", path="/catalog", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def raw(self, headers, body=b"", *, method="POST", path="/call", close_write=True):
        request = f"{method} {path} HTTP/1.0\r\n".encode() + b"\r\n".join(headers) + b"\r\n\r\n" + body
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as connection:
            connection.sendall(request)
            if close_write:
                connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                data = connection.recv(65536)
                if not data:
                    break
                chunks.append(data)
        head, payload = b"".join(chunks).split(b"\r\n\r\n", 1)
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        return status, json.loads(payload)

    def basic_headers(self):
        return [f"Host: {self.host}".encode(), b"Content-Type: application/json", f"Content-Length: {len(self.body)}".encode()]

    def assert_untouched(self):
        self.assertEqual(self.box.catalog()["tools"][0]["remaining"], 2)
        self.assertEqual(self.box.report()["events"], [])

    def test_binds_ipv4_loopback_and_reports_catalog(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body["tools"][0]["remaining"], 2)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Connection"], "close")
        self.assertFalse(any(name.lower().startswith("access-control-") for name in headers))

    def test_success_and_scripted_failure_are_valid_protocol_responses(self):
        first = self.request("POST", "/call", self.body, {"Content-Type": "application/json"})
        second = self.request("POST", "/call", self.body, {"Content-Type": "application/json"})
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2], {"ok": True, "result": {"title": "Synthetic"}})
        self.assertEqual(second[0], 200)
        self.assertFalse(second[2]["ok"])
        self.assertEqual(second[2]["error"]["code"], "temporary")
        self.assertEqual(second[2]["call_id"], "call-2")
        status, _, events = self.request(path="/events")
        self.assertEqual(status, 200)
        self.assertEqual(events["summary"]["calls"], 2)

    def test_unknown_mismatch_and_exhaustion_return_409(self):
        for body, code in ((b'{"tool":"other","arguments":null}', "unknown_tool"),
                           (b'{"tool":"lookup","arguments":null}', "arguments_mismatch")):
            status, _, response = self.request("POST", "/call", body, {"Content-Type": "application/json"})
            self.assertEqual((status, response["error"]["code"]), (409, code))
        for _ in range(2):
            self.request("POST", "/call", self.body, {"Content-Type": "application/json"})
        status, _, response = self.request("POST", "/call", self.body, {"Content-Type": "application/json"})
        self.assertEqual((status, response["error"]["code"]), (409, "sequence_exhausted"))

    def test_missing_foreign_and_duplicate_hosts_are_rejected(self):
        for hosts in ([], [b"Host: example.com"], [f"Host: 127.0.0.1:{self.server.server_port + 1}".encode()],
                      [f"Host: {self.host}".encode()] * 2):
            with self.subTest(hosts=hosts):
                status, response = self.raw(hosts, method="GET", path="/events")
                self.assertEqual((status, response["error"]["code"]), (403, "host_rejected"))
        self.assert_untouched()

    def test_localhost_and_same_origin_are_accepted(self):
        headers = {"Host": f"localhost:{self.server.server_port}", "Origin": f"http://localhost:{self.server.server_port}"}
        self.assertEqual(self.request(headers=headers)[0], 200)
        self.assertEqual(self.request(headers={"Origin": "http://" + self.host})[0], 200)

    def test_foreign_null_and_duplicate_origins_are_rejected(self):
        for origins in ([b"Origin: null"], [b"Origin: https://example.com"],
                        [f"Origin: http://{self.host}".encode()] * 2,
                        [f"Origin: https://{self.host}".encode()]):
            with self.subTest(origins=origins):
                status, response = self.raw(self.basic_headers() + origins, self.body)
                self.assertEqual((status, response["error"]["code"]), (403, "origin_rejected"))
        self.assert_untouched()

    def test_options_is_denied_without_cors(self):
        status, headers, response = self.request("OPTIONS", "/call")
        self.assertEqual((status, response["error"]["code"]), (403, "cors_disabled"))
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assert_untouched()

    def test_unknown_routes_and_query_variants_are_not_executed(self):
        for method, path in (("GET", "/"), ("GET", "/catalog?x=1"), ("POST", "/events"), ("POST", "/call?x=1")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path)[0], 404)
        self.assert_untouched()

    def test_unsupported_method_returns_json(self):
        status, _, response = self.request("DELETE", "/catalog")
        self.assertEqual(status, 501)
        self.assertEqual(response["error"]["code"], "http_error")
        self.assert_untouched()

    def test_missing_and_duplicate_lengths_are_rejected(self):
        base = [f"Host: {self.host}".encode(), b"Content-Type: application/json"]
        for lengths, expected in (([], 411), ([b"Content-Length: 1", b"Content-Length: 1"], 400),
                                  ([b"Content-Length: -1"], 400), ([b"Content-Length: +2"], 400),
                                  ([b"Content-Length: abc"], 400)):
            with self.subTest(lengths=lengths):
                self.assertEqual(self.raw(base + lengths)[0], expected)
        self.assert_untouched()

    def test_oversized_body_and_long_length_token_are_rejected_before_read(self):
        base = [f"Host: {self.host}".encode(), b"Content-Type: application/json"]
        for length in (b"65537", b"9" * 100):
            status, response = self.raw(base + [b"Content-Length: " + length])
            self.assertEqual((status, response["error"]["code"]), (413, "body_too_large"))
        self.assert_untouched()

    def test_transfer_encoding_is_rejected_even_with_content_length(self):
        status, response = self.raw(self.basic_headers() + [b"Transfer-Encoding: chunked"], self.body)
        self.assertEqual((status, response["error"]["code"]), (400, "transfer_encoding_rejected"))
        self.assert_untouched()

    def test_missing_wrong_and_duplicate_content_types_are_rejected(self):
        base = [f"Host: {self.host}".encode(), f"Content-Length: {len(self.body)}".encode()]
        for types in ([], [b"Content-Type: text/plain"], [b"Content-Type: application/json"] * 2):
            with self.subTest(types=types):
                self.assertEqual(self.raw(base + types, self.body)[0], 415)
        self.assert_untouched()

    def test_content_type_parameters_are_accepted(self):
        status, _, response = self.request("POST", "/call", self.body, {"Content-Type": "Application/JSON; charset=utf-8"})
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])

    def test_invalid_json_utf8_schema_and_duplicate_keys_do_not_consume(self):
        bodies = [b"", b"\xff", b"{", b"[]", b'{"tool":"lookup"}',
                  b'{"tool":"lookup","arguments":null,"extra":1}',
                  b'{"tool":"lookup","arguments":NaN}',
                  b'{"tool":"lookup","tool":"other","arguments":null}',
                  b'{"tool":"lookup","arguments":"\\ud800"}']
        for body in bodies:
            with self.subTest(body=body):
                status, _, response = self.request("POST", "/call", body, {"Content-Type": "application/json"})
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assert_untouched()

    def test_short_body_is_detected_after_client_closes_write(self):
        status, response = self.raw(self.basic_headers(), b"{}")
        self.assertEqual((status, response["error"]["code"]), (400, "incomplete_body"))
        self.assert_untouched()

    def test_inactive_body_read_returns_timeout(self):
        with patch("tool_mockbox.server.READ_TIMEOUT_SECONDS", 0.1):
            status, response = self.raw(self.basic_headers(), b"{", close_write=False)
        self.assertEqual((status, response["error"]["code"]), (408, "read_timeout"))
        self.assert_untouched()

    def test_event_limit_returns_conflict_without_consuming(self):
        with patch("tool_mockbox.core.MAX_EVENTS", 0):
            status, _, response = self.request("POST", "/call", self.body, {"Content-Type": "application/json"})
        self.assertEqual((status, response["error"]["code"]), (409, "event_limit"))
        self.assertIsNone(response["call_id"])
        self.assert_untouched()

    def test_invalid_server_arguments_fail_before_bind(self):
        for port in (True, -1, 65536, 1.0, "0"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                create_server(self.box, port=port)
        with self.assertRaises(TypeError):
            create_server({})

    def test_http_client_example_uses_actual_loopback_server(self):
        client = Path(__file__).resolve().parents[1] / "examples/http_client.py"
        for _ in range(2):
            result = subprocess.run([sys.executable, str(client), "--port", str(self.server.server_port)],
                                    capture_output=True, timeout=10)
            self.assertIn(b"HTTP 200", result.stdout)
            expected = 0 if self.box.report()["summary"]["calls"] == 1 else 1
            self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
