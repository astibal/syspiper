# Deployment and operations

[Back to README](../README.md)

## systemd installation

The supplied unit expects `/opt/syspiper/self` and a `syspiper` user/group.
The following Debian/Ubuntu example uses root for installation and an
unprivileged account at runtime. Install Git, Python and venv support first.
Use an account with repository access if cloning over SSH instead of HTTPS.

```sh
sudo apt install git python3 python3-venv
sudo adduser --system --group --home /opt/syspiper syspiper
sudo git clone https://github.com/astibal/syspiper.git /opt/syspiper/self
cd /opt/syspiper/self
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.txt
sudo cp config_example.json config.json
sudo chown root:syspiper config.json
sudo chmod 640 config.json
sudoedit config.json
```

Choose a unique API key, client IP allowlist and real upstream URLs. The example
allows only loopback; add the intended clients or proxy IPs before remote use.
Keep code, scripts and the virtualenv administrator-owned and readable by the
service account. Do not make the source tree writable by that account.

```sh
sudo ln -s /opt/syspiper/self/systemd/syspiper.service /etc/systemd/system/syspiper.service
sudo systemctl daemon-reload
sudo systemctl enable --now syspiper
sudo systemctl status syspiper
sudo journalctl -u syspiper -n 50 --no-pager
```

If the account, checkout or unit already exists, use the upgrade procedure
instead of repeating creation commands. For optional APT statistics:

```sh
sudo apt install python3-apt
```

The APT helper uses `/usr/bin/python3 -I`; installing bindings in the application
virtualenv is unnecessary. It reads existing package indexes and never refreshes
or installs packages. Keep index refreshes under your normal administration.

## Binding, configuration and TLS

The unit runs four Gunicorn workers on `0.0.0.0:8181`. Override environment values
with `sudo systemctl edit syspiper`, for example:

```ini
[Service]
Environment=IP_PORT=127.0.0.1:8181
Environment=CONFIG_PATH=/etc/syspiper/config.json
```

Ensure any external configuration is readable by `syspiper`. The standalone
`run.here.sh` assigns its own port and does not accept an environment override;
run Gunicorn directly when you need other foreground options:

```sh
CONFIG_PATH=config.json .venv/bin/gunicorn -w 4 -b 127.0.0.1:8181 wsgi:app
```

The provided launchers serve plain HTTP. Use a trusted transport or configure TLS
termination before sending API keys across untrusted networks. With a reverse
proxy, enforce client access there as well: the application checks the immediate
peer address, not `X-Forwarded-For`. It does not configure proxy trust middleware.

Direct alias calls honor per-node `tls_verify`; keep it true. SysPiper next-hop
calls always verify TLS and send the local API key. They do not honor per-node
custom header or TLS settings. Requests can also use standard proxy and CA
bundle environment variables; review the service environment when diagnosing
unexpected outbound routing or certificate failures.

## Hardening and resource behavior

The unit runs without capabilities and enables a read-only system filesystem,
home-directory protection, private temporary storage and additional kernel and
process restrictions. It permits AF_INET, AF_INET6 and AF_NETLINK; netlink is
needed for interface enumeration. Statistics reflect the service's visible
namespaces and mount restrictions, which can differ from an interactive shell.

The default workers are synchronous. CPU samples block for one second, scripts
for up to five seconds, and the APT helper for up to ten seconds. Slow upstreams
or filesystem mounts can also occupy workers. Outbound HTTP operations have an
additional total budget of 15 seconds by default (`upstream_deadline_seconds`). There is no application-level
rate limit or collection cache. Size worker capacity for the polling workload.

Scripts retain filesystem/network access under the service identity. Their
resource limits are described in [Script authoring](scripts.md). A temporary
working directory is not a security boundary for untrusted code.

## VRF and AppArmor

The [unit file](../systemd/syspiper.service) contains an optional VRF override
using `ip vrf exec` and `setpriv`. Adapt the VRF and address to your host; the
example grants capabilities to the launcher and then drops privileges before
Gunicorn. It also relaxes some cgroup/filesystem restrictions. It is not required
for normal deployment. Test it on the intended host and check journal output.

The [AppArmor profile](../apparmor.d/syspiper) is experimental and is not enabled
by the installation above. Its [tunables](../apparmor.d/tunables/syspiper) default
to `/opt/syspiper/self`, matching the systemd unit. Adjust them for a custom
installation before loading the profile. It has not been validated here against live Gunicorn,
script subprocesses, the new HTTP helper, netlink collection or the
distribution-Python APT helper.
Profile parsing alone does not establish that those operations work.

## Upgrades

Update your checkout using your normal Git workflow, preserving local changes
and `config.json`. Then, from the deployment directory:

```sh
sudo .venv/bin/python -m pip install -r requirements.txt
sudo .venv/bin/python -m pip check
sudo systemctl daemon-reload
sudo systemctl restart syspiper
sudo journalctl -u syspiper -n 50 --no-pager
```

Dependencies are not fully pinned; record and validate the installed versions
for your deployment. The declared `urllib3>=2.7.0` floor is required by the
project's bounded decompression design; do not silently reuse an older environment.
Run the regression suite in an unprivileged development/test checkout before
upgrading production. Refresh the virtualenv if its underlying Python changed.

On older installations, migrate `myip.url` to `myip_url`; unknown keys now stop
startup. Correct invalid node references, URL/header types and absolute alias
URLs before restarting. Duplicate JSON keys are rejected as well. The development
server now defaults to 8181, matching Gunicorn; update clients previously using
8080. The sample next-hop name is now `node8181` (existing custom node names
remain valid). The new example enables
TLS verification and restricts access to loopback; existing `config.json` files
are not changed automatically. Upgrade every node in a proxy chain to preserve
hop limits end to end.

If increasing the total upstream budget, also review Gunicorn and reverse-proxy
timeouts: an outer server may terminate the request earlier. Keep distinct
connection/read and collector timeouts suited to their respective operations.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Startup cannot load config | Working directory, `--config` versus `CONFIG_PATH`, file permissions and JSON syntax |
| Import errors or missing pip | Whether the virtualenv still matches the installed Python; recreate it if necessary |
| HTTP 401 | Exact API key and actual peer IP, including IPv6/proxy address |
| HTTP 400 on an endpoint | Route spelling, script path and flat-route format |
| HTTP 404 for an alias | Alias/node names and the configuration on the node that resolves them |
| HTTP 422 | Script logs, unprivileged identity, helper imports and resource limits |
| HTTP 502 | Upstream status, certificates, DNS/connectivity, redirects, JSON and size limit |
| HTTP 504 for `/apt/<node>` | Collection may exceed the next-hop's five-second read timeout |
| HTTP 508 | Cyclic routes or an excessive incoming hop count |
| HTTP 200 with partial data | Per-section `status`, optional `reason`, kernel support and service permissions |
| Missing APT data | `/usr/bin/python3`, `python3-apt`, readable local package indexes |
| Interface permissions failure | AF_NETLINK and any additional sandbox restrictions |

Request logs include paths. Keep secrets in configured headers rather than
client URLs; script output and errors are also logged and must not contain
credentials. Protect access to logs and configuration files.
