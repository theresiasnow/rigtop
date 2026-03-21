"""Direwolf KISS TCP client — receives local RF APRS decodes."""

from __future__ import annotations

import socket
import threading

from loguru import logger

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def _kiss_unescape(data: bytes) -> bytes:
    """Remove KISS escape sequences."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == FESC:
            i += 1
            if i < len(data):
                if data[i] == TFEND:
                    out.append(FEND)
                elif data[i] == TFESC:
                    out.append(FESC)
                else:
                    out.append(data[i])
        else:
            out.append(data[i])
        i += 1
    return bytes(out)


def _decode_ax25_addr(data: bytes) -> tuple[str, int, bool]:
    """Decode one 7-byte AX.25 address.

    Returns ``(callsign, ssid, last_flag)``.
    """
    if len(data) < 7:
        return ("", 0, True)
    call = "".join(chr(b >> 1) for b in data[:6]).rstrip()
    ssid = (data[6] >> 1) & 0x0F
    last = bool(data[6] & 0x01)
    return (call, ssid, last)


def _ax25_to_tnc2(frame: bytes) -> str | None:
    """Convert a raw AX.25 UI frame to TNC2-format text.

    Returns e.g. ``'SM0XXX-9>APRS,WIDE1-1*:!5921.00N/01807.00E>mobile'``
    or *None* if the frame is not a UI frame.
    """
    if len(frame) < 16:
        return None

    # Destination (bytes 0-6)
    dest_call, dest_ssid, _ = _decode_ax25_addr(frame[0:7])
    dest = f"{dest_call}-{dest_ssid}" if dest_ssid else dest_call

    # Source (bytes 7-13)
    src_call, src_ssid, last = _decode_ax25_addr(frame[7:14])
    src = f"{src_call}-{src_ssid}" if src_ssid else src_call

    # Digipeater addresses
    digis: list[str] = []
    offset = 14
    while not last and offset + 7 <= len(frame):
        d_call, d_ssid, last = _decode_ax25_addr(frame[offset : offset + 7])
        d = f"{d_call}-{d_ssid}" if d_ssid else d_call
        if frame[offset + 6] & 0x80:  # H-bit: has been repeated
            d += "*"
        digis.append(d)
        offset += 7

    # Control (must be 0x03 = UI) + PID (must be 0xF0 = no layer 3)
    if offset + 2 > len(frame):
        return None
    if frame[offset] != 0x03 or frame[offset + 1] != 0xF0:
        return None

    info = frame[offset + 2 :].decode("ascii", errors="replace")

    path = ",".join(digis)
    if path:
        return f"{src}>{dest},{path}:{info}"
    return f"{src}>{dest}:{info}"


class DirewolfClient:
    """Background KISS TCP client that feeds decoded RF packets into an AprsBuffer."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8001) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._rx_count = 0
        self.aprs_buffer = None  # set by main.py

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def rx_count(self) -> int:
        return self._rx_count

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="direwolf-kiss",
        )
        self._thread.start()

    def _connect(self) -> bool:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=10)
            sock.settimeout(5)
            self._sock = sock
            self._connected = True
            logger.info("Direwolf KISS connected to {}:{}", self._host, self._port)
        except OSError as e:
            logger.warning("Direwolf KISS connection failed: {}", e)
            self._connected = False
            return False
        else:
            return True

    def _run(self) -> None:
        """Main loop: connect, read KISS frames, decode, push to buffer."""
        while not self._stop.is_set():
            if not self._connected:
                if not self._connect():
                    self._stop.wait(10)
                    continue

            buf = bytearray()
            try:
                while not self._stop.is_set():
                    try:
                        data = self._sock.recv(4096)
                    except TimeoutError:
                        continue
                    if not data:
                        raise ConnectionError("Direwolf KISS disconnected")
                    buf.extend(data)

                    # Extract complete KISS frames
                    while True:
                        # Find frame boundaries
                        start = buf.find(FEND)
                        if start == -1:
                            buf.clear()
                            break
                        # Skip leading FENDs
                        while start < len(buf) and buf[start] == FEND:
                            start += 1
                        end = buf.find(FEND, start)
                        if end == -1:
                            break  # incomplete frame
                        frame_raw = bytes(buf[start:end])
                        buf = buf[end + 1 :]

                        if not frame_raw:
                            continue

                        # First byte is KISS command (0x00 = data frame)
                        if frame_raw[0] != 0x00:
                            continue

                        ax25 = _kiss_unescape(frame_raw[1:])
                        tnc2 = _ax25_to_tnc2(ax25)
                        if tnc2:
                            self._rx_count += 1
                            logger.info("Direwolf RF: {}", tnc2)
                            if self.aprs_buffer is not None:
                                self.aprs_buffer.push(tnc2, source="rf-local")

            except (OSError, ConnectionError) as e:
                if not self._stop.is_set():
                    logger.warning("Direwolf KISS error: {} — reconnecting", e)
                self._connected = False
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
                self._stop.wait(5)

    def close(self) -> None:
        self._stop.set()
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def connections(self) -> list[dict]:
        status = "receiving" if self._connected and self._rx_count else (
            "open" if self._connected else "closed"
        )
        return [
            {
                "label": "DW KISS",
                "kind": "tcp",
                "status": status,
                "address": f"{self._host}:{self._port}",
                "clients": self._rx_count,
            }
        ]

    def __str__(self) -> str:
        return f"direwolf-kiss@{self._host}:{self._port}"
