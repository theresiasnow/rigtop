"""Position sink: NMEA GPS feed for Direwolf, PinPoint, and other NMEA consumers.

Supports two modes:
- **TCP** (default): Listens on a port; clients connect to receive NMEA.
  Works on all platforms.  Linux Direwolf: ``GPSNMEA host=localhost:10110``
- **Serial**: Writes NMEA to a COM/tty port.  Windows Direwolf requires this.
  Use a virtual serial port pair (com0com) — rigtop writes one end,
  the consumer reads the other.  ``GPSNMEA COM11``
"""

from __future__ import annotations

import socket
import threading

from loguru import logger

from rigtop.geo import build_gga_sentence, build_rmc_sentence
from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position


@register_sink("nmea")
class NmeaSink(PositionSink):
    """NMEA GGA+RMC feed via TCP server or serial port.

    If *device* is set (e.g. ``COM10``, ``/dev/ttyUSB0``), NMEA is written
    to that serial port.  Otherwise a TCP server is started on *host*:*port*.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 10110,
        device: str = "",
        baudrate: int = 4800,
    ) -> None:
        self.host = host
        self.port = port
        self.device = device
        self.baudrate = baudrate
        # TCP mode state
        self._server: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Serial mode state
        self._serial = None  # serial.Serial instance

    @property
    def _is_serial(self) -> bool:
        return bool(self.device)

    @property
    def connected(self) -> bool:
        if self._is_serial:
            return self._serial is not None and self._serial.is_open
        return self._server is not None

    def start(self) -> None:
        if self._is_serial:
            self._start_serial()
        else:
            self._start_tcp()

    def _start_serial(self) -> None:
        try:
            import serial
        except ImportError:
            logger.warning(
                "pyserial is not installed — serial port output disabled. "
                "Install it with: pip install pyserial"
            )
            self._serial = None
            return
        try:
            self._serial = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                timeout=1,
            )
            logger.info("NMEA sink writing to {} @ {} baud", self.device, self.baudrate)
        except serial.SerialException as exc:
            logger.warning("NMEA sink: cannot open {}: {}", self.device, exc)
            self._serial = None

    def _start_tcp(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.settimeout(1.0)
        self._server.bind((self.host, self.port))
        self._server.listen(4)
        logger.info("NMEA sink listening on {}:{}", self.host, self.port)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        """Accept incoming TCP connections in a background thread."""
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()  # type: ignore[union-attr]
                with self._lock:
                    self._clients.append(conn)
                logger.info("NMEA client connected: {}:{}", addr[0], addr[1])
            except TimeoutError:
                continue
            except OSError:
                break

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        gga = build_gga_sentence(pos.lat, pos.lon)
        rmc = build_rmc_sentence(pos.lat, pos.lon)
        data = (gga + "\r\n" + rmc + "\r\n").encode("ascii")

        if self._is_serial:
            return self._send_serial(data)
        return self._send_tcp(data)

    def _send_serial(self, data: bytes) -> str | None:
        if self._serial and self._serial.is_open:
            try:
                self._serial.write(data)
            except OSError as e:
                logger.warning("Serial write error: {}", e)
            else:
                return f"NMEA → {self.device}"
        return None

    def _send_tcp(self, data: bytes) -> str | None:
        dead: list[socket.socket] = []
        with self._lock:
            for sock in self._clients:
                try:
                    sock.sendall(data)
                except OSError:
                    dead.append(sock)
            for sock in dead:
                self._clients.remove(sock)
                try:
                    sock.close()
                except OSError:
                    pass
                logger.info("NMEA client disconnected")

        n = len(self._clients) - len(dead)
        if n > 0:
            return f"NMEA → {n} client{'s' if n != 1 else ''}"
        return None

    def close(self) -> None:
        self._stop.set()
        # Serial cleanup
        if self._serial:
            try:
                self._serial.close()
            except OSError:
                pass
            self._serial = None
        # TCP cleanup
        with self._lock:
            for sock in self._clients:
                try:
                    sock.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._server:
            self._server.close()
            self._server = None
        if self._accept_thread:
            self._accept_thread.join(timeout=3)

    def __str__(self) -> str:
        if self._is_serial:
            return f"nmea@{self.device}"
        return f"nmea@{self.host}:{self.port}"

    def connections(self) -> list[dict]:
        label = "nmea"
        if self._is_serial:
            is_open = self._serial is not None and self._serial.is_open
            return [{
                "label": label,
                "kind": "serial",
                "status": "open" if is_open else "closed",
                "address": self.device,
                "clients": [f"{self.baudrate} baud"] if is_open else [],
            }]
        with self._lock:
            client_addrs = []
            for sock in self._clients:
                try:
                    peer = sock.getpeername()
                    client_addrs.append(f"{peer[0]}:{peer[1]}")
                except OSError:
                    pass
        return [{
            "label": label,
            "kind": "tcp",
            "status": "listening" if self._server else "closed",
            "address": f"{self.host}:{self.port}",
            "clients": client_addrs,
        }]
