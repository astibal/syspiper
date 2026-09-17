# API reference

[Back to README](../README.md)

Send `X-API-Key` on every request and `Accept: application/json` when you need
JSON errors. Success responses are JSON. GET is the application method;
Flask's automatic HEAD may execute the same work but omits the response body.
OPTIONS also requires authentication.

## Built-in endpoints

Every endpoint in this table supports an optional `/<node>` suffix for
[forwarding](configuration.md). Byte fields use bytes; percentages use 0–100.

| Endpoint | Response fields / behavior |
| --- | --- |
| `/cpu` | `status`, `percent`; blocks for a one-second CPU sample |
| `/ram` | `status`, `total`, `available`, `percent`, `used`, `free` |
| `/disk` | `status`, `total`, `used`, `free`, `percent`; usage of `/` only |
| `/net` | `status`, `sent`, `recv`; aggregate cumulative network bytes, using psutil's default process-local wrap correction |
| `/public_ip` | `status`, `ip`; upstream response text verbatim, not validated or stripped |
| `/interfaces` | Snapshot of addresses, links and raw per-interface counters |
| `/system` | Snapshot of identity, distribution, boot, CPU counts/times and load |
| `/filesystems` | Snapshot of mounted filesystem byte and inode usage |
| `/pressure` | Snapshot of Linux pressure stall information |
| `/apt` | Snapshot of distribution and locally indexed available package updates |

`/cpu`, `/ram`, `/disk`, `/net`, and `/public_ip` use the original flat format
with `status: "ok"`; they do not have `sampled_at` or per-section failure states.
Use `/interfaces` for network deltas across multiple Gunicorn workers.
Configured aliases return the upstream JSON value without adding a snapshot
envelope. Script response formats are described in [Script authoring](scripts.md).
There is no dedicated root, discovery or health endpoint.

## Errors

```json
{
  "status": "error",
  "code": 401,
  "name": "Unauthorized",
  "description": "Unauthorized"
}
```

Errors use JSON when Accept is absent, empty, exactly `*/*`, or contains
`application/json`; other Accept values receive a small HTML error page.

| HTTP status | Typical cause |
| --- | --- |
| 400 | Unknown script, malformed flat route, invalid proxy hop header |
| 401 | Wrong/missing key or disallowed source IP |
| 404 | Unknown node or alias on a recognized route |
| 405 | Unsupported HTTP method after successful authentication |
| 422 | Script failed, timed out, returned invalid data, or was run as root |
| 500 | Unhandled local application error |
| 502 | Upstream HTTP error, connection failure, invalid JSON, redirect, oversized body or failed substitution |
| 504 | Direct alias or next-hop connection/read timeout, or the total upstream deadline on any outbound path |
| 508 | Next-hop forwarding limit reached |

An upstream error status is normally mapped to 502, not transparently relayed.
Next-hop forwarding preserves 508. `/public_ip` maps connection/read timeouts to
502, but the new total elapsed-time deadline returns 504.
Snapshot section failures normally still return HTTP 200 with `status: "partial"`;
clients must inspect both HTTP and body status.

## Extended system snapshots

The following authenticated GET endpoints also support `/<node>` proxying,
for example `/interfaces/node8181`. Existing endpoints retain their response formats.

| Endpoint | Sections | Contents |
| --- | --- | --- |
| `/interfaces` | `addresses`, `links`, `counters` | Maps keyed by interface name: IPv4/IPv6/MAC addresses, netmasks, link state, MTU, speed in Mbps, duplex, flags, byte/packet/error/drop counters |
| `/system` | `identity`, `distro`, `boot`, `cpu`, `cpu_times`, `load` | Hostname, OS, kernel, architecture, boot time, uptime in seconds, logical/physical CPU counts, cumulative CPU times in seconds, 1/5/15-minute load averages |
| `/filesystems` | `filesystems` | List of mounted filesystems reported by `psutil.disk_partitions(all=False)` (pseudo filesystems filtered), device, mountpoint, type, options, byte usage and inode usage |
| `/pressure` | `cpu`, `memory`, `io` | Linux PSI from `/proc/pressure`, with `some`/`full` where provided, percentage averages over 10/60/300 seconds and cumulative `total_us` |

Every structured snapshot response includes `sampled_at` (Unix time in seconds, recorded at the end
of collection) and `status` (`ok` or `partial`). Each section has `status` and
`data`. Failed sections return `data: null` and `unsupported`,
`permission_denied`, or `unavailable`. Filesystem entries contain separate
`usage` and `inodes` sections; individual mount failures do not discard other mounts.
Unknown link speed/duplex and unavailable CPU counts are null. Filesystems
reporting zero total inodes expose inode statistics as unsupported.

Example shape for an interface counter section:

```json
{
  "status": "ok",
  "data": {
    "eth0": {
      "bytes_sent": 1024, "bytes_recv": 2048,
      "packets_sent": 10, "packets_recv": 20,
      "errin": 0, "errout": 0, "dropin": 0, "dropout": 0
    }
  }
}
```

The `/interfaces` network counters are raw cumulative kernel values, without process-local wrap
correction. Calculate rates from successive samples (`delta bytes / delta time`);
discard intervals across reboots, interface recreation or counter decreases.
The structured snapshot collectors do not sleep or maintain a background sampler. Sections are read
sequentially, so a snapshot is not atomic. Filesystem queries can be delayed by
unresponsive mounts. Statistics reflect the service's visible namespaces and
filesystem sandbox, which may differ from an interactive host session.

PSI requires kernel support and readable `/proc/pressure` files. System-wide CPU
`full` is undefined by Linux and may be reported as zero; do not interpret it as
an independent health signal. Missing PSI is reported without failing the request.

The supplied systemd unit allows `AF_NETLINK` for interface enumeration while
retaining an empty capability set. When upgrading an installed unit, reload
systemd and restart the service to apply this change. A stricter external
sandbox may still return `permission_denied` for interface addresses/link data.

## Debian / Ubuntu APT updates

`/system` includes a `distro` section read from `os-release`, with `id`,
`id_like`, `name`, `pretty_name`, `version_id` and `version_codename`.

`GET /apt` (or `/apt/<node>`) returns `distro` and `updates` sections using the
same snapshot envelope and authentication as other system endpoints. For example:

```json
{
  "status": "ok",
  "sampled_at": 1789632000,
  "distro": {"status": "ok", "data": {"id": "ubuntu", "version_id": "24.04"}},
  "updates": {
    "status": "ok",
    "data": {
      "total": 12,
      "security": 5,
      "held": 2,
      "security_held": 1,
      "indexes": {
        "oldest_mtime": 1789500000,
        "newest_mtime": 1789630000,
        "oldest_age_seconds": 132000
      }
    }
  }
}
```

Install the optional OS dependency with `sudo apt install python3-apt`.
The collector invokes `/usr/bin/python3 -I` with a fixed local helper and a
10-second timeout, so native APT bindings need not be installed in the app's
virtualenv. It opens an in-memory APT cache and never updates indexes, installs
packages, acquires an installation lock or requests root access. Index refreshes
remain the responsibility of your existing APT timers/administration.

Counts represent installed binary packages (architectures counted separately)
whose APT policy candidate is newer than the installed version. Held packages
are included in `total` and `security`; `held` and `security_held` are subsets.
This is not an upgrade transaction simulation: dependency resolution, phasing
and holds can affect what an actual upgrade installs.

Security classification recognizes trusted Debian security origins (including
legacy `/updates` suites), Ubuntu `-security`, and Ubuntu ESM Infra/Apps security
pockets. It checks available versions newer than installed and no newer than the
candidate, so an ordinary update superseding a pending security fix still counts
once as security. Packages pinned at their installed version are excluded.

These are **available package updates, not a CVE count or a vulnerability audit**.
Counts only cover locally indexed, enabled sources; disabled/unavailable ESM,
missing security sources and fixes no longer represented in security indexes
cannot be inferred. A zero security count does not prove the host is patched.

Index timestamps are package-index file mtimes, **not the time of the last
successful `apt update`**; APT can preserve repository timestamps and unchanged
indexes can legitimately be old. The oldest/newest timestamps provide context,
not a definitive freshness verdict. Missing indexes, missing python3-apt,
permission failures and timeouts return null data with an explicit status/reason
rather than a misleading zero count. No package names or repository credentials
are returned.

## CPU time counters

`/system.cpu_times.data` contains the fields supplied by `psutil.cpu_times()`
for the host (for example `user`, `system`, `idle`, `iowait`, `irq`, `softirq`,
`steal`, `guest`, and `guest_nice` on Linux). Values are cumulative seconds,
not percentages, and fields vary by platform. Consumers should handle counter
resets and avoid double-counting guest time included in user/nice counters.

## Upstream limits

All three outbound paths (next-hop, direct alias, public-IP lookup) reject HTTP
3xx without following redirects. Configure the final URL. Bodies are streamed
in 64 KiB chunks and limited to 1 MiB after decompression before JSON parsing.
An oversized declared Content-Length is rejected before body collection; missing
or invalid lengths do not bypass the streaming limit. Responses are closed on
success and failure. This is a response limit, not a process-memory limit.

Connection/read timeouts remain 5 seconds for next-hop and public-IP requests,
and 3 seconds for direct aliases. These detect connection/read stalls and remain
independent of the total budget. The APT helper retains its separate 10-second
deadline, so a valid slow `/apt` collection can outlast the 5-second next-hop read
timeout; this behavior is intentional.

`upstream_deadline_seconds` adds a total elapsed-time budget (default 15 seconds)
for each outbound operation. It covers helper startup, DNS, connection/TLS,
response headers, body transfer and child-side JSON parsing. A separate helper
process performs the fetch; the parent kills and reaps it on expiry, closing its
sockets even if headers or the body keep arriving a byte at a time. This works
with both synchronous and threaded WSGI workers. Expiry returns HTTP 504 on all
three outbound paths. Normal process scheduling and cleanup add some overhead;
this is not a real-time scheduling guarantee.

The helper adds one short-lived Python process per outbound request, using the
same virtualenv and service identity. URL credentials and configured headers
are passed over stdin, not in command-line arguments. There is no new Python
dependency. The 1 MiB upstream body bound remains; the internal JSON result
representation can be larger due to escaping. Each hop has its own budget,
not one shared budget for the entire proxy chain.
