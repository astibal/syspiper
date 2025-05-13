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
from functools import partial
from fnmatch import fnmatchcase as glob_match

import io
import importlib.util
from contextlib import redirect_stdout, redirect_stderr
from sexec import safe_exec

from urllib.parse import urlparse, urljoin, urlunparse
from flask import Flask, request, jsonify, abort, make_response

from filters import *

from typing import List


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

    def __init__(self, config_path):
        """Initialize the app with configuration."""
        self.config = self.load_config(config_path)
        self.api_key = self.config.get("api_key", "default_key")
        self.log_level = self.config.get("log_level", "INFO").upper()
        self.myip_url = self.config.get("myip_url", "https://myip.dk")
        self._allowed_ips = self.config.get("allowed_ips", None)
        self.allowed_nodes = self.config.get("allowed_nodes", {})
        self.allowed_paths = self.config.get("allowed_paths", {})
        self.tls_verify = self.config.get("tls_verify", {})
        self.routes = self.config.get("routes", {}) # remote node -> next_hop (wildcard supported)

        self.allowed_ips: List[paddress.IPv4Address | ipaddress.IPv6Address] = []
        self.allowed_networks: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        self.remote_proxy = None

        if not self._allowed_ips:
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

        api_key = request.headers.get("X-API-Key")
        api_key = brutal_filter(api_key)[:256]
        self.logger.debug(
            f"Received API key: {api_key}, expected: {self.api_key}"
        )
        if api_key != self.api_key:
            abort(401, description="Unauthorized")

    def proxy_it(self, node):
        """Proxy the request to the remote node."""
        rq_path = request.path
        target_url = self.allowed_nodes.get(node)
        if not target_url:
            abort(404, description="Node not allowed")

        try:
            headers = {"X-API-Key": self.api_key}

            endpoint = rq_path.replace(f"/{node}", "")  # remove /<node> part
            full_url = f"{target_url}{endpoint}"

            # Note: this may seem nice to add at first glance, but proxyable are intended to be proxied
            #       to next-hop SysPiper, templating is not really desirable.
            #       This may change, but I find it hard to imagine how that is useful.

            # full_url = self.substitute(node, endpoint)

            # Let's keep the sanity/misconfig check here. No escapes allowed!
            if not full_url or SysPiper._contains_substitution(full_url):
                abort(502, description="substitution error: data missing or formatting error")

            self.logger.debug(f"Proxying to {full_url}")
            proxy_response = requests.get(full_url, headers=headers, timeout=5)
            proxy_response.raise_for_status()

        except requests.exceptions.Timeout as e:
            self.logger.error(f"Timeout proxying to {node}: {e}")
            return jsonify({
                "status": "error",
                "code": 504,
                "name": "Gateway Timeout",
                "description": "Request timed out"}), 504

        except Exception as e:
            self.logger.error(f"Proxy to node {node} failed: {e}")
            abort(502, description=f"Failed to fetch from target node: {str(e)}")

        return jsonify(proxy_response.json())

    def proxyable(self, func):
        """Decorator to proxy the request if 'node' parameter is present."""

        def wrapper(*args, **kwargs):
            node = kwargs.get('node')
            if node:
                return self.proxy_it(node)
            else:
                # Local call
                result = func(*args, **kwargs)
                return result

        wrapper.__name__ = func.__name__
        return wrapper

    def _run_sandbox(self, name):
        name = brutal_filter(name)[:256].lower()
        script_path = os.path.join("scripts", f"{name}.py")

        result = safe_exec(name, script_path)
        if result is None:
            self.logger.error(f"Script '{name}' failed without response")
            return None

        for key in ("error", "result"):
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
        @self.app.errorhandler(500)
        @self.app.errorhandler(502)
        @self.app.errorhandler(504)
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
                    f"<html><head><title>{error.code} {error.name}</title></head><body><h1>{error.code} {error.name}</h1><p>{error.description}</p></body></html>"
                return make_response(html_response, error.code)

    @staticmethod
    def _contains_substitution(url):
        return "~~" in url

    def substitute(self, node: str, alias: str) -> str | None:

        uri_path = self.allowed_paths[alias].lstrip("/")
        base = self.allowed_nodes[node].rstrip("/")

        full_url = urljoin(base, uri_path)

        try:
            if SysPiper._contains_substitution(full_url):
                match = re.search(r'~~(\w+)~~', uri_path)
                if not match:
                    return None

                part = match.group(1)
                parts = self.config.get("parts", {})

                if part and f"@{part}" in parts:
                    node_value = parts.get(f"@{part}",{})[node]
                    uri_path = full_url.replace(f"~~{part}~~", node_value)
                    full_url = normalize_url(urljoin(self.allowed_nodes[node], uri_path))
                else:
                    return None

            return full_url

        except (KeyError, IndexError, ValueError, re.error) as e:
            self.logger.error(f"Failed to parse and substitute parts in URL: {e}")

        return None

    def get_node_headers(self, node: str) -> dict:
        headers = {}
        headers_cfg = self.config.get("headers", {})
        node_headers = headers_cfg.get(node, [])
        for tup in node_headers:
            if isinstance(tup, list) and len(tup) == 2:
                h, v = tup
                self.logger.debug(f"Adding header '{h}: {v}' to request")
                headers[h] = v

        return headers

    def setup_routes(self):
        """Define all API routes."""

        @self.app.route("/public_ip", defaults={"node": None}, methods=["GET"])
        @self.app.route("/public_ip/<node>", methods=["GET"])
        @self.proxyable
        def fetch_ip(node):
            """Fetch public IP by requesting an external service."""
            self.check_auth()
            try:
                headers = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}
                response = requests.get(self.myip_url, timeout=5, headers=headers)

            except Exception as e:
                self.logger.error(f"Failed to fetch IP: {e}")
                abort(502, description=f"Failed to reach external service: {str(e)}")

            self.logger.info(f"Fetched IP service response, length={len(response.text)}")
            return jsonify({
                "status": "ok",
                "ip": response.text
            })

        # CPU
        @self.app.route("/cpu", defaults={"node": None}, methods=["GET"])
        @self.app.route("/cpu/<node>", methods=["GET"])
        @self.proxyable
        def cpu(node):
            """Measure CPU usage and return as JSON."""
            self.check_auth()
            cpu_percent = psutil.cpu_percent(interval=0.5)
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
            self.check_auth()
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
            self.check_auth()
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
            self.check_auth()
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
            self.check_auth()

            if node not in self.allowed_nodes:
                abort(404, description="Node not allowed")

            if alias not in self.allowed_paths:
                abort(404, description="Alias not allowed for this node")

            try:
                full_url = self.substitute(node, alias)

                if not full_url or SysPiper._contains_substitution(full_url):
                    abort(502, description="substitution error: data missing or formatting error")

                self.logger.debug(f"Proxying remote request to {full_url}")

                verify = self.tls_verify.get(node, True)
                proxy_response = requests.get(full_url, headers=self.get_node_headers(node), timeout=3, verify=verify)
                proxy_response.raise_for_status()

            except requests.exceptions.Timeout as e:
                self.logger.error(f"Gateway timeout to {node}: {e}")
                return jsonify({
                    "status": "error",
                    "code": 504,
                    "name": "Gateway Timeout",
                    "description": "Request timed out"}), 504

            except Exception as e:
                self.logger.error(f"General gateway error to {node}: {e}")
                abort(502, description=f"Failed to fetch from target node: {str(e)}")

            return jsonify(proxy_response.json())
        # we must keep the reference to the remote_proxy function to allow remote_via_dollars to work
        self.remote_proxy = remote_proxy

        @self.app.route("/<path:full>", methods=["GET"])
        def full_path(full: str):
            """Handler for flat /<alias>@<node> + optionally /other_syspiper """
            self.check_auth()

            full = brutal_filter(full, additional_chars="/")[:256]
            url_parts = urlparse(full)
            full = url_parts.path

            if not '@' in full or not full.count("$") != 1 or not full.count('/') <= 1:

                # script pre-flight check
                if os.path.isfile(os.path.join("scripts", f"{full}.py")):
                    #ret = self._run_script(full)
                    ret = self._run_sandbox(full)
                    if ret is not None:
                        return ret
                    else:
                        abort(422, description="Endpoint error")

                abort(400, description="Invalid format, expected /<node>@<alias>")


            remote_alias, remote_node = full.split("@", 1)
            next_hop = None
            if '/' in remote_node:
                remote_node, next_hop = remote_node.split("/", 1)
            else:
                for nd_glob, nxt in self.routes.items():
                    if glob_match(remote_node, nd_glob):
                        next_hop = nxt
                        break

            if next_hop is not None:
                return self.proxy_it(next_hop)
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
