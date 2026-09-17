"""Validate administrator configuration without disclosing credential values."""
import ipaddress
import math
import re
from urllib.parse import urlsplit


NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
PART = re.compile(r"~~(\w+)~~")
KEYS = {
    "api_key", "log_level", "myip_url", "allowed_ips", "allowed_nodes",
    "allowed_paths", "parts", "headers", "tls_verify", "routes",
    "upstream_deadline_seconds",
}


def fail(field, message):
    raise ValueError(f"Invalid configuration: {field} {message}") from None


def http_url(value, field, *, base=False):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        fail(field, "must be an absolute HTTP(S) URL without whitespace")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and "#" not in value and "~~" not in value and "\\" not in value)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            valid = False
        if base and (parsed.query or '?' in value):
            valid = False
    except ValueError:
        valid = False
    if not valid:
        fail(field, "must be an absolute HTTP(S) URL without credentials, fragments or templates"
             + ("; node base URLs cannot contain queries" if base else ""))


def header_value(value, field):
    if value[:1].isspace() or any((ord(c) < 32 and c != '\t') or ord(c) == 127 for c in value):
        fail(field, "must not contain leading whitespace or HTTP control characters")
    try:
        value.encode('latin-1')
    except UnicodeEncodeError:
        fail(field, "must be encodable as Latin-1 for HTTP headers")


def relative_path(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        fail("allowed_paths", "contains a malformed URL path")
    if (parsed.scheme or parsed.netloc or '#' in value or '\\' in value
            or value.startswith('//') or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        fail("allowed_paths", "must contain relative URL paths without hosts, fragments or whitespace")


def validate_config(config):
    if not isinstance(config, dict):
        fail("root", "must be a JSON object")
    if set(config) - KEYS:
        # Do not echo arbitrary property names, which may contain credentials.
        fail("keys", "contains unknown options; see docs/configuration.md")
    key = config.get("api_key")
    if not isinstance(key, str) or not 0 < len(key) <= 256:
        fail("api_key", "must be a non-empty string of at most 256 characters")
    header_value(key, "api_key")
    level = config.get("log_level", "INFO")
    if not isinstance(level, str) or level.upper() not in {"CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET"}:
        fail("log_level", "must name a standard logging level")
    deadline = config.get("upstream_deadline_seconds", 15)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not 0 < deadline <= 300 or not math.isfinite(deadline):
        fail("upstream_deadline_seconds", "must be a finite number greater than 0 and at most 300")
    http_url(config.get("myip_url", "https://myip.dk"), "myip_url")
    ips = config.get("allowed_ips")
    if ips is not None:
        if not isinstance(ips, list):
            fail("allowed_ips", "must be an array or null")
        for index, entry in enumerate(ips):
            try:
                if not isinstance(entry, str):
                    raise ValueError
                ipaddress.ip_network(entry, strict=False) if '/' in entry else ipaddress.ip_address(entry)
            except ValueError:
                fail(f"allowed_ips[{index}]", "must be an IP address or CIDR")
    for field in ("allowed_nodes", "allowed_paths", "parts", "headers", "tls_verify", "routes"):
        if not isinstance(config.get(field, {}), dict):
            fail(field, "must be an object")
    nodes = config.get("allowed_nodes", {})
    paths = config.get("allowed_paths", {})
    parts = config.get("parts", {})
    for field, mapping in (("allowed_nodes", nodes), ("allowed_paths", paths)):
        for name in mapping:
            if not NAME.fullmatch(name) or name in ('.', '..'):
                fail(field, "names must contain 1–128 ASCII letters, digits, underscores, dots or hyphens")
    for value in nodes.values():
        http_url(value, "allowed_nodes URL", base=True)
    for name, mapping in parts.items():
        if not re.fullmatch(r"@\w+", name) or not isinstance(mapping, dict):
            fail("parts", "must map @name to an object of node-specific strings")
        for node, value in mapping.items():
            if node not in nodes:
                fail("parts", "references an unknown node")
            if not isinstance(value, str) or '~~' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
                fail("parts", "values must be strings without template markers or control characters")
    for value in paths.values():
        if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            fail("allowed_paths", "values must be non-empty paths without whitespace")
        placeholders = PART.findall(value)
        if '~~' in PART.sub('part', value):
            fail("allowed_paths", "contains a malformed template")
        relative_path(PART.sub('part', value))
        if any('@' + name not in parts for name in placeholders):
            fail("allowed_paths", "references an undefined template part")
        for node in nodes:
            if all(node in parts['@' + name] for name in placeholders):
                relative_path(PART.sub(lambda match: parts['@' + match.group(1)][node], value))
        # Parts can deliberately be restricted to a subset of nodes. Do not
        # require an alias to resolve on every node in this global alias map.
    for node, pairs in config.get("headers", {}).items():
        if node not in nodes or not isinstance(pairs, list):
            fail("headers", "must map known nodes to arrays of [name, value] pairs")
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(v, str) for v in pair):
                fail("headers", "entries must be pairs of strings")
            name, value = pair
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                fail("headers", "contains an invalid HTTP header name")
            header_value(value, "headers value")
    for node, value in config.get("tls_verify", {}).items():
        if node not in nodes or not isinstance(value, bool):
            fail("tls_verify", "must map known nodes to booleans")
    for pattern, hop in config.get("routes", {}).items():
        if not pattern or not re.fullmatch(r"[A-Za-z0-9_.?*\[\]!-]+", pattern):
            fail("routes", "keys must be node-name glob patterns")
        if not isinstance(hop, str) or hop not in nodes:
            fail("routes", "next hops must reference known nodes")
    return config


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            fail("JSON", "contains duplicate object keys")
        result[name] = value
    return result
