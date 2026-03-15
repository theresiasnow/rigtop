"""GPS source: IC-705 (and other rigs) via Hamlib rigctld."""

import socket

from nmead.sources import GpsSource, Position, register_source


@register_source("rigctld")
class RigctldSource(GpsSource):
    """Read GPS position from a rig via rigctld TCP protocol."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4532, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def _send_command(self, cmd: str) -> str:
        if not self._sock:
            raise ConnectionError("Not connected to rigctld")
        self._sock.sendall((cmd + "\n").encode())
        response = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if response.endswith(b"\n"):
                    break
            except socket.timeout:
                break
        return response.decode().strip()

    def get_position(self) -> Position | None:
        resp = self._send_command("+\\get_position")
        lines = resp.splitlines()
        lat = None
        lon = None
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
        # Fallback: simple two-line response (lat\nlon)
        if lat is None and lon is None and len(lines) >= 2:
            try:
                lat = float(lines[0])
                lon = float(lines[1])
            except ValueError:
                return None
        if lat is not None and lon is not None:
            return Position(lat, lon)
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

    def get_mode(self) -> str | None:
        resp = self._send_command("+\\get_mode")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("Mode:"):
                return line.split(":", 1)[1].strip()
        lines = resp.splitlines()
        return lines[0].strip() if lines else None

    def __str__(self) -> str:
        return f"rigctld@{self.host}:{self.port}"
