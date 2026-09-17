import json
import logging
import errno
import socket
import ssl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError, ProtocolError, ProxyError

from app import SysPiper, connection_error_detail


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.json"
        self.key = "test-secret"
        self.headers = {"X-API-Key": self.key}
        self.app = self.make_app()
        self.client = self.app.app.test_client()

    def make_app(self, **overrides):
        config = {
            "api_key": self.key,
            "allowed_ips": ["127.0.0.1"],
            "allowed_nodes": {"hop": "http://hop.invalid"},
            "allowed_paths": {"status": "/api/status"},
        }
        config.update(overrides)
        self.config_path.write_text(json.dumps(config))
        app = SysPiper(str(self.config_path))
        app.app.testing = True
        return app

    @staticmethod
    def upstream(payload=None, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload or {"ok": True}).encode()
        response._content_consumed = True
        return response

    def test_every_proxy_endpoint_requires_key_and_allowed_ip(self):
        with patch("app.requests.get") as get:
            for path in ("/cpu/hop", "/ram/hop", "/disk/hop", "/net/hop", "/public_ip/hop"):
                for headers, address in (({}, "127.0.0.1"), ({"X-API-Key": "wrong"}, "127.0.0.1"),
                                         (self.headers, "198.51.100.10")):
                    with self.subTest(path=path, headers=headers, address=address):
                        response = self.client.get(path, headers=headers,
                                                   environ_overrides={"REMOTE_ADDR": address})
                        self.assertEqual(response.status_code, 401)
            get.assert_not_called()

    def test_local_remote_and_script_endpoints_require_auth(self):
        with patch("app.requests.get") as get, patch("app.safe_exec") as execute:
            for path in ("/ram", "/remote/hop/status", "/status@hop", "/examples/date"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 401)
            get.assert_not_called()
            execute.assert_not_called()

    def test_key_must_match_exactly(self):
        self.assertEqual(self.client.get("/ram", headers={"X-API-Key": "test-!secret"}).status_code, 401)
        self.assertEqual(self.client.get("/ram", headers={"X-API-Key": "tést-secret"}).status_code, 401)

    def test_missing_or_empty_configured_key_fails_at_startup(self):
        for key in (None, "", 123, "x" * 257):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.make_app(api_key=key)
        self.config_path.write_text("{}")
        with self.assertRaises(ValueError):
            SysPiper(str(self.config_path))

    def test_empty_acl_denies_and_missing_acl_retains_default(self):
        for allowed_ips, expected in (([], 401), (None, 200)):
            with self.subTest(allowed_ips=allowed_ips):
                app = self.make_app(allowed_ips=allowed_ips)
                response = app.app.test_client().get("/ram", headers=self.headers,
                                                     environ_overrides={"REMOTE_ADDR": "198.51.100.10"})
                self.assertEqual(response.status_code, expected)

    def test_ipv6_acl(self):
        app = self.make_app(allowed_ips=["::1", "2001:db8::/32"])
        for address, expected in (("::1", True), ("2001:db8::1", True), ("2001:db9::1", False)):
            self.assertEqual(app.is_remote_addr_allowed(address), expected)

    def test_auth_and_outgoing_headers_do_not_log_secrets(self):
        app = self.make_app(headers={"hop": [["Authorization", "outgoing-secret"]]})
        with self.assertLogs(app.logger, level=logging.DEBUG) as logs:
            app.logger.debug("start log capture")
            self.assertEqual(app.app.test_client().get("/ram", headers={"X-API-Key": "wrong-secret"}).status_code, 401)
            self.assertEqual(app.get_node_headers("hop"), {"Authorization": "outgoing-secret"})
        for secret in (self.key, "wrong-secret", "outgoing-secret"):
            self.assertNotIn(secret, "\n".join(logs.output))

    def test_proxy_strips_only_explicit_final_segment(self):
        app = self.make_app(allowed_nodes={"cpu": "http://hop.invalid/"})
        with patch("app.requests.get", return_value=self.upstream()) as get:
            response = app.app.test_client().get("/cpu/cpu", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_args.args[0], "http://hop.invalid/cpu")
        self.assertEqual(get.call_args.kwargs["headers"], {**self.headers, "X-SysPiper-Hops": "1"})

    def test_explicit_and_configured_hops_preserve_alias(self):
        app = self.make_app(allowed_nodes={"st": "http://hop.invalid"}, routes={"target": "st"})
        for path in ("/status@target/st", "/status@target"):
            with self.subTest(path=path), patch("app.requests.get", return_value=self.upstream()) as get:
                self.assertEqual(app.app.test_client().get(path, headers=self.headers).status_code, 200)
                self.assertEqual(get.call_args.args[0], "http://hop.invalid/status@target")

    def test_system_aliases_reach_authenticated_target(self):
        target = self.make_app(api_key="target-secret")
        gateway = self.make_app(
            allowed_nodes={"hs1": "http://hs1.invalid:8181"},
            allowed_paths={"sys_cpu": "/cpu", "sys_disk": "/disk"},
            headers={"hs1": [["X-API-Key", "target-secret"]]},
        )
        forwarded = []

        def forward(url, **kwargs):
            path = urlsplit(url).path
            forwarded.append(path)
            response = target.app.test_client().get(path, headers=kwargs["headers"])
            return self.upstream(response.json, response.status_code)

        with patch("app.requests.get", side_effect=forward), patch("app.psutil.cpu_percent", return_value=12.5):
            for alias in ("sys_cpu", "sys_disk"):
                with self.subTest(alias=alias):
                    response = gateway.app.test_client().get(f"/{alias}@hs1", headers=self.headers)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.json["status"], "ok")
        self.assertEqual(forwarded, ["/cpu", "/disk"])

    def test_upstream_http_status_is_reported_without_leaking_response(self):
        for status in (401, 403, 404, 422, 500):
            for path in ("/ram/hop", "/status@hop"):
                with self.subTest(status=status, path=path):
                    upstream = self.upstream({"secret": "upstream-secret"}, status)
                    upstream.url = "http://hop.invalid/url-secret"
                    with patch("app.requests.get", return_value=upstream), self.assertLogs(self.app.logger) as logs:
                        response = self.client.get(path, headers=self.headers)
                    self.assertEqual(response.status_code, 502)
                    self.assertIn(f"HTTP {status}", response.json["description"])
                    self.assertIn(f"HTTP {status}", "\n".join(logs.output))
                    for secret in ("upstream-secret", "url-secret"):
                        self.assertNotIn(secret, response.get_data(as_text=True))
                        self.assertNotIn(secret, "\n".join(logs.output))

    def test_connection_failures_report_nested_reason_without_secrets(self):
        refused = NewConnectionError(None, "url-secret")
        refused.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "password-secret")
        failures = (
            (NameResolutionError("host-secret", None, socket.gaierror(socket.EAI_NONAME, "password-secret")), "DNS error"),
            (refused, "ECONNREFUSED"),
            (OSError(errno.ENETUNREACH, "password-secret"), "ENETUNREACH"),
            (ProtocolError("url-secret", ConnectionResetError(errno.ECONNRESET, "password-secret")), "ECONNRESET"),
            (ProxyError("proxy-secret", PermissionError(errno.EACCES, "password-secret")), "EACCES"),
            (ssl.SSLCertVerificationError(1, "certificate-secret"), "SSLCertVerificationError"),
        )
        for reason, expected in failures:
            error = requests.ConnectionError(MaxRetryError(None, "/url-secret", reason))
            for path in ("/ram/hop", "/status@hop"):
                with self.subTest(reason=expected, path=path):
                    with patch("app.requests.get", side_effect=error), self.assertLogs(self.app.logger) as logs:
                        response = self.client.get(path, headers=self.headers)
                    self.assertEqual(response.status_code, 502)
                    self.assertIn(expected, response.json["description"])
                    self.assertIn(expected, "\n".join(logs.output))
                    self.assertNotIn("secret", response.get_data(as_text=True))
                    self.assertNotIn("secret", "\n".join(logs.output))

    def test_connection_error_context_cycles_are_bounded(self):
        error = requests.ConnectionError("password-secret")
        error.__context__ = error
        self.assertEqual(connection_error_detail(error), "ConnectionError")

    def test_proxy_hops_are_validated_and_bounded(self):
        for hops, expected in (("-1", 400), ("bad", 400), ("9" * 100, 400), ("8", 508), ("99", 508)):
            with self.subTest(hops=hops), patch("app.requests.get") as get:
                response = self.client.get("/ram/hop", headers={**self.headers, "X-SysPiper-Hops": hops})
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.json["code"], expected)
                get.assert_not_called()

    def test_routing_cycle_terminates_and_propagates_error(self):
        app = self.make_app(routes={"target": "hop"})
        visited = []

        def forward(url, **kwargs):
            visited.append(kwargs["headers"]["X-SysPiper-Hops"])
            self.assertLessEqual(len(visited), app.MAX_PROXY_HOPS)
            downstream = app.app.test_client().get(urlsplit(url).path, headers=kwargs["headers"])
            return self.upstream(downstream.json, downstream.status_code)

        with patch("app.requests.get", side_effect=forward):
            response = app.app.test_client().get("/status@target", headers=self.headers)
        self.assertEqual(response.status_code, 508)
        self.assertEqual(visited, [str(i) for i in range(1, app.MAX_PROXY_HOPS + 1)])

    def test_all_template_parts_are_replaced(self):
        app = self.make_app(allowed_nodes={"hop": "http://hop.invalid/base/"},
                            allowed_paths={"status": "/~~version~~/~~key~~/~~version~~"},
                            parts={"@version": {"hop": "v1"}, "@key": {"hop": "dummy"}})
        with patch("app.requests.get", return_value=self.upstream()) as get:
            self.assertEqual(app.app.test_client().get("/status@hop", headers=self.headers).status_code, 200)
        self.assertEqual(get.call_args.args[0], "http://hop.invalid/base/v1/dummy/v1")

    def test_missing_or_invalid_template_parts_fail_without_request(self):
        for path, parts in (("/~~missing~~", {}), ("/~~bad-name~~", {}),
                            ("/~~key~~", {"@key": {"hop": None}})):
            with self.subTest(path=path, parts=parts):
                app = self.make_app(allowed_paths={"status": path}, parts=parts)
                with patch("app.requests.get") as get:
                    response = app.app.test_client().get("/status@hop", headers=self.headers)
                self.assertEqual(response.status_code, 502)
                get.assert_not_called()

    def test_template_secrets_not_logged_on_failure(self):
        app = self.make_app(allowed_paths={"status": "/~~key~~"}, parts={"@key": {"hop": "url-secret"}})
        for error in (requests.ConnectionError("http://hop.invalid/url-secret"),
                      requests.Timeout("http://hop.invalid/url-secret")):
            with self.subTest(error=type(error).__name__), self.assertLogs(app.logger, logging.DEBUG) as logs:
                with patch("app.requests.get", side_effect=error):
                    response = app.app.test_client().get("/status@hop", headers=self.headers)
            self.assertNotIn("url-secret", "\n".join(logs.output))
            self.assertNotIn("url-secret", response.get_data(as_text=True))

    def test_nested_script_preserves_path(self):
        with patch("app.safe_exec", return_value={"result": {"ok": True}}) as execute:
            response = self.client.get("/examples/date", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(execute.call_args.args, ("examples/date", str(Path("scripts/examples/date.py").resolve())))

    def test_script_paths_reject_traversal_and_escaping_symlinks(self):
        for name in ("../app", "examples/../../app", "examples/./date", "/app", "examples//date", "x" * 256):
            with self.subTest(name=name):
                self.assertIsNone(self.app._script_path(name))
        with patch("app.Path", wraps=Path) as paths:
            root = Path(self.temp.name) / "scripts"
            root.mkdir()
            (root / "escape.py").symlink_to(self.config_path)
            paths.return_value = root
            self.assertIsNone(self.app._script_path("escape"))

    def test_malformed_flat_routes_are_rejected(self):
        for path in ("/status@@hop", "/status@hop/", "/status@hop/a/b", "/status!@hop"):
            with self.subTest(path=path), patch("app.requests.get") as get:
                self.assertEqual(self.client.get(path, headers=self.headers).status_code, 400)
                get.assert_not_called()

    def test_script_error_is_json(self):
        with patch("app.safe_exec", return_value={"error": "timeout"}):
            response = self.client.get("/examples/date", headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json["code"], 422)

    def test_non_json_upstream_is_gateway_error(self):
        upstream = self.upstream()
        upstream._content = b"not json"
        for path in ("/ram/hop", "/status@hop"):
            with self.subTest(path=path), patch("app.requests.get", return_value=upstream):
                self.assertEqual(self.client.get(path, headers=self.headers).status_code, 502)


if __name__ == "__main__":
    unittest.main()
