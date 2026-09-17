"""Read-only system snapshots; counters are raw and require client-side deltas."""
import json
import math
import os
import platform
import socket
import subprocess
import time
from pathlib import Path

import psutil


def read_value(reader):
    """Keep missing facilities distinct from real zero values."""
    try:
        return {"status": "ok", "data": reader()}
    except (PermissionError, psutil.AccessDenied):
        return {"status": "permission_denied", "data": None}
    except (FileNotFoundError, NotImplementedError, AttributeError):
        return {"status": "unsupported", "data": None}
    except (OSError, ValueError, psutil.Error):
        return {"status": "unavailable", "data": None}


def snapshot(**sections):
    return {
        "status": "ok" if all(v["status"] == "ok" for v in sections.values()) else "partial",
        "sampled_at": time.time(),
        **sections,
    }


def interfaces():
    addresses = read_value(psutil.net_if_addrs)
    links = read_value(psutil.net_if_stats)
    # nowrap caches are process-local; expose kernel counters across all workers.
    counters = read_value(lambda: psutil.net_io_counters(pernic=True, nowrap=False))
    families = {socket.AF_INET: "ipv4", socket.AF_INET6: "ipv6", psutil.AF_LINK: "mac"}
    if addresses["data"] is not None:
        addresses["data"] = {
            name: [{"family": families.get(a.family, str(a.family)),
                    "address": a.address, "netmask": a.netmask,
                    "broadcast": a.broadcast, "ptp": a.ptp} for a in values]
            for name, values in addresses["data"].items()
        }
    if links["data"] is not None:
        duplex = {psutil.NIC_DUPLEX_FULL: "full", psutil.NIC_DUPLEX_HALF: "half"}
        links["data"] = {
            name: {"is_up": s.isup, "mtu": s.mtu,
                   "speed_mbps": s.speed if s.speed > 0 else None,
                   "duplex": duplex.get(s.duplex),
                   "flags": getattr(s, "flags", "").split(",") if getattr(s, "flags", "") else []}
            for name, s in links["data"].items()
        }
    if counters["data"] is not None:
        counters["data"] = {name: value._asdict() for name, value in counters["data"].items()}
    return snapshot(addresses=addresses, links=links, counters=counters)


def system():
    def identity():
        uname = platform.uname()
        return {"hostname": uname.node, "os": uname.system, "kernel": uname.release,
                "architecture": uname.machine}

    def boot():
        boot_time = psutil.boot_time()
        return {"boot_time": boot_time, "uptime_seconds": max(0, time.time() - boot_time)}

    return snapshot(
        identity=read_value(identity), distro=read_value(distribution), boot=read_value(boot),
        cpu=read_value(lambda: {"logical": psutil.cpu_count(), "physical": psutil.cpu_count(logical=False)}),
        cpu_times=read_value(lambda: psutil.cpu_times()._asdict()),
        load=read_value(lambda: dict(zip(("avg1", "avg5", "avg15"), os.getloadavg()))),
    )


def distribution():
    release = platform.freedesktop_os_release()
    return {key.lower(): release.get(key) for key in
            ("ID", "ID_LIKE", "NAME", "PRETTY_NAME", "VERSION_ID", "VERSION_CODENAME")}


def apt():
    # The native apt bindings belong to the distro Python, not the app virtualenv.
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", str(Path(__file__).with_name("apt_stats.py"))],
            capture_output=True, text=True, timeout=10, check=True,
        )
        updates = json.loads(result.stdout)
    except FileNotFoundError:
        updates = {"status": "unsupported", "data": None}
    except PermissionError:
        updates = {"status": "permission_denied", "data": None}
    except subprocess.TimeoutExpired:
        updates = {"status": "unavailable", "data": None, "reason": "timeout"}
    except (OSError, subprocess.CalledProcessError, ValueError):
        updates = {"status": "unavailable", "data": None, "reason": "apt_helper_failed"}
    return snapshot(distro=read_value(distribution), updates=updates)


def filesystems():
    def inodes(path):
        stat = os.statvfs(path)
        if stat.f_files == 0:
            raise NotImplementedError
        return {"total": stat.f_files, "used": stat.f_files - stat.f_ffree,
                "free": stat.f_ffree, "available": stat.f_favail,
                "percent": (stat.f_files - stat.f_ffree) / stat.f_files * 100}

    partitions = read_value(lambda: psutil.disk_partitions(all=False))
    if partitions["data"] is not None:
        entries = []
        for p in partitions["data"]:
            usage = read_value(lambda: psutil.disk_usage(p.mountpoint)._asdict())
            inode_usage = read_value(lambda: inodes(p.mountpoint))
            entries.append({"device": p.device, "mountpoint": p.mountpoint,
                            "fstype": p.fstype, "options": p.opts,
                            "usage": usage, "inodes": inode_usage})
            if usage["status"] != "ok" or inode_usage["status"] != "ok":
                partitions["status"] = "partial"
        partitions["data"] = entries
    return snapshot(filesystems=partitions)


def parse_pressure(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        fields = dict(token.split("=", 1) for token in parts[1:])
        try:
            values = {key: float(fields[key]) for key in ("avg10", "avg60", "avg300")}
            values["total_us"] = int(fields["total"])
        except KeyError as error:
            raise ValueError("Incomplete PSI data") from error
        if any(not math.isfinite(v) or not 0 <= v <= 100 for k, v in values.items() if k != "total_us") or values["total_us"] < 0:
            raise ValueError("Invalid PSI data")
        result[parts[0]] = values
    if "some" not in result:
        raise ValueError("Missing PSI data")
    return result


def pressure():
    return snapshot(**{
        resource: read_value(lambda resource=resource: parse_pressure(
            Path(f"/proc/pressure/{resource}").read_text(encoding="ascii")))
        for resource in ("cpu", "memory", "io")
    })
