# SysPiper - system info JSON gateway with proxy support and loop protection

This lightweight GET-only Flask app serves system info as JSON.  
It operates in read-only mode, does not modify the system,
and accepts no parameters except fixed endpoints and parseable
elements from URL.  

Key features:

- uses HTTP header for request authorization

- Queries local system or obtains json responses from 3rd-party APIs

- Everything is static or statically templated. There is no query run if not configured by you.

- Stateless - no data are written to system

- Supports proxying to next-hop syspiper nodes, including JSON queries

# Install
```shell
# root is not needed for most cases 
cd /home/syspiper

# clone to this very directory
git clone ssh://git@github.com/astibal/syspiper self

# create virtual environment
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp apparmor.d/syspiper /etc/apparmor.d/
cp apparmor.d/tunables/syspiper /etc/apparmor.d/tunables/
```


# Run:
python app.py --config /etc/stats-proxy/prod-config.json

# Config example:


> Note: API key is required, it may contain only alphanumeric characters and -_@.
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
``
