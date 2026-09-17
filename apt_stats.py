"""Read-only APT helper, run with the distribution Python for python3-apt."""
import json
import re
import time
from pathlib import Path


def security_origin(origin):
    """Recognize signed distribution security pockets, never arbitrary PPAs."""
    if not origin.trusted:
        return False
    if origin.origin in ("Ubuntu", "UbuntuESM", "UbuntuESMApps"):
        return origin.archive.endswith("-security")
    return origin.origin == "Debian" and (
        origin.label == "Debian-Security" or origin.archive.endswith("-security")
        or origin.archive.endswith("/updates")
    )


def count_updates(cache, version_compare, hold_state):
    total = security = held = security_held = 0
    for package in cache:
        installed, candidate = package.installed, package.candidate
        if installed is None or candidate is None:
            continue
        if version_compare(candidate.version, installed.version) <= 0:
            continue
        total += 1
        is_held = package._pkg.selected_state == hold_state
        held += int(is_held)
        # A newer ordinary candidate can supersede a still-pending security fix.
        is_security = any(
            version_compare(version.version, installed.version) > 0
            and version_compare(version.version, candidate.version) <= 0
            and any(security_origin(origin) for origin in version.origins)
            for version in package.versions
        )
        security += int(is_security)
        security_held += int(is_security and is_held)
    return {"total": total, "security": security, "held": held,
            "security_held": security_held}


def collect():
    try:
        import apt
        import apt_pkg
    except ImportError:
        return {"status": "unsupported", "data": None, "reason": "python3_apt_missing"}

    try:
        lists = Path(apt_pkg.config.find_dir("Dir::State::lists"))
        timestamps = [p.stat().st_mtime for p in lists.iterdir()
                      if re.search(r"_Packages(?:\.(?:lz4|gz|xz|bz2|zst))?$", p.name)]
        if not timestamps:
            return {"status": "unavailable", "data": None, "reason": "package_indexes_missing"}
        with apt.Cache(memonly=True) as cache:
            counts = count_updates(cache, apt_pkg.version_compare, apt_pkg.SELSTATE_HOLD)
        return {"status": "ok", "data": {
            **counts,
            "indexes": {"oldest_mtime": min(timestamps), "newest_mtime": max(timestamps),
                        "oldest_age_seconds": max(0, time.time() - min(timestamps))},
        }}
    except PermissionError:
        return {"status": "permission_denied", "data": None}
    except (OSError, SystemError, ValueError):
        return {"status": "unavailable", "data": None, "reason": "apt_cache_unavailable"}


if __name__ == "__main__":
    print(json.dumps(collect()))
