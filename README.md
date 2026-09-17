# SysPiper

SysPiper is a Flask JSON gateway for host statistics, administrator-installed
Python scripts, and configured upstream APIs. It supports forwarding requests
through other SysPiper nodes with an eight-hop limit.

```text
Client -- API key + IP allowlist --> SysPiper
                                      |-- local system collectors
                                      |-- trusted Python scripts
                                      |-- configured JSON API
                                      `-- next-hop SysPiper --> target
```

All application endpoints require `X-API-Key` and an allowed client IP. Routes
use GET; Flask also supplies HEAD and OPTIONS. HEAD can still run the collector
or script. There is no arbitrary command endpoint or caller-supplied upstream URL.
Scripts execute with the service account's permissions, so install only code you trust.

## Quick start

Use Linux and Python 3.10 or newer (the source uses Python 3.10 syntax and APIs).
Python dependencies are listed in [requirements.txt](requirements.txt).
APT statistics additionally require the distribution's `python3-apt` package.

```sh
git clone https://github.com/astibal/syspiper.git
cd syspiper
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config_example.json config.json
chmod 600 config.json
```

Edit `config.json`: replace the example key, choose allowed client addresses,
and configure any upstream nodes. The supplied example allows loopback only.
Then start the development server as your ordinary user:

```sh
python app.py --config config.json
# In another terminal, use the key you configured:
curl -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8080/system
```

`python app.py` binds to `0.0.0.0:8080`. For foreground Gunicorn on
`0.0.0.0:8181`, run `./run.here.sh` from the repository root. Use the
[deployment guide](docs/deployment.md) for systemd installation and TLS guidance.

## Documentation

- [Configuration and routing](docs/configuration.md): keys, IP access, aliases, templates, proxy chains.
- [API reference](docs/api.md): endpoints, response fields, errors, snapshots, and APT counts.
- [Deployment and operations](docs/deployment.md): installation, upgrades, hardening, troubleshooting.
- [Script authoring](docs/scripts.md): return values, imports, execution limits.

## Development and verification

Run the regression suite as an unprivileged user from the repository root:

```sh
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

Tests cover authentication, routing, upstream HTTP limits, script execution and
system/APT collectors. They mock upstream HTTP; some collector and script tests
also exercise the local OS. They do not validate a deployed systemd, VRF or
AppArmor configuration.

| File | Responsibility |
| --- | --- |
| `app.py` | Configuration, authentication, Flask routes and outbound HTTP |
| `system_stats.py` | Structured system snapshots and APT helper invocation |
| `apt_stats.py` | Distribution-Python helper for local APT indexes |
| `sexec.py` | Resource-limited execution of trusted scripts |
| `wsgi.py` | Gunicorn entry point |
| `scripts/` | Installed script endpoints and their helpers |
| `systemd/`, `apparmor.d/` | Deployment examples |
| `tests/` | Standard-library unittest regression suite |

`filters.py` is not imported by the application.

## License

[BSD 3-Clause](LICENSE). Copyright (c) 2025–2026 Ales Stibal.
