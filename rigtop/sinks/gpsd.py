"""Position sink: gpsd-compatible JSON server (protocol 3.x)."""

from __future__ import annotations

import datetime
import json
import socket
import threading

from loguru import logger

from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position

_VERSION_RESPONSE = {
    "class": "VERSION",
    "release": "rigtop",
    "rev": "1.0",
    "proto_major": 3,
    "proto_minor": 14,
}

_DEVICES_RESPONSE = {
    "class": "DEVICES",
    "devices": [{"class": "DEVICE", "path": "/dev/rigtop", "activated": True}],
}

_WATCH_RESPONSE = {
    "class": "WATCH",
    "enable": True,
    "json": True,
    "nmea": False,
    "raw": 0,
    "scaled": False,
    "split24": False,
    "pps": False,
}


def _tpv(pos: Position) -> dict:
    """Build a gpsd TPV (Time-Position-Velocity) object."""
    return {
        "class": "TPV",
        "device": "/dev/rigtop",
        "mode": 3,  # 3-D fix
        "time": datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        "lat": round(pos.lat, 8),
        "lon": round(pos.lon, 8),
    }


class _GpsdClient:
    """State machine for a single gpsd client connection."""

    __slots__ = ("addr", "sock", "watching")

    def __init__(self, sock: socket.socket, addr: tuple) -> None:
        self.sock = sock
        self.addr = addr
        self.watching = False

    def send_json(self, obj: dict) -> bool:
        """Send a JSON line. Returns False if the connection is dead."""
        try:
            self.sock.sendall((json.dumps(obj) + "\r\n").encode("ascii"))
            return True
        except OSError:
            return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@register_sink("gpsd")
class GpsdSink(PositionSink):
    """TCP server implementing the gpsd JSON protocol (subset).

    Clients connect on the configured port (default 2947) and receive
    TPV position reports.  Compatible with any gpsd client library.

    Usage in Direwolf::

        GPSD  localhost:2947

    Usage with gpsd clients::

        gpspipe -w localhost:2947
        cgps -s localhost:2947
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2947) -> None:
        self.host = host
        self.port = port
        self._server: socket.socket | None = None
        self._clients: list[_GpsdClient] = []
        self._lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None
        self._reader_threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.settimeout(1.0)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        logger.info("gpsd sink listening on {}:{}", self.host, self.port)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()  # type: ignore[union-attr]
                conn.settimeout(1.0)
                client = _GpsdClient(conn, addr)
                # gpsd sends VERSION on connect
                client.send_json(_VERSION_RESPONSE)
                with self._lock:
                    self._clients.append(client)
                logger.info("gpsd client connected: {}:{}", addr[0], addr[1])
                t = threading.Thread(
                    target=self._read_client, args=(client,), daemon=True
                )
                t.start()
                self._reader_threads.append(t)
            except TimeoutError:
                continue
            except OSError:
                break

    def _read_client(self, client: _GpsdClient) -> None:
        """Read commands from a gpsd client."""
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = client.sock.recv(1024)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_command(client, line.decode("ascii", errors="replace").strip())
            except TimeoutError:
                continue
            except OSError:
                break
        self._remove_client(client)

    def _handle_command(self, client: _GpsdClient, cmd: str) -> None:
        """Process a gpsd client command."""
        if cmd.startswith("?WATCH"):
            client.watching = True
            client.send_json(_WATCH_RESPONSE)
            client.send_json(_DEVICES_RESPONSE)
        elif cmd.startswith("?DEVICES"):
            client.send_json(_DEVICES_RESPONSE)
        elif cmd.startswith("?VERSION"):
            client.send_json(_VERSION_RESPONSE)
        elif cmd.startswith("?POLL"):
            # POLL returns last known position — handled via the next send() call
            client.watching = True

    def _remove_client(self, client: _GpsdClient) -> None:
        client.close()
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                logger.info("gpsd client disconnected: {}:{}", client.addr[0], client.addr[1])

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        tpv = _tpv(pos)
        dead: list[_GpsdClient] = []

        with self._lock:
            for client in self._clients:
                if client.watching:
                    if not client.send_json(tpv):
                        dead.append(client)

        for client in dead:
            self._remove_client(client)

        n = len(self._clients) - len(dead)
        if n > 0:
            return f"gpsd → {n} client{'s' if n != 1 else ''}"
        return None

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for client in self._clients:
                client.close()
            self._clients.clear()
        if self._server:
            self._server.close()
            self._server = None
        if self._accept_thread:
            self._accept_thread.join(timeout=3)

    def __str__(self) -> str:
        return f"gpsd@{self.host}:{self.port}"

    def connections(self) -> list[dict]:
        with self._lock:
            client_addrs = [
                f"{c.addr[0]}:{c.addr[1]}" for c in self._clients
            ]
        return [{
            "label": f"gpsd  {self.host}:{self.port}",
            "kind": "tcp",
            "status": "listening" if self._server else "closed",
            "clients": client_addrs,
        }]
