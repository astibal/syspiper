import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

from upstream import fetch_result
from app import SysPiper


class TrackedBody(io.BytesIO):
    def __init__(self, data, *, forbid_read=False):
        super().__init__(data)
        self.bytes_read = 0
        self.forbid_read = forbid_read

    def read(self, size=-1):
        if self.forbid_read:
            raise AssertionError("Rejected response body must not be read")
        data = super().read(size)
        self.bytes_read += len(data)
        return data

    read1 = read


class FixtureAdapter(HTTPAdapter):
    """Exercise real Requests hooks/redirects and urllib3 decoding without sockets."""
    def __init__(self, body, *, status=200, headers=None):
        super().__init__()
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        if len(self.sent) > 1:
            raise AssertionError("A redirect must never trigger another request")
        raw = HTTPResponse(body=self.body, status=self.status, headers=self.headers,
                           preload_content=False, decode_content=False)
        return self.build_response(request, raw)


class HttpSafetyTests(unittest.TestCase):
    paths = ("/ram/hop", "/status@hop", "/public_ip")

    def setUp(self):
        # HTTP semantics are tested in-process; process cancellation has real-socket tests.
        transport = patch("app.fetch_with_deadline", side_effect=lambda options, deadline: fetch_result(options))
        transport.start()
        self.addCleanup(transport.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config_path = Path(self.temp.name) / "config.json"
        config_path.write_text(json.dumps({
            "api_key": "incoming-test-key", "allowed_ips": ["127.0.0.1"],
            "allowed_nodes": {"hop": "https://allowed.invalid"},
            "allowed_paths": {"status": "/api/status"},
            "headers": {"hop": [["X-API-Key", "outgoing-test-key"]]},
            "myip_url": "https://allowed.invalid/ip",
        }))
        self.service = SysPiper(str(config_path))
        self.service.app.testing = True

    def fetch(self, path, adapter):
        with requests.Session() as session:
            session.trust_env = False
            session.mount("https://", adapter)
            with patch("upstream.requests.get", side_effect=session.get):
                return self.service.app.test_client().get(
                    path, headers={"X-API-Key": "incoming-test-key"})

    def test_endpoint_read_timeouts_remain_distinct(self):
        for path, timeout in (("/ram/hop", 5), ("/status@hop", 3), ("/public_ip", 5)):
            with self.subTest(path=path):
                adapter = FixtureAdapter(TrackedBody(b'{}'))
                with patch.object(adapter, 'send', wraps=adapter.send) as send:
                    self.assertEqual(self.fetch(path, adapter).status_code, 200)
                self.assertEqual(send.call_args.kwargs['timeout'], timeout)

    def test_redirects_are_rejected_before_body_read_or_credentials_forwarding(self):
        for status in (300, 301, 302, 303, 304, 307, 308):
            for location in ("https://other.invalid/secret", "/same-host-secret"):
                for path in self.paths:
                    with self.subTest(status=status, location=location, path=path):
                        body = TrackedBody(b"body-secret", forbid_read=True)
                        adapter = FixtureAdapter(body, status=status, headers={"Location": location})
                        response = self.fetch(path, adapter)
                        self.assertEqual(response.status_code, 502)
                        self.assertEqual(response.json["description"], "Upstream redirects are not allowed")
                        self.assertEqual(len(adapter.sent), 1)
                        self.assertTrue(body.closed)
                        self.assertNotIn("Location", response.headers)
                        self.assertNotIn("secret", response.get_data(as_text=True))
                        expected_key = {"/ram/hop": "incoming-test-key",
                                        "/status@hop": "outgoing-test-key", "/public_ip": None}[path]
                        self.assertEqual(adapter.sent[0].headers.get("X-API-Key"), expected_key)

    def test_declared_oversize_body_is_rejected_without_reading(self):
        for path in self.paths:
            with self.subTest(path=path):
                body = TrackedBody(b"", forbid_read=True)
                adapter = FixtureAdapter(body, headers={
                    "Content-Length": str(self.service.MAX_UPSTREAM_BYTES + 1)})
                response = self.fetch(path, adapter)
                self.assertEqual(response.status_code, 502)
                self.assertIn("1 MiB", response.json["description"])
                self.assertTrue(body.closed)

    def test_stream_size_boundary_without_content_length(self):
        limit = self.service.MAX_UPSTREAM_BYTES
        for size, expected in ((limit, 200), (limit + 1, 502)):
            for path in self.paths:
                with self.subTest(size=size, path=path):
                    payload = b'"' + b'x' * (size - 2) + b'"'
                    body = TrackedBody(payload)
                    response = self.fetch(path, FixtureAdapter(body))
                    self.assertEqual(response.status_code, expected)
                    self.assertTrue(body.closed)
                    if expected == 200:
                        self.assertEqual(response.json if path != "/public_ip" else response.json["ip"],
                                         "x" * (size - 2) if path != "/public_ip" else payload.decode())

    def test_oversize_stream_is_stopped_before_reading_the_rest(self):
        for path in self.paths:
            with self.subTest(path=path):
                payload = b"x" * (4 * self.service.MAX_UPSTREAM_BYTES)
                body = TrackedBody(payload)
                response = self.fetch(path, FixtureAdapter(body))
                self.assertEqual(response.status_code, 502)
                self.assertLessEqual(body.bytes_read, self.service.MAX_UPSTREAM_BYTES + (64 << 10))
                self.assertTrue(body.closed)

    def test_compressed_size_limit_applies_after_decompression(self):
        limit = self.service.MAX_UPSTREAM_BYTES
        for size, expected in ((limit, 200), (limit + 1, 502)):
            for path in self.paths:
                with self.subTest(size=size, path=path):
                    compressed = gzip.compress(b'"' + b'x' * (size - 2) + b'"')
                    self.assertLess(len(compressed), limit)
                    body = TrackedBody(compressed)
                    response = self.fetch(path, FixtureAdapter(body, headers={
                        "Content-Encoding": "gzip", "Content-Length": str(len(compressed))}))
                    self.assertEqual(response.status_code, expected)
                    self.assertTrue(body.closed)

    def test_error_bodies_are_closed_without_reading(self):
        for path in self.paths:
            with self.subTest(path=path):
                body = TrackedBody(b"body-secret", forbid_read=True)
                response = self.fetch(path, FixtureAdapter(body, status=500))
                self.assertEqual(response.status_code, 502)
                self.assertTrue(body.closed)
                self.assertNotIn("body-secret", response.get_data(as_text=True))

    def test_invalid_json_closes_response(self):
        for path in self.paths[:2]:
            with self.subTest(path=path):
                body = TrackedBody(b"not json")
                response = self.fetch(path, FixtureAdapter(body))
                self.assertEqual(response.status_code, 502)
                self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
