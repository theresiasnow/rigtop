"""Position sink: ICOM CI-V serial proxy backed by rigctld.

Exposes a virtual serial port that speaks ICOM CI-V protocol.
Programs like Ham Radio Deluxe connect to one end of a virtual serial
pair (e.g. COM15); rigtop writes the other end (e.g. COM14).

CI-V frames: FE FE <to> <from> <cmd> [<sub>] [<data>...] FD

The proxy translates CI-V read/write commands to rigctld TCP calls,
caches rig state from each poll cycle, and responds immediately to
read-polls that HRD issues at high rate.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from loguru import logger

from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position

# ---------------------------------------------------------------------------
# CI-V constants
# ---------------------------------------------------------------------------
PREAMBLE = 0xFE
EOM = 0xFD
CTRL_ADDR = 0xE0  # "computer" address (HRD default)

# ICOM rig addresses (add more as needed)
_RIG_ADDRS: dict[str, int] = {
    "IC-705": 0xA4,
    "IC-7300": 0x94,
    "IC-7100": 0x88,
    "IC-9700": 0xA2,
    "IC-7610": 0x98,
}
DEFAULT_RIG_ADDR = 0xA4  # IC-705


# ---------------------------------------------------------------------------
# BCD helpers — ICOM encodes frequency/mode data as BCD
# ---------------------------------------------------------------------------

def _freq_to_bcd(freq_hz: int) -> bytes:
    """Encode frequency (Hz) as 5-byte little-endian BCD (ICOM format).

    Example: 14_250_000 Hz → 00 00 25 41 00  (10-Hz digit first).
    """
    digits = f"{freq_hz:010d}"  # 10 digits, zero-padded
    # Group into 5 pairs (high→low): d9d8 d7d6 d5d4 d3d2 d1d0
    bcd = []
    for i in range(0, 10, 2):
        hi = int(digits[i])
        lo = int(digits[i + 1])
        bcd.append((hi << 4) | lo)
    # ICOM sends least-significant byte first
    bcd.reverse()
    return bytes(bcd)


def _bcd_to_freq(data: bytes) -> int:
    """Decode 5-byte little-endian BCD to frequency in Hz."""
    data = bytes(reversed(data))  # big-endian
    result = 0
    for b in data:
        result = result * 100 + (b >> 4) * 10 + (b & 0x0F)
    return result


# Mode byte → rigctld mode string
_CIV_MODE_TO_NAME: dict[int, str] = {
    0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW",
    0x04: "RTTY", 0x05: "FM", 0x06: "WFM", 0x07: "CW-R",
    0x08: "RTTY-R", 0x17: "DV",
}
_NAME_TO_CIV_MODE: dict[str, int] = {v: k for k, v in _CIV_MODE_TO_NAME.items()}

# Filter/passband byte (simplified — only common values)
_CIV_FILTER: dict[int, int] = {1: 0, 2: 0, 3: 0}  # filter 1/2/3 → passband=0 (normal)


def _mode_to_bytes(mode_name: str) -> bytes:
    """Return CI-V mode + filter bytes for a mode string."""
    mode_byte = _NAME_TO_CIV_MODE.get(mode_name.upper(), 0x01)
    filt = 0x01  # FIL1
    return bytes([mode_byte, filt])


def _bytes_to_mode(data: bytes) -> tuple[str, int]:
    """Parse CI-V mode byte(s) → (mode_name, filter_number)."""
    if not data:
        return "USB", 1
    mode_byte = data[0]
    filt = data[1] if len(data) > 1 else 1
    name = _CIV_MODE_TO_NAME.get(mode_byte, "USB")
    return name, filt


# ---------------------------------------------------------------------------
# CI-V frame builder / parser
# ---------------------------------------------------------------------------

def _build_frame(to_addr: int, from_addr: int, cmd: int,
                 sub: int | None = None, data: bytes = b"") -> bytes:
    """Build a complete CI-V frame."""
    frame = bytes([PREAMBLE, PREAMBLE, to_addr, from_addr, cmd])
    if sub is not None:
        frame += bytes([sub])
    frame += data
    frame += bytes([EOM])
    return frame


def _ack_frame(to_addr: int, from_addr: int) -> bytes:
    """FB (Fine Business) — positive acknowledgement."""
    return _build_frame(to_addr, from_addr, 0xFB)


def _nak_frame(to_addr: int, from_addr: int) -> bytes:
    """NG (No Good) — negative acknowledgement."""
    return _build_frame(to_addr, from_addr, 0xFA)


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------

@register_sink("civ_proxy")
class CivProxySink(PositionSink):
    """CI-V serial proxy — lets HRD (or any CI-V program) talk to rigctld.

    Parameters
    ----------
    device : str
        Serial port rigtop opens (e.g. ``COM14``).  HRD connects to the
        other end of the virtual pair (e.g. ``COM15``).
    baudrate : int
        Baud rate for the virtual serial port (default 19200).
    rig_addr : int
        CI-V address of the emulated rig (default 0xA4 = IC-705).
    rig_name : str
        Rig model name — used to auto-select *rig_addr* if not set.
    """

    def __init__(
        self,
        device: str = "",
        baudrate: int = 19200,
        rig_addr: int = 0,
        rig_name: str = "",
    ) -> None:
        self.device = device
        self.baudrate = baudrate
        if rig_addr:
            self.rig_addr = rig_addr
        elif rig_name:
            self.rig_addr = _RIG_ADDRS.get(rig_name, DEFAULT_RIG_ADDR)
        else:
            self.rig_addr = DEFAULT_RIG_ADDR
        self._serial = None  # serial.Serial
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Cached rig state (updated each poll cycle via send())
        self._freq_hz: int = 0
        self._mode: str = "USB"
        self._passband: int = 0
        self._ptt: bool = False
        self._s_meter: float = 0.0
        self._swr: float = 1.0
        self._rfpower: float = 0.0
        # Callback set by app.py — lets us issue rigctld commands
        self._rigctld_cmd: Callable[[str], str] | None = None
        # Statistics
        self._rx_frames = 0
        self._tx_frames = 0

    # ---- Lifecycle --------------------------------------------------------

    def start(self) -> None:
        if not self.device:
            logger.warning("civ_proxy: no device configured — disabled")
            return
        try:
            import serial
        except ImportError:
            logger.warning(
                "pyserial not installed — civ_proxy disabled. "
                "Install with: pip install pyserial"
            )
            return
        try:
            self._serial = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                timeout=0.05,  # short timeout for responsive reading
            )
            logger.info(
                "CI-V proxy opened {} @ {} baud (rig addr 0x{:02X})",
                self.device, self.baudrate, self.rig_addr,
            )
        except serial.SerialException as exc:
            logger.warning("civ_proxy: cannot open {}: {}", self.device, exc)
            self._serial = None
            return
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._serial:
            try:
                self._serial.close()
            except OSError:
                pass
            self._serial = None
        if self._reader_thread:
            self._reader_thread.join(timeout=3)

    # ---- PositionSink interface ------------------------------------------

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        """Cache latest rig state from the poll cycle."""
        freq = kwargs.get("freq")
        if freq:
            try:
                self._freq_hz = int(float(freq))
            except (ValueError, TypeError):
                pass
        mode = kwargs.get("mode")
        if mode:
            self._mode = mode
        passband = kwargs.get("passband")
        if passband is not None:
            try:
                self._passband = int(passband)
            except (ValueError, TypeError):
                pass
        ptt = kwargs.get("ptt")
        if ptt is not None:
            self._ptt = bool(ptt)
        meters = kwargs.get("meters")
        if meters and isinstance(meters, dict):
            if "STRENGTH" in meters:
                self._s_meter = meters["STRENGTH"]
            if "SWR" in meters:
                self._swr = meters["SWR"]
            if "RFPOWER_METER" in meters:
                self._rfpower = meters["RFPOWER_METER"]

        if self._serial and self._serial.is_open:
            return f"CI-V → {self.device} (rx:{self._rx_frames} tx:{self._tx_frames})"
        return None

    # ---- Serial reader ----------------------------------------------------

    def _read_loop(self) -> None:
        """Background thread: read CI-V frames from the serial port."""
        buf = bytearray()
        while not self._stop.is_set():
            if not self._serial or not self._serial.is_open:
                break
            try:
                chunk = self._serial.read(256)
            except OSError:
                break
            if not chunk:
                continue
            buf.extend(chunk)
            # Extract complete frames
            while True:
                # Find preamble pair
                start = -1
                for i in range(len(buf) - 1):
                    if buf[i] == PREAMBLE and buf[i + 1] == PREAMBLE:
                        start = i
                        break
                if start < 0:
                    # Keep only last byte (might be half of a preamble)
                    if len(buf) > 1:
                        buf = buf[-1:]
                    break
                # Find EOM
                eom_idx = buf.find(bytes([EOM]), start + 2)
                if eom_idx < 0:
                    # Incomplete frame — wait for more data
                    break
                frame = bytes(buf[start:eom_idx + 1])
                buf = buf[eom_idx + 1:]
                self._handle_frame(frame)

    # ---- Frame handler ----------------------------------------------------

    def _handle_frame(self, frame: bytes) -> None:
        """Parse and respond to a CI-V frame from the controller (HRD)."""
        # Minimum frame: FE FE to from cmd FD = 6 bytes
        if len(frame) < 6:
            return
        to_addr = frame[2]
        from_addr = frame[3]
        cmd = frame[4]
        payload = frame[5:-1]  # between cmd and FD

        # Only handle frames addressed to our rig
        if to_addr != self.rig_addr:
            return

        self._rx_frames += 1
        logger.debug(
            "CI-V RX: {:02X}→{:02X} cmd={:02X} payload={}",
            from_addr, to_addr, cmd,
            payload.hex() if payload else "(empty)",
        )

        response = self._dispatch(cmd, payload, from_addr)
        if response:
            self._write(response)

    def _dispatch(self, cmd: int, payload: bytes, caller: int) -> bytes | None:
        """Route a CI-V command to the appropriate handler."""
        match cmd:
            # 03 — Read operating frequency
            case 0x03:
                return self._handle_read_freq(caller)
            # 04 — Read operating mode
            case 0x04:
                return self._handle_read_mode(caller)
            # 05 — Set operating frequency
            case 0x05:
                return self._handle_set_freq(payload, caller)
            # 06 — Set operating mode
            case 0x06:
                return self._handle_set_mode(payload, caller)
            # 07 — Set VFO mode
            case 0x07:
                return _ack_frame(caller, self.rig_addr)
            # 15 — Read meter levels
            case 0x15:
                return self._handle_read_meter(payload, caller)
            # 1A — Read/write rig settings (many sub-commands)
            case 0x1A:
                # Respond with NAK for unsupported settings — HRD will cope
                return _nak_frame(caller, self.rig_addr)
            # 1C — PTT control
            case 0x1C:
                return self._handle_ptt(payload, caller)
            # 25 — Read frequency (IC-705/7300 extended)
            case 0x25:
                return self._handle_read_freq(caller)
            # 26 — Read mode (IC-705/7300 extended)
            case 0x26:
                return self._handle_read_mode(caller)
            case _:
                logger.debug("CI-V: unsupported cmd 0x{:02X}", cmd)
                return _nak_frame(caller, self.rig_addr)

    # ---- Command handlers -------------------------------------------------

    def _handle_read_freq(self, caller: int) -> bytes:
        bcd = _freq_to_bcd(self._freq_hz)
        return _build_frame(caller, self.rig_addr, 0x03, data=bcd)

    def _handle_read_mode(self, caller: int) -> bytes:
        mode_data = _mode_to_bytes(self._mode)
        return _build_frame(caller, self.rig_addr, 0x04, data=mode_data)

    def _handle_set_freq(self, payload: bytes, caller: int) -> bytes:
        if len(payload) < 5:
            return _nak_frame(caller, self.rig_addr)
        freq_hz = _bcd_to_freq(payload[:5])
        logger.info("CI-V: set freq {} Hz", freq_hz)
        # Forward to rigctld
        if self._rigctld_cmd:
            resp = self._rigctld_cmd(f"+F {freq_hz}")
            if "RPRT 0" in resp:
                self._freq_hz = freq_hz
                return _ack_frame(caller, self.rig_addr)
            return _nak_frame(caller, self.rig_addr)
        # No rigctld callback — just update cache
        self._freq_hz = freq_hz
        return _ack_frame(caller, self.rig_addr)

    def _handle_set_mode(self, payload: bytes, caller: int) -> bytes:
        if not payload:
            return _nak_frame(caller, self.rig_addr)
        mode_name, filt = _bytes_to_mode(payload)
        logger.info("CI-V: set mode {} filter {}", mode_name, filt)
        if self._rigctld_cmd:
            resp = self._rigctld_cmd(f"+M {mode_name} 0")
            if "RPRT 0" in resp:
                self._mode = mode_name
                return _ack_frame(caller, self.rig_addr)
            return _nak_frame(caller, self.rig_addr)
        self._mode = mode_name
        return _ack_frame(caller, self.rig_addr)

    def _handle_read_meter(self, payload: bytes, caller: int) -> bytes:
        """CI-V 15 xx — read meter value.

        Sub-commands: 02=S-meter, 11=RF power, 12=SWR.
        Values are 0000-0255 BCD (ICOM encoding).
        """
        if not payload:
            return _nak_frame(caller, self.rig_addr)
        sub = payload[0]
        match sub:
            case 0x02:  # S-meter (dB → 0-255 scale)
                # rigctld STRENGTH is in dB (e.g. -54). Map -60..0 → 0..255
                raw = max(0, min(255, int((self._s_meter + 60) * 255 / 60)))
                val_bcd = self._int_to_bcd16(raw)
                return _build_frame(caller, self.rig_addr, 0x15, sub=0x02, data=val_bcd)
            case 0x11:  # RF power meter (0-1 float → 0-255)
                raw = max(0, min(255, int(self._rfpower * 255)))
                val_bcd = self._int_to_bcd16(raw)
                return _build_frame(caller, self.rig_addr, 0x15, sub=0x11, data=val_bcd)
            case 0x12:  # SWR (1.0-10.0 → 0-255)
                raw = max(0, min(255, int((self._swr - 1.0) * 255 / 9.0)))
                val_bcd = self._int_to_bcd16(raw)
                return _build_frame(caller, self.rig_addr, 0x15, sub=0x12, data=val_bcd)
            case _:
                return _nak_frame(caller, self.rig_addr)

    def _handle_ptt(self, payload: bytes, caller: int) -> bytes:
        """CI-V 1C 00 — read/set PTT."""
        if not payload:
            return _nak_frame(caller, self.rig_addr)
        sub = payload[0]
        if sub != 0x00:
            return _nak_frame(caller, self.rig_addr)
        if len(payload) == 1:
            # Read PTT state
            ptt_byte = 0x01 if self._ptt else 0x00
            return _build_frame(caller, self.rig_addr, 0x1C, sub=0x00,
                                data=bytes([ptt_byte]))
        # Set PTT
        desired = payload[1] != 0x00
        logger.info("CI-V: set PTT {}", "TX" if desired else "RX")
        if self._rigctld_cmd:
            resp = self._rigctld_cmd(f"+T {1 if desired else 0}")
            if "RPRT 0" in resp:
                self._ptt = desired
                return _ack_frame(caller, self.rig_addr)
            return _nak_frame(caller, self.rig_addr)
        self._ptt = desired
        return _ack_frame(caller, self.rig_addr)

    # ---- Helpers ----------------------------------------------------------

    @staticmethod
    def _int_to_bcd16(val: int) -> bytes:
        """Encode an integer (0-9999) as 2-byte BCD (big-endian)."""
        val = max(0, min(9999, val))
        hi = val // 100
        lo = val % 100
        return bytes([((hi // 10) << 4) | (hi % 10),
                      ((lo // 10) << 4) | (lo % 10)])

    def _write(self, data: bytes) -> None:
        """Write a CI-V frame to the serial port."""
        if not self._serial or not self._serial.is_open:
            return
        with self._lock:
            try:
                self._serial.write(data)
                self._tx_frames += 1
                logger.debug("CI-V TX: {}", data.hex())
            except OSError as e:
                logger.warning("CI-V serial write error: {}", e)

    def set_rigctld_callback(self, callback: Callable[[str], str]) -> None:
        """Register the rigctld command callback for write operations."""
        self._rigctld_cmd = callback

    # ---- Display ----------------------------------------------------------

    def __str__(self) -> str:
        return f"civ_proxy@{self.device}"

    def connections(self) -> list[dict]:
        is_open = self._serial is not None and self._serial.is_open
        return [{
            "label": f"civ   {self.device}",
            "kind": "serial",
            "status": "open" if is_open else "closed",
            "clients": [
                f"{self.baudrate} baud",
                f"rig 0x{self.rig_addr:02X}",
                f"rx:{self._rx_frames} tx:{self._tx_frames}",
            ] if is_open else [],
        }]
