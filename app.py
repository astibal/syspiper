import json
import sys
import os
import logging
import requests
import argparse
import psutil
from urllib.parse import urlparse, urljoin
from flask import Flask, request, jsonify, abort

"""
SIBuddy - system info JSON gateway with proxy support and loop protection

This lightweight GET-only Flask app serves system info as JSON.
It operates in read-only mode, does not modify the system,
and accepts no parameters except fixed endpoints.

Supports proxying to allowed nodes, with automatic loop protection
when requests come from localhost addresses.

# Run:
python app.py --config /etc/stats-proxy/prod-config.json

# Config example:
{
  "api_key": "<some_secret_phrase>",
  "log_level": "INFO",
  "myip_url": "https://myip.dk",
  "allowed_nodes": {
    "node1": "http://192.168.0.101:8080",
    "node2": "https://server02.example.com",
    "localhost": "http://127.0.0.1:8080"
  }
  "allowed_paths": {
    "public_ip": "/public_ip",
    "cpu": "/cpu",
    "ram": "/ram",
    "disk": "/disk",
    "net": "/net"
  }
}

# Test local:
curl -X GET http://localhost:8080/cpu \
  -H "X-API-Key: tajnyklic123"

# Test proxy:
curl -X GET http://localhost:8080/cpu/node1 \
  -H "X-API-Key: tajnyklic123"
"""

class SysPiper:
    """Main application class for SIBuddy."""

    def __init__(self, config_path):
        """Initialize the app with configuration."""
        self.config = self.load_config(config_path)
        self.api_key = self.config.get("api_key", "default_key")
        self.log_level = self.config.get("log_level", "INFO").upper()
        self.myip_url = self.config.get("myip_url", "https://myip.dk")
        self.allowed_nodes = self.config.get("allowed_nodes", {})
        self.allowed_paths = self.config.get("allowed_paths", {})

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

    def check_auth(self):
        """Verify the API key in the request headers."""
        if request.headers.get("X-API-Key") != self.api_key:
            abort(401, description="Unauthorized")

    def proxyable(self, func):
        """Decorator to proxy the request if 'node' parameter is present."""

        def wrapper(*args, **kwargs):
            node = kwargs.get('node')
            if node:
                # Protect against proxy loops from localhost requests
                if request.remote_addr in ("127.0.0.1", "::1") or "localhost" in request.host:
                    self.logger.warning(f"Detected localhost request, bypassing proxy for safety")

                    if node in self.allowed_nodes:
                        result = func(*args, **kwargs)
                        return jsonify(result)
                    else:
                        abort(403, description="Access to localhost is not allowed")


                target_url = self.allowed_nodes.get(node)
                if not target_url:
                    abort(404, description="Node not allowed")

                try:
                    headers = {"X-API-Key": self.api_key}
                    endpoint = request.path.replace(f"/{node}", "")  # remove /<node> part
                    full_url = f"{target_url}{endpoint}"
                    self.logger.debug(f"Proxying to {full_url}")
                    proxy_response = requests.get(full_url, headers=headers, timeout=5)
                    proxy_response.raise_for_status()
                except Exception as e:
                    self.logger.error(f"Proxy to node {node} failed: {e}")
                    abort(502, description=f"Failed to fetch from target node: {str(e)}")

                return jsonify(proxy_response.json())
            else:
                # Local call
                result = func(*args, **kwargs)
                return result

        wrapper.__name__ = func.__name__
        return wrapper

    def register_error_handlers(self):
        """Register global JSON error handlers."""

        @self.app.errorhandler(400)
        @self.app.errorhandler(401)
        @self.app.errorhandler(403)
        @self.app.errorhandler(404)
        @self.app.errorhandler(405)
        @self.app.errorhandler(500)
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
                "cpu_percent": cpu_percent
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
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv
            }

        @self.app.route("/remote/<node>/<alias>", methods=["GET"])
        def remote_proxy(node, alias):
            """Proxy only explicitly allowed aliases to remote nodes."""
            self.check_auth()

            if node not in self.allowed_nodes.keys():
                abort(404, description="Node not allowed")

            if alias not in self.allowed_paths.keys():
                abort(404, description="Alias not allowed for this node")

            try:
                headers = {"X-API-Key": self.api_key}
                full_url = urljoin(self.allowed_nodes[node], self.allowed_paths[alias])
                self.logger.debug(f"Proxying remote request to {full_url}")

                proxy_response = requests.get(full_url, headers=headers, timeout=5)
                proxy_response.raise_for_status()
            except Exception as e:
                self.logger.error(f"Failed to proxy remote request to {node}: {e}")
                abort(502, description=f"Failed to fetch from target node: {str(e)}")

            return jsonify(proxy_response.json())

    def run(self, host="0.0.0.0", port=8080):
        """Start the Flask application."""
        self.app.run(host=host, port=port)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="SIBuddy App")
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to configuration JSON file (default: config.json)"
    )
    return parser.parse_args()

def main():
    """Main entry point of the application."""
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    args = parse_args()
    app = SysPiper(config_path=(config_path or args.config))
    app.run()

if __name__ == "__main__":
    main()
