"""APRS-IS position beacon sink with messaging.

Connects to an APRS-IS Tier 2 server and beacons live GPS positions.
Incoming APRS-IS traffic is logged so it appears in the TUI log pane.
Supports APRS message send/receive with acknowledgement.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from typing import Any

from loguru import logger

from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position

# APRS message format:  SENDER>PATH::DEST     :text{msgno}
# Dest callsign is padded to 9 characters with spaces.
_MSG_RE = re.compile(
    r"^(?P<sender>[^>]+)>[^:]*::(?P<dest>.{9}):(?P<body>.*)$"
)
_ACK_RE = re.compile(r"^ack(?P<msgno>\w+)$")
_REJ_RE = re.compile(r"^rej(?P<msgno>\w+)$")


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
        aprs_filter: str = "",
    ) -> None:
        self._callsign = callsign
        self._server = server
        self._passcode = passcode
        self._port = port
        self._comment = comment
        self._symbol_table = symbol_table
        self._symbol_code = symbol_code
        self._interval = max(interval, 30)  # minimum 30s to be polite
        self._filter = aprs_filter
        self._filter_sent = bool(aprs_filter)  # track if a filter has been sent

        self._sock: socket.socket | None = None
        self._connected = False
        self._keepalive_thread: threading.Thread | None = None
        self._receiver_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._beacon_enabled = True
        self._last_beacon = 0.0
        self._last_rx: float = 0.0    # monotonic time of last received packet
        self._rx_count: int = 0       # total received packets
        self._lock = threading.Lock()
        self.aprs_buffer = None  # set by main.py to share with TUI
        self.msg_buffer = None   # MessageBuffer, set by cli.py

        # Messaging state
        self._msg_seq = 0       # outgoing message sequence number
        self._pending_acks: dict[str, tuple[str, str, int, float]] = {}
        # {msgno: (dest, text, retries_left, next_retry_time)}
        self._retry_interval = 30.0
        self._max_retries = 5

        if not self._callsign:
            raise ValueError("aprsis: callsign required")
        if not self._server:
            raise ValueError("aprsis: server required")
        if not self._passcode:
            raise ValueError("aprsis: passcode required")

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def receiving(self) -> bool:
        """True if a packet was received within the last 5 minutes."""
        return (time.monotonic() - self._last_rx) < 300 if self._last_rx else False

    @property
    def rx_count(self) -> int:
        return self._rx_count

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
            login = f"user {self._callsign} pass {self._passcode} vers rigtop 1.0"
            if self._filter:
                login += f" filter {self._filter}"
            login += "\r\n"
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
        """Read incoming APRS-IS packets, parse messages, log traffic."""
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
                    self._last_rx = time.monotonic()
                    self._rx_count += 1
                    if self.aprs_buffer is not None:
                        self.aprs_buffer.push(line)
                    self._handle_message(line)
                    logger.info("APRS-IS: {}", line)
            except TimeoutError:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    self._stop_event.wait(5)

    def _handle_message(self, line: str) -> None:
        """Check if line is an APRS message addressed to us, or an ack."""
        m = _MSG_RE.match(line)
        if not m:
            return
        sender = m.group("sender").strip()
        dest = m.group("dest").strip()
        body = m.group("body")

        my_call = self._callsign.upper().split("-")[0]
        # Also match with SSID
        dest_upper = dest.upper()
        if dest_upper != my_call and dest_upper != self._callsign.upper():
            return

        # Check if it's an ack/rej for a message we sent
        ack_m = _ACK_RE.match(body)
        if ack_m:
            msgno = ack_m.group("msgno")
            if msgno in self._pending_acks:
                del self._pending_acks[msgno]
                logger.info("APRS msg ack received: {}", msgno)
                if self.msg_buffer is not None:
                    self.msg_buffer.mark_ack(msgno)
            return

        rej_m = _REJ_RE.match(body)
        if rej_m:
            msgno = rej_m.group("msgno")
            self._pending_acks.pop(msgno, None)
            logger.info("APRS msg rejected: {}", msgno)
            return

        # It's a regular message to us — extract text and msgno
        msgno = ""
        text = body
        if "{" in body:
            text, msgno = body.rsplit("{", 1)
            msgno = msgno.rstrip("}")

        logger.info("APRS msg from {}: {} (msgno={})", sender, text, msgno)
        if self.msg_buffer is not None:
            self.msg_buffer.push_rx(sender, text, msgno)

        # Send ack if msgno present
        if msgno:
            self._send_ack(sender, msgno)

    def _send_ack(self, dest: str, msgno: str) -> None:
        """Send an ack packet for a received message."""
        dest_padded = f"{dest:<9}"
        packet = f"{self._callsign}>APRS,TCPIP*::{dest_padded}:ack{msgno}\r\n"
        with self._lock:
            sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(packet.encode("ascii"))
            logger.debug("APRS ack sent: {}", packet.strip())
        except OSError as e:
            logger.warning("APRS ack send failed: {}", e)

    def send_message(self, dest: str, text: str) -> str | None:
        """Send an APRS message to dest callsign. Returns msgno."""
        with self._lock:
            sock = self._sock
            connected = self._connected
        if not connected or sock is None:
            return None

        self._msg_seq = (self._msg_seq + 1) % 100000
        msgno = str(self._msg_seq)
        dest_padded = f"{dest.upper():<9}"
        packet = f"{self._callsign}>APRS,TCPIP*::{dest_padded}:{text}{{{msgno}\r\n"
        try:
            sock.sendall(packet.encode("ascii"))
            logger.info("APRS msg sent to {}: {} {{{}}}", dest, text, msgno)
            # Track for ack
            self._pending_acks[msgno] = (
                dest, text, self._max_retries, time.monotonic() + self._retry_interval
            )
            if self.msg_buffer is not None:
                self.msg_buffer.push_tx(dest.upper(), text, msgno)
            return msgno
        except OSError as e:
            logger.warning("APRS msg send failed: {}", e)
            return None

    def _retry_pending(self) -> None:
        """Resend unacknowledged messages (called from keepalive loop)."""
        now = time.monotonic()
        expired = []
        for msgno, (dest, text, retries, next_t) in list(self._pending_acks.items()):
            if now < next_t:
                continue
            if retries <= 0:
                expired.append(msgno)
                continue
            dest_padded = f"{dest:<9}"
            packet = f"{self._callsign}>APRS,TCPIP*::{dest_padded}:{text}{{{msgno}\r\n"
            with self._lock:
                sock = self._sock
            if sock is None:
                break
            try:
                sock.sendall(packet.encode("ascii"))
                logger.debug("APRS msg retry to {} msgno={}", dest, msgno)
                self._pending_acks[msgno] = (
                    dest, text, retries - 1, now + self._retry_interval
                )
            except OSError:
                break
        for msgno in expired:
            del self._pending_acks[msgno]
            logger.warning("APRS msg {} gave up after retries", msgno)

    def _keepalive_loop(self) -> None:
        """Send keepalive comments, retry messages, and handle reconnects."""
        while not self._stop_event.wait(60):
            with self._lock:
                sock = self._sock
            if sock is None:
                self._connect()
                continue
            try:
                sock.sendall(b"#keepalive\r\n")
                self._retry_pending()
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
        if not self._beacon_enabled:
            return None
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
            # Auto-set range filter from first beacon position if none configured
            if not self._filter_sent:
                self._send_filter(f"r/{pos.lat:.1f}/{pos.lon:.1f}/200")
            return f"APRS-IS: beaconed to {self._server}"
        except OSError as e:
            logger.warning("APRS-IS send failed: {}", e)
            with self._lock:
                self._connected = False
            return None

    def _send_filter(self, filt: str) -> None:
        """Send a server-side filter command to APRS-IS."""
        with self._lock:
            sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(f"#filter {filt}\r\n".encode("ascii"))
            self._filter_sent = True
            logger.info("APRS-IS filter set: {}", filt)
        except OSError as e:
            logger.warning("APRS-IS filter send failed: {}", e)

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
        age = time.monotonic() - self._last_rx if self._last_rx else -1
        if connected and self._last_rx and age < 300:
            status = "receiving"
        elif connected:
            status = "open"
        else:
            status = "closed"
        return [
            {
                "label": f"APRS-IS {self._server}:{self._port}",
                "kind": "tcp",
                "status": status,
                "clients": self._rx_count,
            }
        ]
