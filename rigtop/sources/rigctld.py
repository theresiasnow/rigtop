"""GPS source: IC-705 (and other rigs) via Hamlib rigctld."""

from __future__ import annotations

import os
import socket

import psutil
from loguru import logger

from rigtop.sources import GpsSource, Position, register_source

#: Meter levels available during TX
#: Friendly display names for well-known CAT client executables.
_CLIENT_ALIASES: dict[str, str] = {
    "L4ONG": "Log4OM",
    "Log4OM2": "Log4OM",
    "HRD": "HRD",
    "Ham Radio Deluxe": "HRD",
    "wsjtx": "WSJT-X",
    "jtdx": "JTDX",
    "js8call": "JS8Call",
    "flrig": "flrig",
    "omnirig": "OmniRig",
    "OmniRig": "OmniRig",
}

TX_METERS = ["ALC", "SWR", "RFPOWER_METER", "COMP_METER", "ID_METER", "VD_METER"]
#: Meter levels available during RX
RX_METERS = ["STRENGTH"]


@register_source("rigctld")
class RigctldSource(GpsSource):
    """Read GPS position from a rig via rigctld TCP protocol."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4532, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        logger.info("Connecting to rigctld at {}:{}", self.host, self.port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        logger.info("Connected to rigctld")

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def reconnect(self) -> None:
        """Close and re-establish the TCP connection to rigctld."""
        self.close()
        logger.info("Reconnecting to rigctld at {}:{}…", self.host, self.port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        logger.info("Reconnected")

    def _send_command(self, cmd: str) -> str:
        if not self._sock:
            raise ConnectionError("Not connected to rigctld")
        logger.debug("TX: {}", cmd)
        try:
            self._sock.sendall((cmd + "\n").encode())
        except OSError:
            self.reconnect()
            self._sock.sendall((cmd + "\n").encode())  # type: ignore[union-attr]
        response = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if response.endswith(b"\n"):
                    break
            except TimeoutError:
                logger.debug("RX: (timeout)")
                break
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                logger.warning("rigctld connection lost during recv: {}", e)
                self._sock = None
                return ""
        decoded = response.decode().strip()
        logger.debug("RX: {}", decoded)
        return decoded

    def get_position(self) -> Position | None:
        resp = self._send_command("+\\get_position")
        lines = resp.splitlines()
        lat = None
        lon = None
        alt = None
        for line in lines:
            line = line.strip()
            if line.startswith("Latitude:"):
                try:
                    lat = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("Longitude:"):
                try:
                    lon = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("Altitude:"):
                try:
                    alt = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        # Fallback: simple two-line response (lat\nlon)
        if lat is None and lon is None and len(lines) >= 2:
            try:
                lat = float(lines[0])
                lon = float(lines[1])
            except ValueError:
                return None
        if lat is not None and lon is not None:
            return Position(lat, lon, alt=alt)
        return None

    def get_frequency(self) -> str | None:
        resp = self._send_command("+\\get_freq")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("Frequency:"):
                return line.split(":", 1)[1].strip()
        lines = resp.splitlines()
        if lines:
            try:
                float(lines[0])
                return lines[0].strip()
            except ValueError:
                pass
        return None

    def get_mode_and_passband(self) -> tuple[str | None, int | None]:
        """Return (mode, passband_hz).  Passband is 0 when 'normal'."""
        resp = self._send_command("+\\get_mode")
        lines = resp.splitlines()
        mode: str | None = None
        passband: int | None = None
        for line in lines:
            line = line.strip()
            if line.startswith("Mode:"):
                mode = line.split(":", 1)[1].strip()
            elif line.startswith("Passband:"):
                try:
                    passband = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        # Fallback: two-line response  mode\npassband
        if mode is None and len(lines) >= 1:
            mode = lines[0].strip() or None
        if passband is None and len(lines) >= 2:
            try:
                passband = int(lines[1].strip())
            except ValueError:
                pass
        return mode, passband

    def get_mode(self) -> str | None:
        mode, _ = self.get_mode_and_passband()
        return mode

    def get_ptt(self) -> bool | None:
        """Return True if transmitting, False if receiving, None on error."""
        resp = self._send_command("+\\get_ptt")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("PTT:"):
                return line.split(":", 1)[1].strip() != "0"
            # Plain numeric response
            try:
                return int(line) != 0
            except ValueError:
                continue
        return None

    def get_level(self, level: str) -> float | None:
        """Read a single rig level value, e.g. 'ALC', 'SWR', 'STRENGTH'."""
        resp = self._send_command(f"+\\get_level {level}")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("Level Value:"):
                try:
                    return float(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
        # Fallback: plain numeric response
        for line in resp.splitlines():
            line = line.strip()
            if not line or line.startswith("RPRT"):
                continue
            try:
                return float(line)
            except ValueError:
                continue
        return None

    def get_strength(self) -> float | None:
        """Read signal strength, trying STRENGTH then RAWSTR (hamlib alias)."""
        val = self.get_level("STRENGTH")
        if val is None:
            val = self.get_level("RAWSTR")
        return val

    def get_meters(self, levels: list[str] | None = None) -> dict[str, float]:
        """Read multiple rig levels. Returns {name: value} for successful reads."""
        if levels is None:
            levels = TX_METERS + RX_METERS
        result: dict[str, float] = {}
        for lvl in levels:
            val = self.get_level(lvl)
            if val is not None:
                result[lvl] = val
        return result

    def set_level(self, level: str, value: float) -> bool:
        """Set a rig level (e.g. 'AF', 'RF', 'SQL', 'ATT'). Returns True on success."""
        resp = self._send_command(f"+L {level} {value}")
        return "RPRT 0" in resp

    def set_mode(self, mode: str, passband: int = 0) -> bool:
        """Set rig mode (e.g. FM, USB, LSB, CW). Returns True on success."""
        resp = self._send_command(f"+M {mode} {passband}")
        return "RPRT 0" in resp

    def set_freq(self, freq_hz: int) -> bool:
        """Set rig frequency in Hz. Returns True on success."""
        resp = self._send_command(f"+F {freq_hz}")
        return "RPRT 0" in resp

    def set_ptt(self, on: bool) -> bool:
        """Set PTT state. *on*=True → TX, *on*=False → RX. Returns True on success."""
        resp = self._send_command(f"+T {1 if on else 0}")
        return "RPRT 0" in resp

    def get_func(self, func: str) -> bool | None:
        """Read a rig function (e.g. 'NB', 'NR'). Returns True/False/None on error."""
        resp = self._send_command(f"+\\get_func {func}")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("Status:"):
                return line.split(":", 1)[1].strip() != "0"
            try:
                return int(line) != 0
            except ValueError:
                continue
        return None

    def set_func(self, func: str, on: bool) -> bool:
        """Set a rig function on or off (e.g. 'NB', 'NR'). Returns True on success."""
        resp = self._send_command(f"+U {func} {1 if on else 0}")
        return "RPRT 0" in resp

    def _connected_clients(self) -> list[str] | None:
        """Return process names of external clients connected to the rigctld port."""
        try:
            conns = psutil.net_connections(kind="tcp")
        except psutil.AccessDenied:
            return None
        our_pid = os.getpid()
        names: list[str] = []
        for c in conns:
            if not (c.raddr and c.raddr.port == self.port and c.status == psutil.CONN_ESTABLISHED):
                continue
            if c.pid == our_pid:
                continue
            try:
                name = psutil.Process(c.pid).name().removesuffix(".exe") if c.pid else "?"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = "?"
            names.append(_CLIENT_ALIASES.get(name, name))
        return names

    def connections(self) -> list[dict]:
        status = "open" if self._sock else "closed"
        clients = self._connected_clients() if self._sock else None
        conn: dict = {
            "label": "rigctld",
            "kind": "tcp",
            "status": status,
            "address": f"{self.host}:{self.port}",
        }
        if clients is not None:
            conn["clients"] = clients
        return [conn]

    def __str__(self) -> str:
        return f"rigctld@{self.host}:{self.port}"
