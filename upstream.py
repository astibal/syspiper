"""Bounded outbound HTTP in a killable process, including DNS and slow streams."""
import errno
import json
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import requests

MAX_UPSTREAM_BYTES = 1 << 20


class UpstreamRejected(Exception):
    def __init__(self, code, description):
        self.code = code
        self.description = description


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


def fetch(url, *, headers, timeout, verify=True, expect_json=True, propagate_loop=False):
    def reject_redirect(response, *args, **kwargs):
        if 300 <= response.status_code < 400:
            # Requests may consume a redirect body to prepare Response.next
            # even with allow_redirects=False. Reject it before that happens.
            response.close()
            raise UpstreamRejected(502, "Upstream redirects are not allowed")

    with requests.get(url, headers=headers, timeout=timeout, verify=verify,
                      allow_redirects=False, stream=True,
                      hooks={"response": reject_redirect}) as response:
        if propagate_loop and response.status_code == 508:
            raise UpstreamRejected(508, "Proxy hop limit exceeded")
        response.raise_for_status()
        try:
            content_length = int(response.headers.get("Content-Length", ""))
        except ValueError:
            content_length = None
        if content_length is not None and content_length > MAX_UPSTREAM_BYTES:
            raise UpstreamRejected(502, "Upstream response exceeds 1 MiB limit")

        body = bytearray()
        # iter_content decodes gzip/deflate, so the limit also covers the
        # expanded body and responses without a trustworthy Content-Length.
        for chunk in response.iter_content(chunk_size=64 << 10):
            if len(body) + len(chunk) > MAX_UPSTREAM_BYTES:
                raise UpstreamRejected(502, "Upstream response exceeds 1 MiB limit")
            body.extend(chunk)

        if expect_json:
            value = body.decode(response.encoding, errors="replace") if response.encoding else body
            return json.loads(value)
        return body.decode(response.encoding or "utf-8", errors="replace")


def fetch_result(options):
    """Return a JSON-safe result or a sanitized failure, never upstream secrets."""
    try:
        return {"value": fetch(**options)}
    except UpstreamRejected as error:
        return {"error": {"code": error.code, "description": error.description}}
    except requests.exceptions.Timeout:
        return {"error": {"code": 504, "description": "Request timed out", "kind": "timeout"}}
    except requests.exceptions.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        return {"error": {"code": 502, "description": f"Target node returned HTTP {status}"}}
    except requests.exceptions.ConnectionError as error:
        return {"error": {"code": 502, "description": "Connection to target node failed: " + connection_error_detail(error)}}
    except Exception:
        return {"error": {"code": 502, "description": "Failed to fetch from target node"}}


def fetch_with_deadline(options, deadline):
    """Bound startup, DNS, connect, headers, body and child-side JSON parsing.

    The helper has no input from request bodies and never executes supplied
    code. Credentials travel over stdin rather than process arguments. Killing
    and reaping it also closes its sockets, including under threaded WSGI.
    """
    started = time.monotonic()
    payload = json.dumps(options).encode('utf-8')
    with subprocess.Popen(
        [sys.executable, '-B', str(Path(__file__).resolve()), '--fetch'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            output, _ = process.communicate(payload, timeout=max(0, deadline - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {"error": {"code": 504, "description": "Upstream total time limit exceeded"}}
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if process.returncode:
            return {"error": {"code": 502, "description": "Upstream helper failed"}}
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or not ('value' in result or 'error' in result):
                raise ValueError
            if time.monotonic() - started >= deadline:
                return {"error": {"code": 504, "description": "Upstream total time limit exceeded"}}
            return result
        except (ValueError, UnicodeError):
            return {"error": {"code": 502, "description": "Invalid upstream helper response"}}


if __name__ == '__main__':
    if sys.argv[1:] != ['--fetch']:
        sys.exit('This module is a SysPiper HTTP helper, not a standalone command.')
    try:
        options = json.load(sys.stdin)
        result = fetch_result(options)
        print(json.dumps(result), flush=True)
    except Exception:
        sys.exit(1)
