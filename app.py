import json
import re
import sys
import os
import logging
import requests
import argparse
import psutil
import ipaddress
import posixpath
import errno
import socket
import ssl
from functools import wraps
from fnmatch import fnmatchcase as glob_match
from pathlib import Path
from hmac import compare_digest
from sexec import safe_exec
import system_stats

from urllib.parse import urlparse, urljoin, urlunparse
from flask import Flask, request, jsonify, abort, make_response
from werkzeug.exceptions import HTTPException
from markupsafe import escape

from typing import List


class ProxyLoopDetected(HTTPException):
    code = 508
    description = "Proxy hop limit exceeded"


def connection_error_detail(error):
    """Describe nested transport failures without exposing URLs or credentials."""
    pending = [error]
    seen = set()
    types = []
    reasons = []
    while pending and len(seen) < 20:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        name = type(current).__name__
        if name not in types:
            types.append(name)
        if isinstance(current, socket.gaierror):
            reason = f"DNS error {current.errno}"
        elif isinstance(current, OSError) and not isinstance(current, ssl.SSLError) and current.errno in errno.errorcode:
            reason = errno.errorcode[current.errno]
        else:
            reason = None
        if reason and reason not in reasons:
            reasons.append(reason)
        nested = [current.__cause__, current.__context__, *current.args]
        # Requests/urllib3 also wrap causes in these attributes.
        nested.extend(getattr(current, attr, None) for attr in ("reason", "original_error", "_reason"))
        pending.extend(item for item in nested if isinstance(item, BaseException))
    detail = " -> ".join(types)
    if reasons:
        detail += " (" + ", ".join(reasons) + ")"
    return detail


def normalize_url(url):
    """Normalize a full URL, cleaning path slashes and dots."""
    parts = urlparse(url)

    # Normalize the path: remove duplicate slashes, resolve '.' and '..'
    normalized_path = posixpath.normpath(parts.path)

    # Special case: if original path ended with '/', preserve it
    if parts.path.endswith('/') and not normalized_path.endswith('/'):
        normalized_path += '/'

    normalized_parts = (
        parts.scheme,
        parts.netloc,
        normalized_path,
        parts.params,
        parts.query,
        parts.fragment
    )

    return urlunparse(normalized_parts)



class SysPiper:
    """Main application class for SysPiper."""

    MAX_PROXY_HOPS = 8
    MAX_UPSTREAM_BYTES = 1 << 20

    def __init__(self, config_path):
        """Initialize the app with configuration."""
        self.config = self.load_config(config_path)
        self.api_key = self.config.get("api_key")
        if not isinstance(self.api_key, str) or not 0 < len(self.api_key) <= 256:
            raise ValueError("api_key must be a non-empty string of at most 256 characters")
        self.log_level = self.config.get("log_level", "INFO").upper()
        self.myip_url = self.config.get("myip_url", "https://myip.dk")
        self._allowed_ips = self.config.get("allowed_ips", None)
        self.allowed_nodes = self.config.get("allowed_nodes", {})
        self.allowed_paths = self.config.get("allowed_paths", {})
        self.tls_verify = self.config.get("tls_verify", {})
        self.routes = self.config.get("routes", {}) # remote node -> next_hop (wildcard supported)

        self.allowed_ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        self.allowed_networks: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        self.remote_proxy = None

        if self._allowed_ips is None:
            self._allowed_ips = ["0.0.0.0/0"]

        for cidr in self._allowed_ips:
            if '/' in cidr:
                self.allowed_networks.append(ipaddress.ip_network(cidr, strict=False))
            else:
                self.allowed_ips.append(ipaddress.ip_address(cidr))


        self.listen_ip = None
        self.listen_port = None


        # Setup logging
        logging.basicConfig(level=self.log_level)
        self.logger = logging.getLogger(__name__)

        # Setup Flask app
        self.app = Flask(__name__)
        self.app.before_request(self.check_auth)
        self.setup_routes()
        self.register_error_handlers()

    def load_config(self, path):
        """Load configuration parameters from a JSON file."""
        try:
            with open(path, "r") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Failed to load configuration from {path}: {e}", file=sys.stderr)
            sys.exit(1)
        return config

    def is_remote_addr_allowed(self, cidr: str) -> bool:
        """Check if client IP is inside allowed IPs or networks."""
        client_ip = ipaddress.ip_address(cidr)

        for entry in self.allowed_networks:
            if client_ip in entry:
                return True

        for entry in self.allowed_ips:
            if client_ip == entry:
                return True

        return False

    def check_auth(self):
        """Verify the API key in the request headers."""
        if not self.is_remote_addr_allowed(request.remote_addr):
            abort(401, description="Unauthorized")

        api_key = request.headers.get("X-API-Key", "")
        if not compare_digest(api_key.encode("utf-8"), self.api_key.encode("utf-8")):
            abort(401, description="Unauthorized")

    def _fetch_upstream(self, url, *, headers, timeout, verify=True,
                        expect_json=True, propagate_loop=False):
        """Fetch one configured URL, with no redirects and a bounded body."""
        def reject_redirect(response, *args, **kwargs):
            if 300 <= response.status_code < 400:
                # Requests may consume a redirect body to prepare Response.next
                # even with allow_redirects=False. Reject it before that happens.
                response.close()
                abort(502, description="Upstream redirects are not allowed")

        with requests.get(url, headers=headers, timeout=timeout, verify=verify,
                          allow_redirects=False, stream=True,
                          hooks={"response": reject_redirect}) as response:
            if propagate_loop and response.status_code == 508:
                raise ProxyLoopDetected()
            response.raise_for_status()
            try:
                content_length = int(response.headers.get("Content-Length", ""))
            except ValueError:
                content_length = None
            if content_length is not None and content_length > self.MAX_UPSTREAM_BYTES:
                abort(502, description="Upstream response exceeds 1 MiB limit")

            body = bytearray()
            # iter_content decodes gzip/deflate, so the limit also covers the
            # expanded body and responses without a trustworthy Content-Length.
            for chunk in response.iter_content(chunk_size=64 << 10):
                if len(body) + len(chunk) > self.MAX_UPSTREAM_BYTES:
                    abort(502, description="Upstream response exceeds 1 MiB limit")
                body.extend(chunk)

            if expect_json:
                value = body.decode(response.encoding, errors="replace") if response.encoding else body
                return json.loads(value)
            return body.decode(response.encoding or "utf-8", errors="replace")

    def proxy_it(self, node, endpoint=None):
        """Proxy the request to the remote node."""
        target_url = self.allowed_nodes.get(node)
        if not target_url:
            abort(404, description="Node not allowed")

        hops = request.headers.get("X-SysPiper-Hops", "0")
        if not re.fullmatch(r"[0-9]{1,2}", hops):
            abort(400, description="Invalid proxy hop count")
        if int(hops) >= self.MAX_PROXY_HOPS:
            raise ProxyLoopDetected()

        headers = {"X-API-Key": self.api_key, "X-SysPiper-Hops": str(int(hops) + 1)}
        full_url = target_url.rstrip("/") + (endpoint if endpoint is not None else request.path)
        if SysPiper._contains_substitution(full_url):
            abort(502, description="substitution error: data missing or formatting error")

        try:
            self.logger.debug("Proxying to node %s", node)
            return jsonify(self._fetch_upstream(full_url, headers=headers, timeout=5,
                                                propagate_loop=True))

        except requests.exceptions.Timeout:
            self.logger.error("Timeout proxying to node %s", node)
            return jsonify({
                "status": "error",
                "code": 504,
                "name": "Gateway Timeout",
                "description": "Request timed out"}), 504

        except HTTPException:
            raise
        except requests.exceptions.HTTPError as error:
            status = error.response.status_code if error.response is not None else "unknown"
            self.logger.error("Proxy to node %s returned HTTP %s", node, status)
            abort(502, description=f"Target node returned HTTP {status}")
        except requests.exceptions.ConnectionError as error:
            detail = connection_error_detail(error)
            self.logger.error("Connection to node %s failed: %s", node, detail)
            abort(502, description=f"Connection to target node failed: {detail}")
        except Exception as e:
            self.logger.error("Proxy to node %s failed (%s)", node, type(e).__name__)
            abort(502, description="Failed to fetch from target node")

    def proxyable(self, func):
        """Decorator to proxy the request if 'node' parameter is present."""

        @wraps(func)
        def wrapper(*args, **kwargs):
            node = kwargs.get('node')
            if node:
                return self.proxy_it(node, request.path.rsplit("/", 1)[0])
            else:
                # Local call
                result = func(*args, **kwargs)
                return result

        return wrapper

    @staticmethod
    def _script_path(name):
        if len(name) > 256 or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name):
            return None
        if any(part in (".", "..") for part in name.split("/")):
            return None
        try:
            scripts_dir = Path("scripts").resolve()
            script_path = (scripts_dir / f"{name}.py").resolve()
            if script_path.is_relative_to(scripts_dir) and script_path.is_file():
                return script_path
        except (OSError, RuntimeError):
            # Invalid paths and symlink loops must not turn into a server error.
            pass
        return None

    def _run_sandbox(self, name):
        script_path = self._script_path(name)
        if script_path is None:
            return None

        result = safe_exec(name, str(script_path))
        if result is None:
            self.logger.error(f"Script '{name}' failed without response")
            return None

        for key in ("error", "output", "result"):
            if key in result:
                msg = result[key]
                if isinstance(msg, dict):
                    continue
                for line in str(msg).splitlines():
                    level = self.logger.error if key == "error" else self.logger.info
                    level(f"Script '{name}': {line}")

        if "result" in result and isinstance(result["result"], dict):
            return jsonify(result["result"])

        self.logger.error(f"Script '{name}': invalid or missing result")
        return None

    def register_error_handlers(self):
        """Register global JSON error handlers."""

        @self.app.errorhandler(400)
        @self.app.errorhandler(401)
        @self.app.errorhandler(403)
        @self.app.errorhandler(404)
        @self.app.errorhandler(405)
        @self.app.errorhandler(422)
        @self.app.errorhandler(500)
        @self.app.errorhandler(502)
        @self.app.errorhandler(504)
        @self.app.errorhandler(ProxyLoopDetected)
        def handle_error(error):
            """Return appropriate error response."""
            accept = request.headers.get('Accept', '*/*')

            response_data = {
                "status": "error",
                "code": error.code,
                "name": error.name,
                "description": error.description
            }

            # Determine if client expects JSON or HTML
            if accept == "*/*" or not accept.strip():
                # Typical script (curl, bot) - JSON
                return jsonify(response_data), error.code
            elif "application/json" in accept:
                # Explicit JSON accept - JSON
                return jsonify(response_data), error.code
            else:
                # Probably a browser - return simple HTML
                html_response = \
                    f"<html><head><title>{error.code} {error.name}</title></head><body><h1>{error.code} {error.name}</h1><p>{escape(error.description)}</p></body></html>"
                return make_response(html_response, error.code)

    @staticmethod
    def _contains_substitution(url):
        return "~~" in url

    def substitute(self, node: str, alias: str) -> str | None:
        try:
            parts = self.config.get("parts", {})

            def replace_part(match):
                value = parts[f"@{match.group(1)}"][node]
                if not isinstance(value, str):
                    raise ValueError("URL parts must be strings")
                return value

            uri_path = re.sub(r"~~(\w+)~~", replace_part, self.allowed_paths[alias])
            base = self.allowed_nodes[node].rstrip("/") + "/"
            full_url = normalize_url(urljoin(base, uri_path.lstrip("/")))
            if not SysPiper._contains_substitution(full_url):
                return full_url
        except (KeyError, TypeError, ValueError, re.error):
            self.logger.error("Failed to substitute URL for node %s, alias %s", node, alias)

        return None

    def get_node_headers(self, node: str) -> dict:
        headers = {}
        headers_cfg = self.config.get("headers", {})
        node_headers = headers_cfg.get(node, [])
        for tup in node_headers:
            if isinstance(tup, list) and len(tup) == 2:
                h, v = tup
                headers[h] = v

        return headers

    def setup_routes(self):
        """Define all API routes."""

        for name in ("interfaces", "system", "filesystems", "pressure", "apt"):
            collector = getattr(system_stats, name)

            def collect(node=None, collector=collector):
                return collector()

            view = self.proxyable(collect)
            self.app.add_url_rule(f"/{name}", endpoint=name, view_func=view,
                                  defaults={"node": None}, methods=["GET"])
            self.app.add_url_rule(f"/{name}/<node>", endpoint=name, view_func=view,
                                  methods=["GET"])

        @self.app.route("/public_ip", defaults={"node": None}, methods=["GET"])
        @self.app.route("/public_ip/<node>", methods=["GET"])
        @self.proxyable
        def fetch_ip(node):
            """Fetch public IP by requesting an external service."""
            try:
                headers = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}
                ip_text = self._fetch_upstream(self.myip_url, timeout=5, headers=headers,
                                               expect_json=False)

            except HTTPException:
                raise
            except Exception as e:
                self.logger.error("Failed to fetch IP (%s)", type(e).__name__)
                abort(502, description="Failed to reach external service")

            self.logger.info("Fetched IP service response, length=%s", len(ip_text))
            return jsonify({
                "status": "ok",
                "ip": ip_text
            })

        # CPU
        @self.app.route("/cpu", defaults={"node": None}, methods=["GET"])
        @self.app.route("/cpu/<node>", methods=["GET"])
        @self.proxyable
        def cpu(node):
            """Measure CPU usage and return as JSON."""
            cpu_percent = psutil.cpu_percent(interval=1.0)
            self.logger.debug(f"Measured CPU usage: {cpu_percent}%")
            return {
                "status": "ok",
                "percent": cpu_percent
            }

        # RAM
        @self.app.route("/ram", defaults={"node": None}, methods=["GET"])
        @self.app.route("/ram/<node>", methods=["GET"])
        @self.proxyable
        def ram(node):
            """Measure RAM usage and return as JSON."""
            mem = psutil.virtual_memory()
            self.logger.debug(f"Measured RAM usage: {mem.percent}%")
            return {
                "status": "ok",
                "total": mem.total,
                "available": mem.available,
                "percent": mem.percent,
                "used": mem.used,
                "free": mem.free
            }

        # Disk
        @self.app.route("/disk", defaults={"node": None}, methods=["GET"])
        @self.app.route("/disk/<node>", methods=["GET"])
        @self.proxyable
        def disk(node):
            """Measure disk usage and return as JSON."""
            disk = psutil.disk_usage('/')
            self.logger.debug(f"Measured disk usage: {disk.percent}%")
            return {
                "status": "ok",
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent
            }

        # Network
        @self.app.route("/net", defaults={"node": None}, methods=["GET"])
        @self.app.route("/net/<node>", methods=["GET"])
        @self.proxyable
        def net(node):
            """Measure network I/O counters and return as JSON."""
            net = psutil.net_io_counters()
            self.logger.debug(f"Measured network IO: sent={net.bytes_sent}, recv={net.bytes_recv}")
            return {
                "status": "ok",
                "sent": net.bytes_sent,
                "recv": net.bytes_recv
            }

        @self.app.route("/remote/<node>/<alias>", methods=["GET"])
        def remote_proxy(node, alias):
            """Proxy only explicitly allowed aliases to remote nodes."""
            if node not in self.allowed_nodes:
                abort(404, description="Node not allowed")

            if alias not in self.allowed_paths:
                abort(404, description="Alias not allowed for this node")

            try:
                full_url = self.substitute(node, alias)

                if not full_url or SysPiper._contains_substitution(full_url):
                    abort(502, description="substitution error: data missing or formatting error")

                self.logger.debug("Proxying remote request to node %s, alias %s", node, alias)

                verify = self.tls_verify.get(node, True)
                return jsonify(self._fetch_upstream(full_url, headers=self.get_node_headers(node),
                                                    timeout=3, verify=verify))

            except requests.exceptions.Timeout:
                self.logger.error("Gateway timeout to node %s", node)
                return jsonify({
                    "status": "error",
                    "code": 504,
                    "name": "Gateway Timeout",
                    "description": "Request timed out"}), 504

            except HTTPException:
                raise
            except requests.exceptions.HTTPError as error:
                status = error.response.status_code if error.response is not None else "unknown"
                self.logger.error("Gateway to node %s, alias %s returned HTTP %s", node, alias, status)
                abort(502, description=f"Target node returned HTTP {status}")
            except requests.exceptions.ConnectionError as error:
                detail = connection_error_detail(error)
                self.logger.error("Connection to node %s, alias %s failed: %s", node, alias, detail)
                abort(502, description=f"Connection to target node failed: {detail}")
            except Exception as e:
                self.logger.error("Gateway error to node %s (%s)", node, type(e).__name__)
                abort(502, description="Failed to fetch from target node")

        # we must keep the reference to the remote_proxy function to allow remote_via_dollars to work
        self.remote_proxy = remote_proxy

        @self.app.route("/<path:full>", methods=["GET"])
        def full_path(full: str):
            """Handler for flat /<alias>@<node> + optionally /other_syspiper """
            if '@' not in full:
                if self._script_path(full) is not None:
                    ret = self._run_sandbox(full)
                    if ret is not None:
                        return ret
                    else:
                        abort(422, description="Endpoint error")

                abort(400, description="Unknown script or invalid format, expected /<alias>@<node>")

            match = re.fullmatch(r"([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?", full)
            if len(full) > 256 or match is None:
                abort(400, description="Invalid format, expected /<alias>@<node>[/<next_hop>]")
            remote_alias, remote_node, next_hop = match.groups()
            if next_hop is None:
                for nd_glob, nxt in self.routes.items():
                    if glob_match(remote_node, nd_glob):
                        next_hop = nxt
                        break

            if next_hop is not None:
                return self.proxy_it(next_hop, f"/{remote_alias}@{remote_node}")
            else:

                if remote_node not in self.allowed_nodes:
                    abort(404, description="Unknown node")
                if remote_alias not in self.allowed_paths:
                    abort(404, description="Unknown alias")

                return self.remote_proxy(remote_node, remote_alias)

    def run(self, host="0.0.0.0", port=8080):
        """Start the Flask application."""
        self.listen_ip = host
        self.listen_port = port
        self.app.run(host=host, port=port)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="SysPiper App")
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to configuration JSON file (default: config.json)"
    )
    return parser.parse_args()

def prepare(custom_config_path: str = None) -> SysPiper:
    """Main entry point of the application."""
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    app = SysPiper(custom_config_path or config_path)
    return app

if __name__ == "__main__":
    args = parse_args()
    app = prepare(args.config or None)
    app.run()
