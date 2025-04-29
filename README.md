SysPiper - system info JSON gateway with proxy support and loop protection

This lightweight GET-only Flask app serves system info as JSON.
It operates in read-only mode, does not modify the system,
and accepts no parameters except fixed endpoints and parseable
elements from URL.

All parameters are white-listed, so unless there is vulnerability in 
flask argument parsing, you should be safe.

Supports proxying to allowed nodes, with automatic loop protection
when requests come from localhost addresses.

Supports remote requests with templated URLs.

# Install
```shell
# root is not needed for most cases 
cd /home/syspiper

# clone to this very directory
git clone ssh://git@github.com/astibal/syspiper .

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
If `allowed_ips` access list is present, but empty, system cannot be accessed.
On the contrary, if the list is _not_ present, system defaults to open access.


> Note: API key is required, it may contain only alphanumeric characters and -_@.
```json
{
  "api_key": "<some_secret_phrase>",
  "log_level": "INFO",
  "myip_url": "https://myip.dk",
  "allowed_ips": ["0.0.0.0/0"],
  "allowed_nodes": {
    "node1": "http://192.168.0.101:8080",
    "node2": "https://server02.example.com",
    "nodex": "http://127.0.0.1:8080"
  },
  "allowed_paths": {
    "public_ip": "/public_ip",
    "cpu": "/cpu",
    "ram": "/ram",
    "disk": "/disk",
    "net": "/net",
    "remote_cpu": "/api/~~keys~~/cpu"
  },
  "@keys": {
    "node1": "/node1-secret/",
    "node2": "/node2-secret/"
  }
}
```

# Test local:

`curl -X GET http://localhost:8181/cpu -H "X-API-Key: secret123"`

# Test proxy:

`curl -X GET http://localhost:8181/cpu/nodex  -H "X-API-Key: secret123"`



# Test remote:
This deserves little explanation. 
Above example shows templating system