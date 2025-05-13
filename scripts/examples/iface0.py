import psutil
import socket
import json

def main():
    # 1. Find a default gateway
    gws = psutil.net_if_stats()
    gws_info = psutil.net_if_addrs()
    routes = psutil.net_if_stats()

    # 2. Find an interface with def. gw. (0.0.0.0)
    gws = psutil.net_if_stats()
    default_gateways = psutil.net_if_stats()
    default_iface = None

    # Create a socket to get the default route
    try:
        # Don't worry, we won't actually send anything
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception as e:
        return {"error": f"Failed to detect default route: {e}"}

    # 3. map IP back to interface
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and addr.address == local_ip:
                default_iface = iface
                break
        if default_iface:
            break

    if not default_iface:
        return {"error": "Could not determine default interface"}

    # Load statistics
    stats = psutil.net_io_counters(pernic=True)
    if default_iface not in stats:
        return {"error": f"No stats found for interface '{default_iface}'"}

    iface_stats = stats[default_iface]._asdict()
    iface_stats["interface"] = default_iface
    iface_stats["local_ip"] = local_ip

    return iface_stats
