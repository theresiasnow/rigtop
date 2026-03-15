"""Position sink: WSJT-X grid locator via UDP."""

import socket
import struct

from nmead.sinks import PositionSink, register_sink
from nmead.sources import Position


@register_sink("wsjtx")
class WsjtxSink(PositionSink):
    """Send Maidenhead grid locator updates to WSJT-X via its UDP protocol."""

    MAGIC = 0xADBCCBDA
    SCHEMA_VERSION = 2
    MSG_TYPE_LOCATION = 11

    def __init__(self, host: str = "127.0.0.1", port: int = 2237, client_id: str = "nmead"):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._sock: socket.socket | None = None
        self._last_grid: str | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _encode_utf8_string(self, s: str) -> bytes:
        encoded = s.encode("utf-8")
        return struct.pack(">I", len(encoded)) + encoded

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        if grid == self._last_grid:
            return None  # Skip if unchanged
        self._last_grid = grid

        payload = b""
        payload += struct.pack(">I", self.MAGIC)
        payload += struct.pack(">I", self.SCHEMA_VERSION)
        payload += struct.pack(">I", self.MSG_TYPE_LOCATION)
        payload += self._encode_utf8_string(self.client_id)
        payload += self._encode_utf8_string(grid)

        if self._sock:
            self._sock.sendto(payload, (self.host, self.port))
        return f"WSJT-X: grid {grid} sent to {self.host}:{self.port}"

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def __str__(self) -> str:
        return f"wsjtx@{self.host}:{self.port}"
