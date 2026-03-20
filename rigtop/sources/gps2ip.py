"""GPS source: iOS GPS2IP app (TCP NMEA stream)."""

import socket

import pynmea2

from rigtop.sources import GpsSource, Position, register_source


@register_source("gps2ip")
class Gps2ipSource(GpsSource):
    """Read GPS position from iOS GPS2IP app via TCP NMEA stream.

    GPS2IP streams raw NMEA sentences over TCP. We connect and parse
    GGA or RMC sentences to extract position.
    """

    def __init__(self, host: str = "192.168.1.1", port: int = 11123, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer: str = ""

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        self._buffer = ""

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None
        self._buffer = ""

    def _read_sentences(self) -> list[str]:
        """Read available NMEA sentences from the TCP stream."""
        if not self._sock:
            raise ConnectionError("Not connected to GPS2IP")
        try:
            data = self._sock.recv(4096)
            if not data:
                raise ConnectionError("GPS2IP connection closed")
            self._buffer += data.decode("ascii", errors="ignore")
        except TimeoutError, ConnectionResetError:
            pass

        sentences = []
        while "\r\n" in self._buffer:
            line, self._buffer = self._buffer.split("\r\n", 1)
            line = line.strip()
            if line.startswith("$"):
                sentences.append(line)
        return sentences

    def get_position(self) -> Position | None:
        sentences = self._read_sentences()
        for raw in sentences:
            try:
                msg = pynmea2.parse(raw)
            except pynmea2.ParseError:
                continue

            if isinstance(msg, (pynmea2.types.talker.GGA, pynmea2.types.talker.RMC)):
                if msg.latitude and msg.longitude:
                    alt = None
                    if hasattr(msg, "altitude") and msg.altitude:
                        try:
                            alt = float(msg.altitude)
                        except ValueError, TypeError:
                            pass
                    return Position(msg.latitude, msg.longitude, alt=alt)
        return None

    def __str__(self) -> str:
        return f"gps2ip@{self.host}:{self.port}"
