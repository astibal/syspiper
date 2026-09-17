# Configuration and routing

[Back to README](../README.md)

Configuration is a JSON object loaded at startup. Restart workers after edits.
Relative configuration and script paths resolve from the process working
directory; run from the repository root.

## Configuration selection

| Entry point | Configuration source |
| --- | --- |
| `python app.py` | `--config`, default `config.json` |
| `wsgi:app` / Gunicorn | `CONFIG_PATH`, default `config.json` |
| `prepare(path)` | Explicit nonempty path, then `CONFIG_PATH`, then `config.json` |

The CLI's default `--config config.json` overrides `CONFIG_PATH`, even when you
do not supply the flag. Neither the CLI nor configuration exposes bind options;
the development server and supplied Gunicorn launchers all default to port 8181.

## Keys

See [config_example.json](../config_example.json) for valid JSON.

| Key | Default | Meaning |
| --- | --- | --- |
| `api_key` | Required | Nonempty HTTP-header-compatible string, at most 256 characters; matched exactly without normalization |
| `log_level` | `INFO` | Python logging level, converted to uppercase |
| `myip_url` | `https://myip.dk` | Text service used by `/public_ip`; the old example key `myip.url` is rejected |
| `allowed_ips` | `["0.0.0.0/0"]` | IP addresses or CIDRs; `[]` denies everyone; missing or null allows all IPv4, not IPv6 |
| `allowed_nodes` | `{}` | Node name to upstream base URL |
| `allowed_paths` | `{}` | Alias to configured upstream path/template |
| `parts` | `{}` | Template replacement maps, indexed by `@part` and node name |
| `headers` | `{}` | Node name to arrays of `[header_name, value]`, for direct alias requests only |
| `tls_verify` | `{}` | Node name to TLS verification boolean, for direct alias requests only; defaults to true |
| `upstream_deadline_seconds` | `15` | Total elapsed-time budget per outbound HTTP operation, including helper startup, DNS, headers and body; a finite number greater than 0 and at most 300 |
| `routes` | `{}` | Target-name glob to next-hop node; first matching entry wins |

Startup rejects unknown options, duplicate JSON keys, wrong types, invalid IPs,
invalid logging levels and malformed headers. Errors identify the option category
without printing credential values. Node references in `headers`, `tls_verify`,
`parts` and route next hops must exist in `allowed_nodes`. Route target patterns
can describe nodes known only to the next hop.

Node and alias names contain 1–128 ASCII letters, digits, underscores, dots or
hyphens; `.` and `..` are rejected. Upstream URLs must use HTTP(S), have a host
and valid port, and contain no embedded credentials, fragments, whitespace or
template markers. Node base URLs must not have queries; use `allowed_paths` for
query strings. Alias paths must be relative to the configured node, not absolute
URLs. Header values and the API key must fit HTTP's Latin-1 encoding and cannot
contain leading whitespace or invalid control characters.

Template definitions must be string maps with known nodes. A referenced template
part must exist, but its node map may intentionally cover only some nodes. Missing
values for a particular node remain an HTTP 502 when that alias is requested.
A template that resolves to an absolute URL is rejected at startup.

IP checks use Flask's `request.remote_addr`. Forwarded address headers are not
interpreted by this application. Behind a reverse proxy the allowlist normally
checks that proxy's address. IPv6 access needs an explicit IPv6 address or CIDR.

## Three request forms

The examples below use the names in the supplied configuration. Replace the
placeholder hosts and secrets before making upstream requests.

```sh
# Built-in collector on another SysPiper:
curl -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8181/cpu/node8181

# Configured third-party API alias (equivalent direct forms):
curl -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8181/remote/sx1/sx_status
curl -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8181/sx_status@sx1

# Resolve the alias on a different SysPiper:
curl -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8181/sx_status@sx1/node8181
```

`/<builtin>/<node>` removes only the final node segment and forwards the
remaining path. It sends the local configured `api_key` as `X-API-Key`, so the
receiving SysPiper must accept that same key and the forwarding host's source IP.
Per-node `headers` and `tls_verify` do not apply to this forwarding operation;
TLS verification stays enabled.

`/remote/<node>/<alias>` and `/<alias>@<node>` perform a direct alias lookup.
They send only configured node headers (plus Requests defaults), not the
incoming client's key. Aliases are global: every alias can be used with every
configured node when its substitutions resolve. There is no per-node alias ACL.

`/<alias>@<target>/<next_hop>` forwards `/<alias>@<target>` to the next hop.
That hop must know the target and alias, or route them onward. The entry node
only needs the next-hop URL. Nested script endpoints have no generic
`/<node>` forwarding suffix.

## Templates and URL construction

For the sample configuration, `sx_status@sx1` combines:

```text
allowed_nodes.sx1                 https://sx1u:55555
allowed_paths.sx_status           /api/status/~~replace_ping~~
parts.@replace_ping.sx1           ping
result                           https://sx1u:55555/api/status/ping
```

Every `~~name~~` requires a string at `parts["@name"][node]`. Malformed templates
and invalid types fail startup; a missing value for a specific node produces
HTTP 502 without fetching.
Parts are inserted verbatim, not URL-encoded. Paths are joined under the node's
base path and dot segments are normalized. Configuration must be trusted;
encode whitespace and special URL characters in path parts as needed.
Caller query parameters and incoming headers are not forwarded automatically.
A query string explicitly embedded in a configured URL is retained.

## Next-hop routing and loop protection

```json
{
  "routes": {
    "target-*": "hop",
    "*": "fallback"
  }
}
```

This fragment requires corresponding `hop` and `fallback` entries in
`allowed_nodes`. Routing applies only to the flat `alias@target` form without
an explicit next hop. Matching is case-sensitive and follows JSON entry order.
The explicit next hop takes precedence; `/remote/...` bypasses `routes`.

Flat alias, target and next-hop names accept ASCII letters, digits, `_`, `.`,
and `-`; the complete captured path is limited to 256 characters.

Each forwarding operation increments `X-SysPiper-Hops` (default 0). Incoming
values must contain one or two decimal digits. A value of 8 or higher refuses
another forward with HTTP 508. Direct collectors and direct alias requests do
not validate this header. Every SysPiper in a chain must preserve the limit;
an upstream 508 is propagated by next-hop forwarding.
