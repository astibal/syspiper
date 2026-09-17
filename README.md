# SysPiper - system info JSON gateway with proxy support and loop protection

This lightweight GET-only Flask app serves system info as JSON.
Built-in endpoints read system information. Optional administrator-installed
Python scripts run under the service account with resource and time limits.
The API accepts fixed endpoints and parseable elements from the URL.

Key features:

- uses HTTP header for request authorization

- Queries local system or obtains json responses from 3rd-party APIs

- Everything is static or statically templated. There is no query run if not configured by you.

- Built-in endpoints are stateless; script execution uses temporary directories

- Supports proxying to next-hop syspiper nodes, including JSON queries

# Install
```shell
# Example systemd installation (administration commands require root).
# For local development, clone into your own directory and skip service setup.
adduser --system --group --home /opt/syspiper syspiper
cd /opt/syspiper

# clone to this very directory
git clone ssh://git@github.com/astibal/syspiper self
cd self

# create virtual environment
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# sample config file - edit to your liking
cp config_example.json config.json

# Allow the service account to read its configuration.
chown root:syspiper config.json
chmod 640 config.json

# make link to systemd unit and enable/customize service
ln -s /opt/syspiper/self/systemd/syspiper.service /etc/systemd/system/syspiper.service
systemctl enable syspiper

# optionally (e.g. for vrf support)
systemctl edit syspiper

# finally, run the service
systemctl start syspiper

# AppArmor remains optional and experimental: compilation is checked, but
# the profile has not been verified against a live Gunicorn deployment.
# Adjust tunables/syspiper to match your installation before trying it.

# cp apparmor.d/syspiper /etc/apparmor.d/
# cp apparmor.d/tunables/syspiper /etc/apparmor.d/tunables/
```


# Run 

## Quick test with python server:
`python app.py`

## Test with gunicorn from shell
This will launch gunicorn on foreground to see if it works
`./run.here.sh`

## Production
... you should indeed use systemd to start `syspiper` service.

The supplied unit requires the `syspiper` user and group and grants no Linux
capabilities by default. Existing installations must create that account and
ensure it can read the application, virtualenv, scripts and configuration.
Privileged VRF setups need an explicit override; the default unit listens on
the unprivileged port 8181.

## Upstream HTTP safety

All outbound HTTP reads (next-hop proxies, configured aliases and `/public_ip`)
reject HTTP 3xx responses with HTTP 502. Redirects are never followed, including
same-host redirects: configure the final URL directly. This prevents API keys
and other configured headers from being forwarded to a redirect destination.
The redirect body is rejected before Requests can read it internally.

Response bodies are streamed in 64 KiB chunks and limited to **1 MiB after
decompression**, before JSON parsing. Oversized Content-Length values are rejected
before reading; missing or invalid Content-Length does not bypass the byte limit.
Oversized responses return HTTP 502, and connections are closed on success and
failure. The limit is `SysPiper.MAX_UPSTREAM_BYTES` in `app.py`.

Update dependencies when deploying this change: `python -m pip install -r requirements.txt`.
The minimum `urllib3>=2.7.0` includes fixes for bounded streaming decompression
([upstream advisory](https://github.com/urllib3/urllib3/security/advisories/GHSA-mf9v-mfxr-j63j)).
The existing Requests timeouts remain connection/read timeouts, not a strict
deadline for the entire download. The byte limit is not a total process RAM limit.

## Scripts

Only install scripts you trust. The runner limits resource use, but scripts
retain the service user's filesystem and network access. It is not an isolation
boundary for third-party Python code. Script execution as root is refused.

`scripts/examples/date.py` is available as `/examples/date`, with the same API
key and IP checks as other endpoints. Script names are case-sensitive; traversal
and symlinks pointing outside `scripts/` are rejected. Each script must provide
`main()` returning a dictionary or a string. Helper imports use a `lib/` directory
beside the script, also available as `lib/` in the temporary working directory.

Each invocation starts a fresh interpreter. A five-second deadline covers both
imports and `main()`, and the parent terminates the process group on timeout.
CPU time is limited to five seconds and address space to 128 MiB. stdout/stderr
are captured separately from the result, drained without blocking the script,
and logged up to 64 KiB. JSON results are limited to 1 MiB. Script errors return
HTTP 422 with a JSON error body for API clients.

# Config example:


> `api_key` is required: a non-empty string of at most 256 characters.
> The supplied key must match exactly; input is not normalized.
```json
{
  "api_key": "secret123",
  "log_level": "DEBUG",
  "myip.url": "https://myip.dk",
  "allowed_ips": ["0.0.0.0/0"],
  "allowed_nodes": {
    "node8080": "http://192.168.1.123:8080",
    "sx1": "https://sx1u:55555"
  },
  "allowed_paths": {
    "sx_status": "/api/status/~~replace_ping~~",
    "sx_webhook": "/webhook/~~keys~~/info"
  },
  "parts": {
    "@keys": {
      "sx1": "sx1-webhook-secret"
    },
    "@replace_ping": {
      "sx1": "ping"
    }
  },
  "headers": {
    "sx1": [ [ "X-Api-Key", "verysecret"] ]
  },
  "tls_verify":{
    "sx1": false
  }
}
```

## Config documentation
- `allowed_ips`
  access list is present, but empty, system cannot be accessed.  
  On the contrary, if the list is _not_ present, system defaults to open access.

- `allowed nodes` is list of other syspipers, or 3rd-party API base URLs

- `allowed paths` is a map for `alias` -> `real_url` making syspiper requests
  easy to use and remember. E.g.: `my_value` can be mapped to `/some/api/path/value`

- `parts` consists of replacement strings maps for `real_url` substitution.
  E.g. `/some/api/~~version~~/value` in `allowed_paths` looks for `@version` key
  in `parts` and replaces `~~version~~` string with `parts`->`@version`->`<allowed_node>` value.  

- `headers` is a map of headers used by specific nodes.

- `tls_verify` is a map of bool values indicating TLS verification. Default is indeed `true`, you can override it at your own risk.  

- `routes` maps a remote node name or wildcard to a next-hop Syspiper node.
  For example, `{"target-*": "hop"}` forwards `/status@target-one` via `hop`.
  Each Syspiper proxy increments `X-SysPiper-Hops`; attempts to forward beyond
  eight hops return HTTP 508. Deploy the updated version on every node in a
  proxy chain so the limit is preserved end to end.
  
  

# Testing and obtaining JSON responses from Syspiper

## Test local:
`curl http://localhost:8181/cpu -H "X-API-Key: secret123"`

> You -> syspiper -> run cpu code to obtain result, respond with JSON

This is query request to local resource target URL server. In this case,
it's 'localhost'

## Test proxy:
`curl http://localhost:8181/cpu/node8080  -H "X-API-Key: secret123"`

> You -> syspiper(localhost) -> syspiper(node8080) -> run code cpu  to obtain result, respond with JSON

This is query to proxy request to another syspiper. System on 'localhost'
knows where 'nodex' is and proxies the request to:
`http(s)://where_nodex_is:port/cpu`

## Test remote:
`curl http://localhost:8181/remote/sx1/status  -H "X-API-Key: secret123"` 

> You -> syspiper(localhost) -> 3rd_json_api

Remote query is a term used for json queries from syspiper to 3rd party
API.

The same can be achieved by simpler:
`curl http://localhost:8181/status@sx1  -H "X-API-Key: secret123"`


## Test proxy remote request
`curl http://localhost:8181/status@sx1/node8080  -H "X-API-Key: secret123"`

> You -> syspiper(localhost) -> syspiper(node8080) -> 3rd_json_api

## Regression tests

With the dependencies installed, run as an unprivileged user from the repository:

```sh
python -m unittest discover -s tests -v
```

Tests use temporary configurations and scripts, with HTTP upstreams mocked.
