import json
import sys
import os
import logging
import requests
import argparse
import psutil
from flask import Flask, request, jsonify, abort

"""
SIBuddy - system info json gateway

This is lightweight GET-only Flask app that serves system info as JSON.
It operates in read-only mode, it changes nothing on the system,
and doesn't accept any parameters, with the intent to be safe
running as root user.

# Run:


`python app.py --config /etc/stats-proxy/prod-config.json`


# Config:
```json
{
  "api_key": "<some_secret_phrase>",
  "log_level": "INFO",
  "myip.url": "https://myip.dk"
}
```

# Test:
`curl -X GET http://localhost:8080/public_ip \
  -H "X-API-Key: secret_key_example"`
  
"""

class SIBuddy:
    """Main application class for Stats Proxy."""

    def __init__(self, config_path):
        """Initialize the app with configuration."""
        self.config = self.load_config(config_path)
        self.api_key = self.config.get("api_key", "default_key")
        self.log_level = self.config.get("log_level", "INFO").upper()
        self.myip_url = self.config.get("myip_url", "https://myip.dk")

        # Setup logging
        logging.basicConfig(level=self.log_level)
        self.logger = logging.getLogger(__name__)

        # Setup Flask app
        self.app = Flask(__name__)
        self.setup_routes()

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

    def setup_routes(self):
        """Define all API routes."""
        
        @self.app.route("/public_ip", methods=["GET"])
        def fetch_ip():
            """Fetch public IP by requesting an external service."""
            self.check_auth()

            try:
                headers = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}
                response = requests.get(self.myip_url, timeout=5, headers=headers)
                response.raise_for_status()
            except Exception as e:
                self.logger.error(f"Failed to fetch IP: {e}")
                abort(502, description=f"Failed to reach external service: {str(e)}")

            self.logger.info(f"Fetched IP service response, length={len(response.text)}")
            return jsonify({
                "status": "success",
                "response": response.text
            })

        @self.app.route("/cpu", methods=["GET"])
        def cpu():
            """Measure CPU usage and return as JSON."""
            self.check_auth()
            cpu_percent = psutil.cpu_percent(interval=0.5)
            self.logger.debug(f"Measured CPU usage: {cpu_percent}%")
            return jsonify({
                "cpu_percent": cpu_percent
            })

        @self.app.route("/ram", methods=["GET"])
        def ram():
            """Measure RAM usage and return as JSON."""
            self.check_auth()
            mem = psutil.virtual_memory()
            self.logger.debug(f"Measured RAM usage: {mem.percent}%")
            return jsonify({
                "total": mem.total,
                "available": mem.available,
                "percent": mem.percent,
                "used": mem.used,
                "free": mem.free
            })

        @self.app.route("/disk", methods=["GET"])
        def disk():
            """Measure disk usage and return as JSON."""
            self.check_auth()
            disk = psutil.disk_usage('/')
            self.logger.debug(f"Measured disk usage: {disk.percent}%")
            return jsonify({
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent
            })

        @self.app.route("/net", methods=["GET"])
        def net():
            """Measure network I/O counters and return as JSON."""
            self.check_auth()
            net = psutil.net_io_counters()
            self.logger.debug(f"Measured network IO: sent={net.bytes_sent}, recv={net.bytes_recv}")
            return jsonify({
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv
            })

    def run(self, host="0.0.0.0", port=8080):
        """Start the Flask application."""
        self.app.run(host=host, port=port)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Stats Proxy App")
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
    app = SIBuddy(config_path=(config_path or args.config))
    app.run()

if __name__ == "__main__":
    main()

