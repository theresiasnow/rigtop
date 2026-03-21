"""Full-screen TUI dashboard using Textual."""

from __future__ import annotations

import datetime
import re
import threading
import time as _time
from collections import deque
from typing import ClassVar

from loguru import logger
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.suggester import Suggester
from textual.widgets import Header, Input, Label, RichLog, Static
from textual.worker import get_current_worker

from rigtop.geo import format_position, maidenhead
from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position

# ── APRS / message buffer classes (used by aprsis sink + cli.py) ───────────

class AprsBuffer:
    """Ring buffer of incoming APRS-IS packets for display."""

    _RF_TOKENS: ClassVar[set[str]] = {"qAR", "qAr", "qAo", "qAO"}

    def __init__(self, maxlen: int = 200) -> None:
        self._lines: deque[tuple[str, str]] = deque(maxlen=maxlen)

    @staticmethod
    def _classify(raw: str) -> str:
        if ">" in raw:
            header = raw.split(":", 1)[0]
            path = header.split(">", 1)[1] if ">" in header else ""
            tokens = {t.strip().rstrip("*") for t in path.split(",")}
            if tokens & AprsBuffer._RF_TOKENS:
                return "rf"
        return "is"

    def push(self, line: str, source: str | None = None) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        src = source if source else self._classify(line)
        self._lines.append((src, f"{ts}  {line}"))

    def render(self, max_lines: int = 8) -> Text:
        tail = list(self._lines)[-max_lines:]
        txt = Text()
        if not tail:
            txt.append(" (no APRS traffic)", style="dim")
            return txt
        for i, (source, line) in enumerate(tail):
            style = "yellow" if source == "rf-local" else ("green" if source == "rf" else "cyan")
            txt.append(f" {line}", style=style)
            if i < len(tail) - 1:
                txt.append("\n")
        return txt


class MessageBuffer:
    """Ring buffer of APRS messages (sent and received)."""

    def __init__(self, maxlen: int = 100) -> None:
        self._msgs: deque[tuple[str, str, str]] = deque(maxlen=maxlen)
        self.unread: int = 0

    def push_rx(self, sender: str, text: str, msgno: str = "") -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        tag = f"{{{msgno}}}" if msgno else ""
        self._msgs.append(("rx", sender, f"{ts}  {sender}> {text}{tag}"))
        self.unread += 1

    def push_tx(self, dest: str, text: str, msgno: str = "", acked: bool = False) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        tag = f"{{{msgno}}}" if msgno else ""
        status = " ✓" if acked else ""
        self._msgs.append(("tx", dest, f"{ts}  >{dest}: {text}{tag}{status}"))

    def mark_ack(self, msgno: str) -> None:
        for i in range(len(self._msgs) - 1, -1, -1):
            d, c, line = self._msgs[i]
            if d == "tx" and f"{{{msgno}}}" in line and "✓" not in line:
                self._msgs[i] = (d, c, line + " ✓")
                break

    def render(self, max_lines: int = 8) -> Text:
        tail = list(self._msgs)[-max_lines:]
        txt = Text()
        if not tail:
            txt.append(" (no messages)", style="dim")
            return txt
        for i, (direction, _call, line) in enumerate(tail):
            style = "bold yellow" if direction == "rx" else "cyan"
            txt.append(f" {line}", style=style)
            if i < len(tail) - 1:
                txt.append("\n")
        self.unread = 0
        return txt


# ── Direwolf output helpers ─────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")
_CHAN_RE = re.compile(r"^\[\d+[LR]?\]")
_IGATE_RE = re.compile(r"^\[ig\]", re.IGNORECASE)

_DW_STYLES: dict[str, tuple[str, str]] = {
    "rigtop":  ("dim",        "dim"),
    "error":   ("dim red",    "bold red"),
    "warn":    ("dim yellow", "yellow"),
    "igate":   ("dim green",  "green"),
    "packet":  ("dim cyan",   "bold white"),
    "status":  ("dim",        "dim yellow"),
    "info":    ("dim",        "white"),
}

_STATUS_KEYWORDS = (
    "Ready to accept", "Attached to KISS", "Now connected",
    "Check server", "connected to IGate", "Listening",
    "Dire Wolf", "dire wolf", "direwolf",
)


def _dw_clean(line: str) -> str:
    return _CTRL_RE.sub("", _ANSI_RE.sub("", line)).strip()


def _dw_tag(line: str) -> str:
    if line.startswith("[rigtop]"):
        return "rigtop"
    low = line.lower()
    if "error" in low or "fatal" in low:
        return "error"
    if "warning" in low:
        return "warn"
    if _IGATE_RE.match(line):
        return "igate"
    if _CHAN_RE.match(line):
        return "packet"
    if any(k in line for k in _STATUS_KEYWORDS):
        return "status"
    return "info"


class DirewolfBuffer:
    """Ring buffer kept for API compatibility; Textual app writes to RichLog directly."""

    def __init__(self, maxlen: int = 200) -> None:
        self._lines: deque[tuple[str, str, str]] = deque(maxlen=maxlen)
        self.packet_count: int = 0
        # Optional secondary callback (set by RigtopApp to forward to RichLog)
        self._forward: list[object] = []  # callable[str] | None stored as list

    def push(self, line: str) -> None:
        clean = _dw_clean(line)
        if not clean:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        tag = _dw_tag(clean)
        if tag == "packet":
            self.packet_count += 1
        self._lines.append((ts, tag, clean))
        for cb in self._forward:
            try:
                cb(ts, tag, clean)
            except Exception:
                pass

    @property
    def has_content(self) -> bool:
        return bool(self._lines)

    def render(self, max_lines: int = 12) -> Text:
        tail = list(self._lines)[-max_lines:]
        txt = Text(overflow="fold")
        if not tail:
            txt.append(" (no output)", style="dim")
            return txt
        for i, (ts, tag, line) in enumerate(tail):
            ts_style, line_style = _DW_STYLES.get(tag, ("dim", "white"))
            txt.append(f" {ts} ", style=ts_style)
            txt.append(line, style=line_style)
            if i < len(tail) - 1:
                txt.append("\n")
        return txt


# ── Meter rendering ─────────────────────────────────────────────────────────

METER_ORDER = ["STRENGTH", "RFPOWER", "ALC", "SWR", "RFPOWER_METER", "COMP_METER"]
METER_LABELS = {
    "STRENGTH":     "S-meter",
    "RFPOWER":      "TX pwr",
    "ALC":          "ALC",
    "SWR":          "SWR",
    "RFPOWER_METER": "Pwr out",
    "COMP_METER":   "Comp",
}


def _s_meter_text(db: float) -> str:
    if db <= -54:
        return "S0"
    if db >= 0:
        return f"S9+{db:.0f}"
    return f"S{max(0, int((db + 54) / 6))}"


def _meter_bar(name: str, value: float, width: int = 18) -> Text:
    if name == "STRENGTH":
        norm = (value + 54) / 114
    elif name == "SWR":
        norm = min((value - 1.0) / 4.0, 1.0)
    else:
        norm = value
    norm = max(0.0, min(1.0, norm))
    filled = int(norm * width)
    bar = "█" * filled + "░" * (width - filled)

    if name == "SWR":
        colour = "green" if value < 2.0 else ("yellow" if value < 3.0 else "red")
    elif name == "ALC":
        colour = "green" if value < 0.5 else ("yellow" if value < 0.8 else "red")
    elif name == "STRENGTH":
        colour = "green" if value > -24 else ("yellow" if value > -42 else "dim")
    else:
        colour = "cyan"

    if name == "STRENGTH":
        val_str = f"{_s_meter_text(value)} ({value:+.0f}dB)"
    elif name == "SWR":
        val_str = f"{value:.1f}:1"
    elif name == "RFPOWER":
        val_str = f"{value * 100:.0f}%"
    else:
        val_str = f"{value:.2f}"

    label = METER_LABELS.get(name, name)
    line = Text()
    line.append(f" {label:<8} ", style="bold")
    line.append(bar, style=colour)
    line.append(f"  {val_str}")
    if (name == "SWR" and value >= 3.0) or (name == "ALC" and value >= 0.8):
        line.append("  ⚠", style="bold red blink")
    return line


# ── Command completion ──────────────────────────────────────────────────────

class CommandSuggester(Suggester):
    """Inline ghost-text completion for rigtop commands."""

    _COMMANDS: ClassVar[dict[str, list[str]]] = {
        "aprs":   ["on", "off"],
        "aprsis": ["on", "off"],
        "packet": ["on", "off"],
        "wsjtx":  ["on", "off"],
        "nmea":   ["on", "off"],
        "civ":    ["on", "off"],
        "data":   ["on", "off"],
        "dw":     ["aprs"],
        "freq":   [],
        "help":   [],
        "igate":  ["on", "off"],
        "info":   [],
        "mode":   ["USB", "LSB", "FM", "AM", "CW", "CWR", "PKTUSB", "PKTLSB", "PKTFM"],
        "msg":    [],
        "q":      [],
        "quit":   [],
        "scan":   [],
    }

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=False)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        parts = value.split()
        if not parts:
            return None
        # Completing command name
        if len(parts) == 1 and not value.endswith(" "):
            prefix = parts[0].lower()
            for cmd in sorted(self._COMMANDS):
                if cmd.startswith(prefix) and cmd != prefix:
                    return cmd
            return None
        # Completing argument
        cmd = parts[0].lower()
        candidates = self._COMMANDS.get(cmd, [])
        if not candidates:
            return None
        arg_prefix = parts[1] if len(parts) > 1 and not value.endswith(" ") else ""
        for arg in candidates:
            if arg.lower().startswith(arg_prefix.lower()):
                full = f"{parts[0]} {arg}"
                return full if full.lower() != value.lower().rstrip() else None
        return None


# ── Textual widgets ─────────────────────────────────────────────────────────

class RigPanel(Static):
    """Left/right top pane: frequency, mode, PTT state and meter bars."""

    def render_data(
        self,
        freq: str | None,
        mode: str | None,
        passband: int | None,
        ptt: bool | None,
        meters: dict[str, float],
        rig_name: str,
        wd_tripped: bool,
    ) -> None:
        txt = Text()
        if rig_name:
            txt.append(f" {rig_name}\n", style="bold cyan")
        # Frequency
        if freq:
            try:
                mhz = f"{float(freq) / 1e6:.6f} MHz"
            except (ValueError, TypeError):
                mhz = str(freq)
        else:
            mhz = "—"
        freq_style = "bold red" if ptt else "bold green"
        txt.append(f" {mhz}\n", style=freq_style)
        # Mode
        if mode:
            pb = f"  ({passband} Hz)" if passband else ""
            txt.append(f" {mode}{pb}\n", style="bold")
        # PTT / watchdog
        if ptt:
            txt.append(" ● TX\n", style="bold red blink")
        elif wd_tripped:
            txt.append(" ⚠ WATCHDOG\n", style="bold yellow blink")
        else:
            txt.append(" ○ RX\n", style="dim green")
        txt.append("\n")
        # Meter bars
        shown = False
        for name in METER_ORDER:
            if name in meters:
                txt.append_text(_meter_bar(name, meters[name]))
                txt.append("\n")
                shown = True
        if not shown:
            txt.append(" No meter data\n", style="dim")
        self.update(txt)


class StationPanel(Static):
    """Right top pane: GPS position, grid, altitude, uptime."""

    def render_data(
        self,
        pos: Position | None,
        grid: str,
        gps_src: str,
        start_time: float,
        source_label: str,
    ) -> None:
        txt = Text()
        if source_label:
            txt.append(f" {source_label}\n", style="dim")
        if pos is None:
            txt.append(" No GPS fix\n", style="yellow")
        else:
            txt.append(f" {format_position(pos.lat, pos.lon)}\n", style="bold white")
            txt.append(f" {pos.lat:.6f}, {pos.lon:.6f}\n", style="dim")
            txt.append(" Grid  ", style="dim")
            txt.append(f"{grid}\n", style="bold green")
            if pos.alt is not None:
                txt.append(" Alt   ", style="dim")
                txt.append(f"{pos.alt:.0f} m\n", style="bold")
        txt.append(" GPS   ", style="dim")
        txt.append(f"{gps_src}\n", style="bold" if gps_src == "rig" else "yellow")
        # Uptime
        uptime_s = int(_time.monotonic() - start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        txt.append(" Up    ", style="dim")
        txt.append(f"{h:02d}:{m:02d}:{s:02d}\n", style="dim")
        self.update(txt)


class ConnectionBar(Static):
    """Multi-line connection status for all active sinks and sources."""

    _ACTIVE: ClassVar[set[str]] = {"receiving", "open", "listening", "ready"}

    def render_data(self, sinks: list, dw_launcher=None, dw_client=None) -> None:
        self.border_title = "Connections"
        lines: list[Text] = []

        for sink in sinks:
            if type(sink).__name__ == "TuiSink":
                continue
            if hasattr(sink, "connections"):
                lines.extend(self._fmt_conn(c) for c in sink.connections())
            elif hasattr(sink, "connected"):
                connected = sink.connected
                name = type(sink).__name__.replace("Sink", "").replace("Source", "")
                icon = "●" if connected else "○"
                colour = "green" if connected else "dim red"
                row = Text()
                row.append(f" {icon} ", style=colour)
                row.append(name, style="bold" if connected else "dim")
                lines.append(row)

        if dw_client is not None and hasattr(dw_client, "connections"):
            lines.extend(self._fmt_conn(c) for c in dw_client.connections())

        if dw_launcher is not None:
            running = dw_launcher.running
            active = dw_launcher.active_config or ""
            profile = active.replace("direwolf-", "").replace(".conf", "")
            icon = "●" if running else "○"
            colour = "green" if running else "dim"
            row = Text()
            row.append(f" {icon} ", style=colour)
            row.append("Direwolf", style="bold" if running else "dim")
            if profile:
                row.append(f"  {profile}", style=colour)
            row.append("  running" if running else "  stopped", style=colour)
            lines.append(row)

        if not lines:
            txt = Text()
            txt.append(" No monitored connections", style="dim")
            self.update(txt)
            return

        txt = Text()
        for i, line in enumerate(lines):
            txt.append_text(line)
            if i < len(lines) - 1:
                txt.append("\n")
        self.update(txt)

    def _fmt_conn(self, conn: dict) -> Text:
        status = conn.get("status", "")
        label = conn.get("label", "")
        kind = conn.get("kind", "")
        clients = conn.get("clients", [])

        active = status in self._ACTIVE
        icon = "●" if active else "○"
        colour = "green" if active else "dim red"
        if status == "closed":
            colour = "dim red"

        row = Text()
        row.append(f" {icon} ", style=colour)
        row.append(label, style="bold" if active else "dim")
        if kind:
            row.append(f"  [{kind}]", style="dim")
        row.append(f"  {status}", style=colour)
        if isinstance(clients, int) and clients > 0:
            row.append(f"  {clients} pkts", style="dim")
        elif isinstance(clients, list) and clients:
            row.append(f"  ← {', '.join(str(c) for c in clients)}", style="dim cyan")
        return row


# ── Main Textual application ────────────────────────────────────────────────

class RigtopApp(App[None]):
    """Textual rigtop dashboard."""

    CSS = """
    Screen {
        layout: vertical;
        overflow: hidden hidden;
    }
    #top-row {
        height: 12;
    }
    RigPanel {
        width: 1fr;
        border: round $accent;
        padding: 0 1;
        height: 100%;
        overflow-y: auto;
    }
    StationPanel {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
        height: 100%;
        overflow-y: auto;
    }
    ConnectionBar {
        height: 8;
        padding: 0 1;
        border: round $surface;
        border-title-color: $text-muted;
    }
    #dw-log {
        height: 10;
        border: round green;
    }
    #cmd-bar {
        height: 3;
        border: tall $accent;
        background: $panel;
    }
    #cmd-prompt {
        width: auto;
        padding: 0 1;
        color: $accent;
        content-align: left middle;
    }
    #cmd-input {
        width: 1fr;
        border: none;
        background: $panel;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("escape", "clear_input", "Clear", show=False),
        Binding("f1", "show_help", "Help"),
    ]

    # ── Reactive title badges ──
    _dw_running: reactive[bool] = reactive(False)
    _aprs_is: reactive[bool] = reactive(False)
    _aprs_active: reactive[bool] = reactive(False)
    _packet_active: reactive[bool] = reactive(False)
    _wd_tripped: reactive[bool] = reactive(False)

    def __init__(
        self,
        *,
        rig,
        sinks: list,
        dw_launcher=None,
        dw_client=None,
        dw_buffer: DirewolfBuffer | None = None,
        rigctld_buffer: DirewolfBuffer | None = None,
        aprs_buffer: AprsBuffer | None = None,
        msg_buffer: MessageBuffer | None = None,
        aprs_config=None,
        packet_config=None,
        rig_name: str = "",
        interval: float = 0.5,
        meters: bool = True,
        gps_fallback=None,
        static_pos: Position | None = None,
        watchdog=None,
        beacon_disabled: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._rig = rig
        self._sinks = sinks
        self._dw_launcher = dw_launcher
        self._dw_client = dw_client
        self._dw_buffer = dw_buffer
        self._rigctld_buffer = rigctld_buffer
        self._aprs_buffer = aprs_buffer
        self._msg_buffer = msg_buffer
        self._aprs_config = aprs_config
        self._packet_config = packet_config
        self._rig_name = rig_name
        self._interval = interval
        self._meters_enabled = meters
        self._gps_fallback = gps_fallback
        self._static_pos = static_pos
        self._watchdog = watchdog
        self._beacon_disabled = beacon_disabled

        # Poll state
        self._start_time = _time.monotonic()
        self._tx_start: float | None = None
        self._prev_ptt: bool = False
        self._tx_hold_until: float = 0.0
        self._tx_hold_meters: dict[str, float] = {}
        self._saved_freq: int | None = None
        self._saved_mode: str | None = None
        self._last_info: dict = {}
        self._dw_active_config: str = ""  # track last-seen config name

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top-row"):
            yield RigPanel(id="rig-panel")
            yield StationPanel(id="station-panel")
        yield ConnectionBar(id="conn-bar")
        yield RichLog(id="dw-log", highlight=False, markup=False, auto_scroll=True)
        with Horizontal(id="cmd-bar"):
            yield Label("❯ ", id="cmd-prompt")
            yield Input(
                placeholder="aprs on | packet on | wsjtx on | freq | mode | help | q",
                id="cmd-input",
                suggester=CommandSuggester(),
            )

    def on_mount(self) -> None:
        self._update_title()
        self._wire_dw_log()
        self._wire_rigctld_log()
        # Replay any buffered Direwolf lines collected before mount
        if self._dw_buffer:
            dw_log = self.query_one("#dw-log", RichLog)
            for ts, tag, line in list(self._dw_buffer._lines):
                self._write_dw_line(dw_log, ts, tag, line)
        self._refresh_conn_bar()
        self._start_poll()
        self._start_conn_refresh()
        self.query_one("#cmd-input", Input).focus()

    def _wire_dw_log(self) -> None:
        """Forward new DirewolfBuffer pushes to the RichLog widget."""
        dw_log = self.query_one("#dw-log", RichLog)

        def _forward(ts: str, tag: str, line: str) -> None:
            self.call_from_thread(self._write_dw_line, dw_log, ts, tag, line)
            if tag == "packet":
                self.call_from_thread(self._update_dw_title)

        if self._dw_buffer is not None:
            self._dw_buffer._forward.append(_forward)
        # Also replace launcher callback so future output goes directly here
        if self._dw_launcher is not None:
            orig = self._dw_launcher.stderr_callback

            def _combined(raw: str) -> None:
                if orig:
                    orig(raw)  # still pushes to _dw_buffer (classification + count)

            self._dw_launcher.stderr_callback = _combined

    def _wire_rigctld_log(self) -> None:
        if self._rigctld_buffer is None:
            return

        def _forward(ts: str, tag: str, line: str) -> None:
            logger.debug("rigctld: {}", line)

        self._rigctld_buffer._forward.append(_forward)

    @staticmethod
    def _write_dw_line(log: RichLog, ts: str, tag: str, line: str) -> None:
        ts_style, line_style = _DW_STYLES.get(tag, ("dim", "white"))
        txt = Text(overflow="fold")
        txt.append(f" {ts} ", style=ts_style)
        txt.append(line, style=line_style)
        log.write(txt)

    # ── Title management ─────────────────────────────────────────────────────

    def _update_title(self) -> None:
        parts = ["rigtop"]
        if self._rig_name:
            parts.append(self._rig_name)
        self.title = "  ".join(parts)
        badges = []
        if self._aprs_active:
            detail = " IS" if self._aprs_is else (" RF" if self._dw_running else "")
            badges.append(f"Mode: APRS{detail}")
        elif self._packet_active:
            badges.append("Mode: PACKET")
        elif self._dw_running:
            # DW running without a named mode (e.g. :dw aprs used directly)
            cfg = (self._dw_launcher.active_config or "") if self._dw_launcher else ""
            profile = cfg.replace("direwolf-", "").replace(".conf", "").upper()
            badges.append(f"Mode: {profile}" if profile else "DW")
        if self._wd_tripped:
            badges.append("⚠ WATCHDOG")
        self.sub_title = "  ".join(badges)

    def _update_dw_title(self) -> None:
        pkts = self._dw_buffer.packet_count if self._dw_buffer else 0
        cfg = self._dw_launcher.active_config or "—" if self._dw_launcher else "—"
        state = "running" if (self._dw_launcher and self._dw_launcher.running) else "stopped"
        dw_log = self.query_one("#dw-log", RichLog)
        dw_log.border_title = f"Direwolf  {state} ({cfg})  {pkts} pkts"

    # ── Poll loop ────────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, name="poll")
    def _start_poll(self) -> None:
        worker = get_current_worker()
        while not worker.is_cancelled:
            try:
                data = self._do_poll()
                self.call_from_thread(self._apply_data, data)
            except Exception as e:
                logger.warning("Poll error: {}", e)
                self.call_from_thread(self._show_conn_error, str(e))
                _time.sleep(5)
                try:
                    self._rig.reconnect()
                except Exception:
                    pass
            _time.sleep(self._interval)

    @work(thread=False, exclusive=False, name="conn-refresh")
    async def _start_conn_refresh(self) -> None:
        """Periodically refresh the connection bar (slower than poll)."""
        import asyncio
        while True:
            await asyncio.sleep(2.0)
            self._refresh_conn_bar()

    def _do_poll(self) -> dict:
        rig = self._rig
        pos = rig.get_position()
        gps_src = "rig"
        if pos is None and self._gps_fallback:
            try:
                pos = self._gps_fallback.get_position()
                gps_src = "fallback"
            except Exception:
                pass
        if pos is None and self._static_pos:
            pos = self._static_pos
            gps_src = "static"

        result: dict = {
            "pos": pos,
            "gps_src": gps_src,
            "source_label": str(rig),
        }
        if pos is not None:
            result["grid"] = maidenhead(pos.lat, pos.lon)

        mode, passband = rig.get_mode_and_passband()
        result["freq"] = rig.get_frequency()
        result["mode"] = mode
        result["passband"] = passband
        result["ptt"] = rig.get_ptt()

        if self._meters_enabled:
            m: dict[str, float] = {}
            strength = rig.get_strength()
            if strength is not None:
                m["STRENGTH"] = strength
            m.update(rig.get_meters(levels=["ALC", "SWR", "RFPOWER_METER", "COMP_METER"]))
            rfpower = rig.get_level("RFPOWER")
            if rfpower is not None:
                m["RFPOWER"] = rfpower
            result["meters"] = m

        return result

    def _apply_data(self, data: dict) -> None:
        pos: Position | None = data.get("pos")
        grid: str = data.get("grid", "")
        freq = data.get("freq")
        mode = data.get("mode")
        passband = data.get("passband")
        ptt: bool | None = data.get("ptt")
        meters: dict[str, float] = data.get("meters") or {}
        gps_src: str = data.get("gps_src", "")
        source_label: str = data.get("source_label", "")

        # TX hold
        mono = _time.monotonic()
        if ptt is True:
            self._tx_hold_meters = dict(meters)
            self._tx_hold_until = mono + 3.0
        elif ptt is False and mono < self._tx_hold_until:
            ptt = True
            meters = self._tx_hold_meters

        # Watchdog
        if ptt:
            if self._tx_start is None:
                self._tx_start = mono
            tx_dur = mono - self._tx_start
            if (not self._wd_tripped and self._watchdog
                    and tx_dur >= self._watchdog.tx_timeout):
                self._wd_tripped = True
                self._update_title()
                logger.critical("TX WATCHDOG fired after {:.0f}s", tx_dur)
                self._rig.set_ptt(False)
                self.notify("TX watchdog — PTT forced OFF", title="⚠ Watchdog", severity="error")
        else:
            if self._prev_ptt and self._tx_start is not None:
                logger.info("TX ended after {:.1f}s", mono - self._tx_start)
            self._tx_start = None
            if ptt is False and self._wd_tripped:
                self._wd_tripped = False
                self._update_title()
        self._prev_ptt = bool(ptt)

        # Snapshot for :info
        self._last_info = {
            "source": source_label, "freq": freq or "",
            "mode": mode or "", "grid": grid, "gps": gps_src,
        }

        # Update widgets
        self.query_one(RigPanel).render_data(
            freq, mode, passband, ptt, meters, self._rig_name, self._wd_tripped,
        )
        self.query_one(StationPanel).render_data(
            pos, grid, gps_src, self._start_time, source_label,
        )

        # Update IS badge
        aprs_is_connected = any(
            type(s).__name__ == "AprsIsSink" and s.connected for s in self._sinks
        )
        if aprs_is_connected != self._aprs_is:
            self._aprs_is = aprs_is_connected
            self._update_title()

        # DW running badge — update on state change OR config change
        dw_now = self._dw_launcher is not None and self._dw_launcher.running
        dw_cfg = (self._dw_launcher.active_config or "") if self._dw_launcher else ""
        if dw_now != self._dw_running or dw_cfg != self._dw_active_config:
            self._dw_running = dw_now
            self._dw_active_config = dw_cfg
            self._update_title()
            self._update_dw_title()
            dw_log = self.query_one("#dw-log", RichLog)
            dw_log.styles.border = ("round", "green" if dw_now else "grey50")


        # Also call send() on peer sinks (nmea, wsjtx, aprsis, etc.)
        if pos is not None:
            extras = {
                "source_label": source_label, "gps_src": gps_src,
                "freq": freq, "mode": mode, "passband": passband,
                "ptt": ptt, "meters": meters,
            }
            for sink in self._sinks:
                if not getattr(sink, "tui", False):
                    try:
                        sink.send(pos, grid, **extras)
                    except Exception as e:
                        logger.warning("Sink {} error: {}", sink, e)

    def _refresh_conn_bar(self) -> None:
        self.query_one(ConnectionBar).render_data(
            self._sinks, self._dw_launcher, self._dw_client
        )

    def _show_conn_error(self, msg: str) -> None:
        self.notify(f"rigctld: {msg}", title="Connection error", severity="warning")

    # ── Command execution ────────────────────────────────────────────────────

    def execute_command(self, raw: str) -> None:
        parts = raw.strip().split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        dispatch = {
            "aprs":   self._cmd_aprs,
            "aprsis": self._cmd_aprsis,
            "packet": self._cmd_packet,
            "wsjtx":  self._cmd_wsjtx,
            "nmea":   self._cmd_nmea,
            "civ":    self._cmd_civ,
            "data":   self._cmd_data,
            "dw":     self._cmd_dw,
            "igate":  self._cmd_igate,
            "freq":   self._cmd_freq,
            "mode":   self._cmd_mode,
            "msg":    self._cmd_msg,
            "info":   lambda _: self._cmd_info(),
            "help":   lambda _: self._cmd_help(),
            "scan":   lambda _: self._cmd_scan(),
            "q":      lambda _: self.action_quit(),
            "quit":   lambda _: self.action_quit(),
        }
        fn = dispatch.get(cmd)
        if fn:
            fn(args)
        else:
            self.notify(f"Unknown command: {cmd}", severity="warning")

    # ── Command helpers ──────────────────────────────────────────────────────

    def _find_aprs_sinks(self) -> list:
        names = {"AprsIsSink", "NmeaSink"}
        return [s for s in self._sinks if type(s).__name__ in names]

    def _cmd_aprs(self, args: list[str]) -> None:
        sinks = self._find_aprs_sinks()
        if not sinks:
            self.notify("No APRS sinks configured", severity="warning")
            return
        if not args:
            parts = [f"{s}={'ON' if s.connected else 'OFF'}" for s in sinks]
            self.notify(f"APRS: {', '.join(parts)}")
            return
        action = args[0].lower()
        if action == "on":
            if self._rig is not None:
                try:
                    self._saved_freq = int(self._rig.get_frequency() or 0) or None
                except (ValueError, TypeError):
                    self._saved_freq = None
                self._saved_mode = self._rig.get_mode()
            qsy: list[str] = []
            if self._rig and self._aprs_config:
                if self._aprs_config.freq > 0:
                    if self._rig.set_freq(int(self._aprs_config.freq * 1e6)):
                        qsy.append(f"{self._aprs_config.freq:.3f} MHz")
                if self._aprs_config.qsy_mode:
                    if self._rig.set_mode(self._aprs_config.qsy_mode):
                        qsy.append(self._aprs_config.qsy_mode)
            for s in sinks:
                if not s.connected:
                    s.start()
            self._aprs_active = True
            self._update_title()
            self._start_direwolf("aprs")
            msg = "APRS ON"
            if qsy:
                msg += f" ({', '.join(qsy)})"
            self.notify(msg, title="APRS")
        elif action == "off":
            for s in sinks:
                s.close()
            self._aprs_active = False
            self._update_title()
            restored: list[str] = []
            if self._rig:
                if self._saved_freq:
                    if self._rig.set_freq(self._saved_freq):
                        restored.append(f"{self._saved_freq / 1e6:.6f} MHz")
                    self._saved_freq = None
                if self._saved_mode:
                    if self._rig.set_mode(self._saved_mode):
                        restored.append(self._saved_mode)
                    self._saved_mode = None
            self._stop_direwolf()
            msg = "APRS OFF"
            if restored:
                msg += f" — restored {', '.join(restored)}"
            self.notify(msg, title="APRS")
        else:
            self.notify("Usage: aprs [on|off]", severity="warning")

    def _cmd_packet(self, args: list[str]) -> None:
        cfg = self._packet_config
        if cfg is None:
            self.notify("No [bbs] config section", severity="warning")
            return
        if not args:
            state = "ON" if self._packet_active else "OFF"
            self.notify(f"Packet: {state}  ({cfg.freq:.3f} MHz {cfg.mode})")
            return
        action = args[0].lower()
        if action == "on":
            qsy = []
            if self._rig:
                if not self._packet_active:
                    try:
                        self._saved_freq = int(self._rig.get_frequency() or 0) or None
                    except (ValueError, TypeError):
                        self._saved_freq = None
                    self._saved_mode = self._rig.get_mode()
                if self._rig.set_freq(int(cfg.freq * 1e6)):
                    qsy.append(f"{cfg.freq:.3f} MHz")
                if self._rig.set_mode(cfg.mode):
                    qsy.append(cfg.mode)
            self._packet_active = True
            self._update_title()
            self._start_direwolf("packet")
            self.notify(f"Packet ON ({', '.join(qsy)})", title="Packet")
        elif action == "off":
            restored = []
            if self._rig:
                if self._saved_freq:
                    if self._rig.set_freq(self._saved_freq):
                        restored.append(f"{self._saved_freq / 1e6:.6f} MHz")
                    self._saved_freq = None
                if self._saved_mode:
                    if self._rig.set_mode(self._saved_mode):
                        restored.append(self._saved_mode)
                    self._saved_mode = None
            self._packet_active = False
            self._update_title()
            self._stop_direwolf()
            msg = "Packet OFF"
            if restored:
                msg += f" — restored {', '.join(restored)}"
            self.notify(msg, title="Packet")
        else:
            self.notify("Usage: packet [on|off]", severity="warning")

    _ACTIVE_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"ready", "open", "listening", "receiving"}
    )

    def _sink_is_active(self, sink) -> bool:
        """Return True if sink is currently running, using connections() or connected."""
        if hasattr(sink, "connections"):
            conns = sink.connections()
            return bool(conns and conns[0].get("status") in self._ACTIVE_STATUSES)
        return bool(getattr(sink, "connected", False))

    def _cmd_sink_toggle(self, sink_type: str, label: str, args: list[str]) -> None:
        """Generic on/off toggle for a single-instance sink."""
        sinks = [s for s in self._sinks if type(s).__name__ == sink_type]
        if not sinks:
            key = sink_type.replace("Sink", "").lower()
            self.notify(
                f"{label} not in config — add [[sinks]] type = \"{key}\" to rigtop.toml",
                severity="warning",
            )
            return
        sink = sinks[0]
        if not args:
            conns = sink.connections() if hasattr(sink, "connections") else []
            status = conns[0].get("status", "?") if conns else "?"
            self.notify(f"{label}: {status}")
            return
        action = args[0].lower()
        if action == "on":
            if self._sink_is_active(sink):
                self.notify(f"{label} already running")
                return
            try:
                sink.start()
                self.notify(f"{label} ON", title=label)
            except Exception as e:
                self.notify(str(e), title=f"{label} error", severity="error")
        elif action == "off":
            sink.close()
            self.notify(f"{label} OFF", title=label)
        else:
            self.notify(f"Usage: {label.lower()} [on|off]", severity="warning")

    def _cmd_wsjtx(self, args: list[str]) -> None:
        self._cmd_sink_toggle("WsjtxSink", "WSJT-X", args)

    def _cmd_nmea(self, args: list[str]) -> None:
        self._cmd_sink_toggle("NmeaSink", "NMEA", args)

    def _cmd_aprsis(self, args: list[str]) -> None:
        self._cmd_sink_toggle("AprsIsSink", "APRS-IS", args)

    def _cmd_civ(self, args: list[str]) -> None:
        self._cmd_sink_toggle("CivProxySink", "CI-V", args)

    _DATA_MODE_MAP: ClassVar[dict[str, str]] = {
        "FM": "PKTFM", "USB": "PKTUSB", "LSB": "PKTLSB",
    }
    _DATA_MODE_REVERSE: ClassVar[dict[str, str]] = {
        v: k for k, v in _DATA_MODE_MAP.items()
    }

    def _cmd_data(self, args: list[str]) -> None:
        if self._rig is None:
            self.notify("No rig connection", severity="error")
            return
        if not args:
            self.notify("Usage: data [on|off]", severity="warning")
            return
        action = args[0].lower()
        mode = self._rig.get_mode() or ""
        if action == "on":
            target = self._DATA_MODE_MAP.get(mode)
            if target is None:
                self.notify(f"No data mode for {mode}", severity="warning")
                return
            if self._rig.set_mode(target):
                self.notify(f"Data mode ON → {target}")
            else:
                self.notify(f"Failed to set {target}", severity="error")
        elif action == "off":
            base = self._DATA_MODE_REVERSE.get(mode)
            if base is None:
                self.notify(f"{mode} is not a data mode", severity="warning")
                return
            if self._rig.set_mode(base):
                self.notify(f"Data mode OFF → {base}")
            else:
                self.notify(f"Failed to set {base}", severity="error")
        else:
            self.notify("Usage: data [on|off]", severity="warning")

    def _cmd_dw(self, args: list[str]) -> None:
        lnchr = self._dw_launcher
        if lnchr is None:
            self.notify("Direwolf not configured", severity="warning")
            return
        if not args:
            state = "running" if lnchr.running else "stopped"
            cfg = lnchr.active_config or "—"
            self.notify(f"Direwolf: {state} ({cfg})")
            return
        profile = args[0].lower()
        conf = lnchr.install_path / f"direwolf-{profile}.conf"
        if not conf.is_file():
            self.notify(f"Config not found: {conf}", severity="error")
            return
        self._start_direwolf(profile)

    def _cmd_igate(self, args: list[str]) -> None:
        sinks = [s for s in self._sinks if type(s).__name__ == "AprsIsSink"]
        if not sinks:
            self.notify("No APRS-IS sink configured", severity="warning")
            return
        if not args:
            state = "ON" if sinks[0].connected else "OFF"
            self.notify(f"IGate: {state}")
            return
        action = args[0].lower()
        if action == "on":
            for s in sinks:
                if not s.connected:
                    s.start()
            self.notify("IGate ON")
        elif action == "off":
            for s in sinks:
                s.close()
            self.notify("IGate OFF")
        else:
            self.notify("Usage: igate [on|off]", severity="warning")

    def _cmd_freq(self, args: list[str]) -> None:
        if self._rig is None:
            self.notify("No rig", severity="error")
            return
        if not args:
            f = self._rig.get_frequency()
            if f:
                self.notify(f"Freq: {float(f) / 1e6:.6f} MHz")
            return
        raw = args[0].replace(",", ".")
        try:
            val = float(raw)
            hz = int(val * 1e6) if val < 1000 else int(val)
        except ValueError:
            self.notify(f"Invalid frequency: {raw}", severity="error")
            return
        if self._rig.set_freq(hz):
            self.notify(f"Freq → {hz / 1e6:.6f} MHz")
        else:
            self.notify("Failed to set frequency", severity="error")

    def _cmd_mode(self, args: list[str]) -> None:
        if self._rig is None:
            self.notify("No rig", severity="error")
            return
        if not args:
            m = self._rig.get_mode()
            self.notify(f"Mode: {m or '?'}")
            return
        mode = args[0].upper()
        pb = int(args[1]) if len(args) > 1 else 0
        if self._rig.set_mode(mode, pb):
            self.notify(f"Mode → {mode}" + (f" ({pb} Hz)" if pb else ""))
        else:
            self.notify(f"Failed to set mode {mode}", severity="error")

    def _cmd_msg(self, args: list[str]) -> None:
        if len(args) < 2:
            self.notify("Usage: msg <CALL> <text>", severity="warning")
            return
        sinks = [s for s in self._sinks if type(s).__name__ == "AprsIsSink"]
        if not sinks:
            self.notify("No APRS-IS sink", severity="warning")
            return
        dest, text = args[0].upper(), " ".join(args[1:])
        sinks[0].send_message(dest, text)
        self.notify(f"→ {dest}: {text}")

    def _cmd_info(self) -> None:
        i = self._last_info
        if not i:
            self.notify("No rig data yet", severity="warning")
            return
        parts = [v for v in [i.get("source"), i.get("mode"), i.get("grid")] if v]
        if i.get("freq"):
            try:
                parts.insert(1, f"{float(i['freq']) / 1e6:.6f} MHz")
            except (ValueError, TypeError):
                pass
        self.notify("  ".join(parts) if parts else "No info")

    def _cmd_help(self) -> None:
        cmds = (
            "aprs [on|off]", "aprsis [on|off]", "packet [on|off]",
            "wsjtx [on|off]", "nmea [on|off]", "civ [on|off]",
            "data [on|off]", "dw [aprs]", "freq <MHz>", "igate [on|off]",
            "mode <MODE>", "msg <CALL> <text>", "info", "scan", "q",
        )
        self.notify("  ".join(cmds), title="Commands", timeout=8)

    def _cmd_scan(self) -> None:
        def _run() -> None:
            from rigtop.discovery import format_results, scan_lan
            results = scan_lan()
            self.call_from_thread(
                self.notify, format_results(results)[:200], "Scan", 10,
            )
        threading.Thread(target=_run, daemon=True).start()
        self.notify("Scanning LAN…")

    # ── Direwolf helpers ─────────────────────────────────────────────────────

    def _start_direwolf(self, profile: str) -> None:
        lnchr = self._dw_launcher
        if lnchr is None:
            return
        conf = lnchr.install_path / f"direwolf-{profile}.conf"
        if not conf.is_file():
            self.notify(f"Config not found: {conf}", severity="error")
            return
        self.notify(f"Direwolf starting ({profile})…")

        def _do() -> None:
            try:
                lnchr.switch_config(f"direwolf-{profile}.conf")
                if not lnchr.running:
                    lnchr.start()
            except (FileNotFoundError, RuntimeError) as e:
                self.call_from_thread(
                    self.notify, str(e), "Direwolf error", 6,
                )

        threading.Thread(target=_do, daemon=True).start()

    def _stop_direwolf(self) -> None:
        lnchr = self._dw_launcher
        if lnchr and lnchr.running:
            lnchr.stop()

    # ── Textual actions ──────────────────────────────────────────────────────

    def action_quit(self) -> None:
        self.exit()

    def action_clear_input(self) -> None:
        self.query_one("#cmd-input", Input).clear()

    def action_show_help(self) -> None:
        self._cmd_help()

    @on(Input.Submitted, "#cmd-input")
    def handle_command(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.clear()
        if cmd in ("q", "quit"):
            self.action_quit()
            return
        if cmd:
            self.execute_command(cmd)


# ── Thin PositionSink stub (kept for sink registry) ─────────────────────────

@register_sink("tui")
class TuiSink(PositionSink):
    """Sentinel sink — the real TUI is RigtopApp, started by cli.py."""

    tui = True

    def __init__(self, **kwargs) -> None:  # absorb type/enabled from SinkConfig
        pass

    def start(self) -> None:
        pass

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        return None

    def close(self) -> None:
        pass
