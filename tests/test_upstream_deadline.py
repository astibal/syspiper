"""Real helper processes and loopback HTTP, including slow headers and bodies."""
import gzip
import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app import SysPiper
from upstream import fetch_with_deadline


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.seen = threading.Event()
        self.stop = threading.Event()
        self.proxy_path = "/ok"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.seen.set()
                path = owner.proxy_path if self.path == '/ram' else self.path
                try:
                    if path == '/slow-headers':
                        self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                        self.wfile.flush()
                    elif path == '/slow-body':
                        self.send_response(200)
                        self.send_header('Content-Length', '100000')
                        self.end_headers()
                    else:
                        payload = b'{"ok":true}'
                        self.send_response(302 if self.path == '/redirect' else 200)
                        if self.path == '/redirect':
                            self.send_header('Location', '/ok')
                        if self.path == '/compressed':
                            payload = gzip.compress(b'"' + b'x' * (1 << 20) + b'"')
                            self.send_header('Content-Encoding', 'gzip')
                        self.send_header('Content-Length', str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    while not owner.stop.wait(0.03):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                except (OSError, ConnectionError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def cleanup_server(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def service(self, path):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({
            'api_key': 'test', 'allowed_ips': ['127.0.0.1'],
            'allowed_nodes': {'hop': self.base}, 'allowed_paths': {'status': path},
            'myip_url': self.base + path, 'upstream_deadline_seconds': 0.7,
        }))
        return SysPiper(config)

    def test_trickling_headers_and_body_are_cancelled_and_children_reaped(self):
        real_popen = subprocess.Popen
        children = []

        def record(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        for path in ('/slow-headers', '/slow-body'):
            # All call sites, including execution outside the main thread.
            for endpoint in ('/public_ip', '/status@hop', '/ram/hop'):
                with self.subTest(path=path, endpoint=endpoint):
                    service = self.service(path)
                    self.proxy_path = path
                    self.seen.clear()
                    def request():
                        return service.app.test_client().get(endpoint, headers={'X-API-Key': 'test'})
                    started = time.monotonic()
                    with patch('upstream.subprocess.Popen', side_effect=record), ThreadPoolExecutor(max_workers=1) as pool:
                        response = pool.submit(request).result(timeout=4)
                    self.assertEqual(response.status_code, 504)
                    self.assertIn('total time limit', response.json['description'])
                    self.assertTrue(self.seen.is_set())
                    self.assertLess(time.monotonic() - started, 2.5)
                    self.assertIsNotNone(children[-1].poll())
                    self.assertTrue(children[-1].stdout.closed)
                    self.assertTrue(children[-1].stdin.closed)

    def test_real_helper_success_redirect_and_decompression_limit(self):
        for path, code in (('/ok', None), ('/redirect', 502), ('/compressed', 502)):
            with self.subTest(path=path):
                result = fetch_with_deadline(dict(url=self.base + path, headers={}, timeout=3), 5)
                if code:
                    self.assertEqual(result['error']['code'], code)
                else:
                    self.assertEqual(result, {'value': {'ok': True}})

    def test_hung_helper_before_response_is_killed(self):
        helper = Path(self.temp.name) / 'hang.py'
        helper.write_text('import time\ntime.sleep(60)\n')
        started = time.monotonic()
        with patch('upstream.__file__', str(helper)):
            result = fetch_with_deadline(dict(url=self.base, headers={}, timeout=5), 0.2)
        self.assertEqual(result['error']['code'], 504)
        self.assertLess(time.monotonic() - started, 2)
