"""APRS-IS position beacon sink.

Connects to an APRS-IS Tier 2 server and beacons live GPS positions.
Incoming APRS-IS traffic is logged so it appears in the TUI log pane.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from loguru import logger

from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position


def _format_lat(lat: float) -> str:
    """Format latitude as APRS DDMM.MMN."""
    hemi = "N" if lat >= 0 else "S"
    lat = abs(lat)
    deg = int(lat)
    minutes = (lat - deg) * 60
    return f"{deg:02d}{minutes:05.2f}{hemi}"


def _format_lon(lon: float) -> str:
    """Format longitude as APRS DDDMM.MME."""
    hemi = "E" if lon >= 0 else "W"
    lon = abs(lon)
    deg = int(lon)
    minutes = (lon - deg) * 60
    return f"{deg:03d}{minutes:05.2f}{hemi}"


@register_sink("aprsis")
class AprsIsSink(PositionSink):
    """Beacon live GPS to APRS-IS and display incoming traffic."""

    def __init__(
        self,
        callsign: str = "",
        server: str = "",
        passcode: str = "",
        port: int = 14580,
        comment: str = "rigtop",
        symbol_table: str = "/",
        symbol_code: str = ">",
        interval: int = 120,
    ) -> None:
        self._callsign = callsign
        self._server = server
        self._passcode = passcode
        self._port = port
        self._comment = comment
        self._symbol_table = symbol_table
        self._symbol_code = symbol_code
        self._interval = max(interval, 30)  # minimum 30s to be polite

        self._sock: socket.socket | None = None
        self._connected = False
        self._keepalive_thread: threading.Thread | None = None
        self._receiver_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_beacon = 0.0
        self._lock = threading.Lock()
        self.aprs_buffer = None  # set by main.py to share with TUI

        if not self._callsign:
            raise ValueError("aprsis: callsign required")
        if not self._server:
            raise ValueError("aprsis: server required")
        if not self._passcode:
            raise ValueError("aprsis: passcode required")

    def start(self) -> None:
        self._connect()
        self._stop_event.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="aprsis-keepalive"
        )
        self._keepalive_thread.start()
        self._receiver_thread = threading.Thread(
            target=self._receiver_loop, daemon=True, name="aprsis-receiver"
        )
        self._receiver_thread.start()

    def _connect(self) -> None:
        """Connect and log in to APRS-IS."""
        try:
            sock = socket.create_connection((self._server, self._port), timeout=15)
            sock.settimeout(30)
            # Read server banner
            banner = sock.recv(512).decode("ascii", errors="replace").strip()
            logger.info("APRS-IS banner: {}", banner)
            # Send login
            login = f"user {self._callsign} pass {self._passcode} vers rigtop 1.0\r\n"
            sock.sendall(login.encode("ascii"))
            # Read login response
            resp = sock.recv(512).decode("ascii", errors="replace").strip()
            logger.info("APRS-IS login: {}", resp)
            if "verified" not in resp.lower():
                logger.warning("APRS-IS login may have failed: {}", resp)
            with self._lock:
                self._sock = sock
                self._connected = True
            logger.info("APRS-IS connected to {}:{}", self._server, self._port)
        except OSError as e:
            logger.error("APRS-IS connection failed: {}", e)
            with self._lock:
                self._connected = False

    def _receiver_loop(self) -> None:
        """Read incoming APRS-IS packets and log them."""
        buf = ""
        while not self._stop_event.is_set():
            with self._lock:
                sock = self._sock
            if sock is None:
                self._stop_event.wait(5)
                continue
            try:
                data = sock.recv(4096)
                if not data:
                    continue
                buf += data.decode("ascii", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if self.aprs_buffer is not None:
                        self.aprs_buffer.push(line)
                    logger.info("APRS-IS: {}", line)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    self._stop_event.wait(5)

    def _keepalive_loop(self) -> None:
        """Send keepalive comments and handle reconnects."""
        while not self._stop_event.wait(60):
            with self._lock:
                sock = self._sock
            if sock is None:
                self._connect()
                continue
            try:
                sock.sendall(b"#keepalive\r\n")
            except OSError:
                logger.warning("APRS-IS keepalive failed, reconnecting")
                with self._lock:
                    self._connected = False
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
                self._connect()

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        now = time.monotonic()
        if now - self._last_beacon < self._interval:
            return None

        with self._lock:
            sock = self._sock
            connected = self._connected
        if not connected or sock is None:
            return None

        lat_str = _format_lat(pos.lat)
        lon_str = _format_lon(pos.lon)
        # APRS position: !DDMM.MMN/DDDMM.MME>comment
        packet = (
            f"{self._callsign}>APRS,TCPIP*:"
            f"!{lat_str}{self._symbol_table}{lon_str}{self._symbol_code}"
            f"{self._comment}\r\n"
        )
        try:
            sock.sendall(packet.encode("ascii"))
            self._last_beacon = now
            logger.debug("APRS-IS beacon: {}", packet.strip())
            return f"APRS-IS: beaconed to {self._server}"
        except OSError as e:
            logger.warning("APRS-IS send failed: {}", e)
            with self._lock:
                self._connected = False
            return None

    def close(self) -> None:
        self._stop_event.set()
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=5)
        if self._receiver_thread:
            self._receiver_thread.join(timeout=5)
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self._connected = False

    def connections(self) -> list[dict[str, Any]]:
        with self._lock:
            connected = self._connected
        return [
            {
                "label": f"APRS-IS {self._server}:{self._port}",
                "kind": "tcp",
                "status": "open" if connected else "closed",
                "clients": 0,
            }
        ]
