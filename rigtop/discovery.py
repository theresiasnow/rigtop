"""LAN discovery for network-capable amateur radios.

Scans the local subnet for Icom radios (CI-V over network) and
rigctld instances.  Uses concurrent TCP connect probes with a short
timeout so a /24 sweep finishes in a few seconds.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger

# Well-known ports used by amateur radio equipment on LAN.
_PORTS: dict[int, str] = {
    50001: "Icom CI-V control",
    50002: "Icom audio stream",
    4532:  "rigctld",
}


def _local_subnets() -> list[ipaddress.IPv4Network]:
    """Return /24 subnets for every non-loopback IPv4 interface."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        # Enumerate interfaces via UDP dummy connect trick
        # Get all local IPs by connecting a UDP socket to a public IP
        # (no actual traffic is sent).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0)
            try:
                s.connect(("10.255.255.255", 1))
                ip = s.getsockname()[0]
            except OSError:
                ip = "127.0.0.1"
        if ip != "127.0.0.1":
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            nets.append(net)
    except Exception:
        pass

    # Also try getaddrinfo for the hostname
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if addr.startswith("127."):
                continue
            net = ipaddress.IPv4Network(f"{addr}/24", strict=False)
            if net not in nets:
                nets.append(net)
    except OSError:
        pass
    return nets


def _probe(host: str, port: int, timeout: float) -> dict | None:
    """TCP connect probe. Returns info dict on success, None otherwise."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            # Try to read a banner (rigctld sends one)
            s.settimeout(0.5)
            try:
                banner = s.recv(256).decode("ascii", errors="replace").strip()
            except (TimeoutError, OSError):
                banner = ""
            return {
                "host": host,
                "port": port,
                "service": _PORTS.get(port, "unknown"),
                "banner": banner,
            }
    except (OSError, TimeoutError):
        return None


def scan_lan(
    ports: list[int] | None = None,
    timeout: float = 0.3,
    workers: int = 128,
    progress_cb=None,
) -> list[dict]:
    """Scan local subnets for radio services.

    Args:
        ports:       Ports to probe (default: all known).
        timeout:     TCP connect timeout per host/port.
        workers:     Max parallel threads.
        progress_cb: Optional callback(scanned, total) for progress.

    Returns:
        List of dicts with keys: host, port, service, banner.
    """
    if ports is None:
        ports = list(_PORTS)

    subnets = _local_subnets()
    if not subnets:
        logger.warning("No local subnets found")
        return []

    # Build work items: (host, port)
    targets: list[tuple[str, int]] = []
    targets.extend(
        (str(host), port)
        for net in subnets
        for host in net.hosts()
        for port in ports
    )

    total = len(targets)
    logger.info("Scanning {} targets across {} subnets", total, len(subnets))

    results: list[dict] = []
    scanned = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_probe, h, p, timeout): (h, p)
            for h, p in targets
        }
        for future in as_completed(futures):
            scanned += 1
            if progress_cb and scanned % 50 == 0:
                progress_cb(scanned, total)
            r = future.result()
            if r is not None:
                results.append(r)

    # Sort by IP then port
    results.sort(key=lambda r: (
        struct.pack("!I", int(ipaddress.IPv4Address(r["host"]))),
        r["port"],
    ))
    return results


def format_results(results: list[dict]) -> str:
    """Format scan results as a human-readable string."""
    if not results:
        return "No radio services found on LAN."
    lines = ["Found radio services on LAN:", ""]
    for r in results:
        line = f"  {r['host']}:{r['port']}  {r['service']}"
        if r.get("banner"):
            line += f"  ({r['banner'][:60]})"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)
