"""Position sink: live terminal dashboard using rich."""

from __future__ import annotations

import datetime
import time as _time
from collections import deque
from typing import ClassVar

from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from rigtop.geo import format_position
from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position


class AprsBuffer:
    """Ring buffer of incoming APRS-IS packets for a dedicated TUI pane."""

    #: Path tokens that indicate the packet was heard on RF and gated
    _RF_TOKENS: ClassVar[set[str]] = {"qAR", "qAr", "qAo", "qAO"}

    def __init__(self, maxlen: int = 200) -> None:
        self._lines: deque[tuple[str, str]] = deque(maxlen=maxlen)  # (source, formatted)

    @staticmethod
    def _classify(raw: str) -> str:
        """Return ``'rf'`` if the packet was gated from RF, else ``'is'``."""
        # APRS-IS format: CALL>PATH:payload — check the path portion
        if ">" in raw:
            header = raw.split(":", 1)[0]  # everything before payload
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
            if source == "rf-local":
                style = "yellow"
            elif source == "rf":
                style = "green"
            else:
                style = "cyan"
            txt.append(f" {line}", style=style)
            if i < len(tail) - 1:
                txt.append("\n")
        return txt


class MessageBuffer:
    """Ring buffer of APRS messages (sent and received) for TUI display."""

    def __init__(self, maxlen: int = 100) -> None:
        # (direction, callsign, formatted_line)
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
        """Update a sent message to show ack status."""
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


class DirewolfBuffer:
    """Ring buffer of Direwolf stdout/stderr lines for TUI display."""

    def __init__(self, maxlen: int = 200) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)

    def push(self, line: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._lines.append(f"{ts}  {line}")

    def render(self, max_lines: int = 10) -> Text:
        tail = list(self._lines)[-max_lines:]
        txt = Text()
        if not tail:
            txt.append(" (no output)", style="dim")
            return txt
        for i, line in enumerate(tail):
            # Colour-code: errors red, warnings yellow, rest dim
            low = line.lower()
            if "error" in low or "fatal" in low:
                style = "bold red"
            elif "warning" in low:
                style = "yellow"
            else:
                style = "dim white"
            txt.append(f" {line}", style=style)
            if i < len(tail) - 1:
                txt.append("\n")
        return txt


METER_ORDER = [
    "STRENGTH",
    "RFPOWER",
    "ALC",
    "SWR",
    "RFPOWER_METER",
    "COMP_METER",
    "ID_METER",
    "VD_METER",
]

METER_LABELS = {
    "STRENGTH": "S-meter",
    "RFPOWER": "TX pwr",
    "ALC": "ALC",
    "SWR": "SWR",
    "RFPOWER_METER": "Pwr out",
    "COMP_METER": "Comp",
    "ID_METER": "Id",
    "VD_METER": "Vd",
}


def _s_meter_text(db: float) -> str:
    """Convert S-meter dB-relative-to-S9 to readable string."""
    if db <= -54:
        return "S0"
    if db >= 0:
        return f"S9+{db:.0f}"
    s_unit = max(0, int((db + 54) / 6))
    return f"S{s_unit}"


def _meter_bar(name: str, value: float, width: int = 20) -> Text:
    """Render a single meter as a labeled bar."""
    # Normalise to 0..1
    if name == "STRENGTH":
        norm = (value + 54) / 114  # -54 .. +60 → 0 .. 1
    elif name == "SWR":
        norm = min((value - 1.0) / 4.0, 1.0)  # 1 .. 5 → 0 .. 1
    else:
        norm = value  # already 0 .. 1

    norm = max(0.0, min(1.0, norm))
    filled = int(norm * width)
    empty = width - filled

    bar = "█" * filled + "░" * empty

    # Colour coding
    if name == "SWR":
        colour = "green" if value < 2.0 else ("yellow" if value < 3.0 else "red")
    elif name == "ALC":
        colour = "green" if value < 0.5 else ("yellow" if value < 0.8 else "red")
    elif name == "STRENGTH":
        colour = "green" if value > -24 else ("yellow" if value > -42 else "dim")
    else:
        colour = "cyan"

    # Value text
    if name == "STRENGTH":
        val_str = f"{_s_meter_text(value)} ({value:+.0f} dB)"
    elif name == "SWR":
        val_str = f"{value:.1f}:1"
    elif name == "RFPOWER":
        val_str = f"{value * 100:.0f}%"
    else:
        val_str = f"{value:.2f}"

    label = METER_LABELS.get(name, name)
    line = Text()
    line.append(f"  {label:<8} ", style="bold")
    line.append(bar, style=colour)
    line.append(f"  {val_str}")

    # Warning tags for dangerous values
    if (name == "SWR" and value >= 3.0) or (name == "ALC" and value >= 0.8):
        line.append("  ⚠ HIGH", style="bold red blink")

    return line


@register_sink("tui")
class TuiSink(PositionSink):
    """Full-screen live dashboard using rich."""

    # Sentinel so app.py can detect TUI mode
    tui = True

    # Known commands and their arguments for tab completion
    _COMMANDS: ClassVar[dict[str, list[str]]] = {
        "aprs": ["on", "off"],
        "bbs": ["on", "off"],
        "data": ["on", "off"],
        "dw": ["aprs", "bbs"],
        "freq": [],
        "help": [],
        "igate": ["on", "off"],
        "info": [],
        "mode": [
            "USB",
            "LSB",
            "FM",
            "AM",
            "CW",
            "CWR",
            "RTTY",
            "RTTYR",
            "PKTUSB",
            "PKTLSB",
            "PKTFM",
        ],
        "msg": [],
        "q": [],
        "quit": [],
        "scan": [],
    }

    def __init__(self) -> None:
        self._console = Console()
        self._live: Live | None = None
        self.aprs_buffer: AprsBuffer | None = None
        self.msg_buffer: MessageBuffer | None = None
        self.peers: list = []  # sibling sinks (set by main.py)
        self.rig = None  # RigctldSource reference (set by main.py)
        self.rig_name: str = ""  # radio name from config (set by main.py)
        self.aprs_config = None  # AprsConfig reference (set by main.py)
        self.bbs_config = None  # BbsConfig reference (set by cli.py)
        self.dw_launcher = None  # DirewolfLauncher reference (set by cli.py)
        self.dw_buffer: DirewolfBuffer | None = None  # Direwolf output (set by cli.py)
        self._saved_freq: int | None = None  # freq before :aprs/:bbs on
        self._saved_mode: str | None = None  # mode before :aprs/:bbs on
        self._bbs_active: bool = False
        self._start_time: float = _time.monotonic()
        # Command input state (k9s-style)
        self.command_mode: bool = False
        self.command_buf: str = ""
        self._status_msg: str = ""
        self._status_style: str = "green"
        self._status_until: float = 0
        self._last_parts: list | None = None  # cached content for refresh
        self._last_title: str = ""
        self._last_border: str = "blue"
        # Snapshot of last-known rig state for :info
        self._last_info: dict[str, str] = {}
        # TX watchdog state
        self._wd_tripped: bool = False
        # TX hold — keep showing TX state briefly after PTT drops
        self._tx_hold_until: float = 0.0  # monotonic deadline
        self._tx_hold_meters: dict[str, float] = {}  # last TX meters
        self._tx_hold_secs: float = 3.0  # how long to hold

    def start(self) -> None:
        self._live = Live(
            console=self._console,
            refresh_per_second=2,
            screen=True,
        )
        self._live.start()
        # Show splash while waiting for first poll
        splash = Panel(
            Text.from_markup("\n[bold cyan]rigtop[/bold cyan]\n\n[dim]Connecting to rig…[/dim]\n"),
            border_style="blue",
            expand=True,
        )
        self._live.update(splash)

    # ── Command handling ──

    def _build_layout(self, content_parts: list, title: str, border_style: str) -> Panel:
        """Build the outer panel with command bar inserted above Connections pane."""
        cmd_bar = self._render_command_bar()
        parts = [content_parts[0], cmd_bar, *content_parts[1:]]
        return Panel(
            Group(*parts),
            title=title,
            border_style=border_style,
        )

    def refresh_command_bar(self) -> None:
        """Rebuild and push the display with a fresh command bar."""
        if self._live and self._last_parts is not None:
            outer = self._build_layout(
                self._last_parts,
                self._last_title,
                self._last_border,
            )
            self._live.update(outer)

    def tab_complete(self) -> None:
        """Cycle through completions for the current command buffer."""
        buf = self.command_buf
        parts = buf.split()
        if not parts:
            # Complete command name from empty
            first = sorted(self._COMMANDS)
            if first:
                self.command_buf = first[0] + " "
            return
        if len(parts) == 1 and not buf.endswith(" "):
            # Completing command name
            prefix = parts[0].lower()
            matches = [c for c in sorted(self._COMMANDS) if c.startswith(prefix)]
            if len(matches) == 1:
                self.command_buf = matches[0] + " "
            return
        # Completing argument (second word)
        cmd = parts[0].lower()
        arg_prefix = parts[1].upper() if len(parts) > 1 else ""
        candidates = self._COMMANDS.get(cmd, [])
        matches = [a for a in candidates if a.startswith(arg_prefix)]
        if len(matches) == 1:
            self.command_buf = f"{parts[0]} {matches[0]}"

    def _get_completions(self) -> list[str]:
        """Return completion candidates for the current command buffer."""
        buf = self.command_buf
        parts = buf.split()
        if not parts or (len(parts) == 1 and not buf.endswith(" ")):
            prefix = parts[0].lower() if parts else ""
            return [c for c in sorted(self._COMMANDS) if c.startswith(prefix)]
        cmd = parts[0].lower()
        arg_prefix = parts[1].upper() if len(parts) > 1 else ""
        candidates = self._COMMANDS.get(cmd, [])
        return [a for a in candidates if a.startswith(arg_prefix)]

    def execute_command(self, raw: str) -> None:
        """Parse and execute a colon-command (e.g. ':log DEBUG')."""
        parts = raw.strip().split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "aprs":
            self._cmd_aprs(args)
        elif cmd == "bbs":
            self._cmd_bbs(args)
        elif cmd == "data":
            self._cmd_data(args)
        elif cmd == "dw":
            self._cmd_dw(args)
        elif cmd == "igate":
            self._cmd_igate(args)
        elif cmd == "help":
            self._cmd_help()
        elif cmd == "info":
            self._cmd_info()
        elif cmd == "mode":
            self._cmd_mode(args)
        elif cmd == "freq":
            self._cmd_freq(args)
        elif cmd == "msg":
            self._cmd_msg(args)
        elif cmd == "scan":
            self._cmd_scan()
        else:
            self._set_status(f"Unknown command: {cmd}", style="red")

    def _cmd_help(self) -> None:
        cmds = (
            ":aprs [on|off] – toggle APRS-IS",
            ":bbs [on|off] – QSY to packet BBS freq/mode",
            ":data [on|off] – toggle data mode (FM↔PKTFM, USB↔PKTUSB)",
            ":dw [aprs|bbs] – Direwolf status / switch profile",
            ":freq <Hz|MHz> – set frequency",
            ":help – show this list",
            ":info – rig connection & status",
            ":mode <MODE> – set rig mode (FM, USB, …)",
            ":msg <CALL> <text> – send APRS message",
            ":scan – scan LAN for radios",
            ":q / :quit – exit rigtop",
        )
        self._set_status("  ".join(cmds), style="cyan", duration=8.0)

    def _cmd_info(self) -> None:
        i = self._last_info
        if not i:
            self._set_status("No rig data yet", style="yellow")
            return
        parts = []
        if i.get("source"):
            parts.append(i["source"])
        if i.get("freq"):
            parts.append(f"{float(i['freq']) / 1e6:.6f} MHz")
        if i.get("mode"):
            parts.append(i["mode"])
        if i.get("grid"):
            parts.append(f"Grid {i['grid']}")
        if i.get("gps"):
            parts.append(f"GPS:{i['gps']}")
        self._set_status("  ".join(parts) if parts else "No info", style="cyan", duration=6.0)

    # Mapping: base mode → data (PKT) mode
    _DATA_MODE_MAP: ClassVar[dict[str, str]] = {
        "FM": "PKTFM",
        "USB": "PKTUSB",
        "LSB": "PKTLSB",
    }
    _DATA_MODE_REVERSE: ClassVar[dict[str, str]] = {v: k for k, v in _DATA_MODE_MAP.items()}

    def _cmd_bbs(self, args: list[str]) -> None:
        """QSY to packet BBS frequency/mode: :bbs on / :bbs off."""
        if self.rig is None:
            self._set_status("No rig connection", style="red")
            return
        # Defaults if no [bbs] config section
        bbs_freq = 144.675
        bbs_mode = "PKTFM"
        if self.bbs_config is not None:
            bbs_freq = self.bbs_config.freq
            bbs_mode = self.bbs_config.mode
        if not args:
            state = "ON" if self._bbs_active else "OFF"
            self._set_status(
                f"BBS: {state}  ({bbs_freq:.3f} MHz {bbs_mode})",
                style="cyan",
            )
            return
        action = args[0].lower()
        if action == "on":
            # Save current freq/mode only on first activation
            if not self._bbs_active:
                try:
                    freq_str = self.rig.get_frequency()
                    self._saved_freq = int(freq_str) if freq_str else None
                except ValueError, TypeError:
                    self._saved_freq = None
                self._saved_mode = self.rig.get_mode()
            # QSY (always — allows re-send if radio drifted)
            qsy_parts: list[str] = []
            freq_hz = int(bbs_freq * 1e6)
            if self.rig.set_freq(freq_hz):
                qsy_parts.append(f"{bbs_freq:.3f} MHz")
            if self.rig.set_mode(bbs_mode):
                qsy_parts.append(bbs_mode)
            self._bbs_active = True
            # Start Direwolf with BBS profile
            self._start_direwolf("bbs")
            self._set_status(f"BBS ON ({', '.join(qsy_parts)})")
        elif action == "off":
            if not self._bbs_active:
                self._set_status("BBS already OFF", style="yellow")
                return
            # Restore previous freq/mode
            restored: list[str] = []
            if self._saved_freq is not None:
                if self.rig.set_freq(self._saved_freq):
                    restored.append(f"{self._saved_freq / 1e6:.6f} MHz")
                self._saved_freq = None
            if self._saved_mode is not None:
                if self.rig.set_mode(self._saved_mode):
                    restored.append(self._saved_mode)
                self._saved_mode = None
            self._bbs_active = False
            # Stop Direwolf
            self._stop_direwolf()
            status = "BBS OFF"
            if restored:
                status += f" — restored {', '.join(restored)}"
            self._set_status(status)
        else:
            self._set_status("Usage: :bbs [on|off]", style="red")

    # ── Direwolf helpers ──

    def _start_direwolf(self, profile: str) -> None:
        """Start Direwolf with the given profile (aprs or bbs)."""
        lnchr = self.dw_launcher
        if lnchr is None:
            return
        config_file = f"direwolf-{profile}.conf"
        try:
            lnchr.switch_config(config_file)
            if not lnchr.running:
                lnchr.start()
        except (FileNotFoundError, RuntimeError) as e:
            self._set_status(f"Direwolf: {e}", style="red")

    def _stop_direwolf(self) -> None:
        """Stop Direwolf if running."""
        lnchr = self.dw_launcher
        if lnchr is None:
            return
        if lnchr.running:
            lnchr.stop()

    def _cmd_data(self, args: list[str]) -> None:
        if self.rig is None:
            self._set_status("No rig connection", style="red")
            return
        current = self.rig.get_mode()
        if not current:
            self._set_status("Cannot read current mode", style="red")
            return
        is_data = current in self._DATA_MODE_REVERSE
        if not args:
            self._set_status(f"Data mode: {'ON' if is_data else 'OFF'} ({current})", style="cyan")
            return
        action = args[0].lower()
        if action == "on":
            if is_data:
                self._set_status(f"Data already ON ({current})", style="yellow")
                return
            target = self._DATA_MODE_MAP.get(current)
            if not target:
                self._set_status(f"No data mode for {current}", style="red")
                return
            if self.rig.set_mode(target):
                self._set_status(f"Data ON: {current} → {target}")
            else:
                self._set_status(f"Failed to set {target}", style="red")
        elif action == "off":
            if not is_data:
                self._set_status(f"Data already OFF ({current})", style="yellow")
                return
            target = self._DATA_MODE_REVERSE[current]
            if self.rig.set_mode(target):
                self._set_status(f"Data OFF: {current} → {target}")
            else:
                self._set_status(f"Failed to set {target}", style="red")
        else:
            self._set_status("Usage: :data [on|off]", style="red")

    def _cmd_dw(self, args: list[str]) -> None:
        """Direwolf status / switch profile: :dw, :dw aprs, :dw bbs."""
        lnchr = self.dw_launcher
        if lnchr is None:
            self._set_status("Direwolf launcher not configured", style="yellow")
            return
        if not args:
            state = "running" if lnchr.running else "stopped"
            self._set_status(
                f"Direwolf: {state} — config: {lnchr.active_config}",
                style="cyan",
            )
            return
        profile = args[0].lower()
        config_file = f"direwolf-{profile}.conf"
        try:
            lnchr.switch_config(config_file)
            self._set_status(f"Direwolf → {config_file}")
        except FileNotFoundError as e:
            self._set_status(str(e), style="red")
        except RuntimeError as e:
            self._set_status(f"Direwolf restart failed: {e}", style="red")

    def _cmd_mode(self, args: list[str]) -> None:
        if self.rig is None:
            self._set_status("No rig connection", style="red")
            return
        if not args:
            mode = self.rig.get_mode()
            self._set_status(f"Current mode: {mode or '?'}", style="cyan")
            return
        mode = args[0].upper()
        passband = int(args[1]) if len(args) > 1 else 0
        if self.rig.set_mode(mode, passband):
            self._set_status(f"Mode → {mode}" + (f" ({passband} Hz)" if passband else ""))
        else:
            self._set_status(f"Failed to set mode {mode}", style="red")

    def _find_aprs_sinks(self) -> list:
        """Return all APRS-related peers (aprsis + nmea sinks)."""
        names = {"AprsIsSink", "NmeaSink"}
        return [p for p in self.peers if type(p).__name__ in names]

    @property
    def _aprs_active(self) -> bool:
        """True if any APRS-related sink is connected."""
        return any(s.connected for s in self._find_aprs_sinks())

    def _build_title(self, source_label: str = "") -> str:
        """Build the outer panel title with optional APRS ON badges."""
        title = "[bold]rigtop[/bold]"
        if source_label:
            title += f"  [dim]{source_label}[/dim]"
        # Per-type APRS badges: one RF (nmea) and one IS (aprs-is)
        rf_on = False
        is_state = None  # None | "receiving" | "connected"
        for sink in self._find_aprs_sinks():
            name = type(sink).__name__
            if name == "NmeaSink" and sink.connected:
                rf_on = True
            elif name == "AprsIsSink" and sink.connected:
                if sink.receiving:
                    is_state = "receiving"
                elif is_state is None:
                    is_state = "connected"
        if rf_on:
            title += "  [bold white on red] RF [/bold white on red]"
        if is_state == "receiving":
            title += "  [bold white on green] IS [/bold white on green]"
        elif is_state == "connected":
            title += "  [bold white on yellow] IS [/bold white on yellow]"
        if self._wd_tripped:
            title += "  [bold white on red blink] WD [/bold white on red blink]"
        if getattr(self, "_bbs_active", False):
            title += "  [bold white on magenta] BBS [/bold white on magenta]"
        return title

    def _cmd_aprs(self, args: list[str]) -> None:
        sinks = self._find_aprs_sinks()
        if not sinks:
            self._set_status("No APRS sinks configured", style="red")
            return
        if not args:
            parts = []
            for s in sinks:
                state = "ON" if s.connected else "OFF"
                parts.append(f"{s}={state}")
            self._set_status(f"APRS: {', '.join(parts)}", style="cyan")
            return
        action = args[0].lower()
        if action == "on":
            # Save current freq/mode so :aprs off can restore them
            if self.rig is not None:
                try:
                    freq_str = self.rig.get_frequency()
                    self._saved_freq = int(freq_str) if freq_str else None
                except ValueError, TypeError:
                    self._saved_freq = None
                self._saved_mode = self.rig.get_mode()
            # QSY to APRS frequency + mode (from [aprs] config)
            qsy_parts: list[str] = []
            if self.rig is not None and self.aprs_config is not None:
                if self.aprs_config.freq > 0:
                    freq_hz = int(self.aprs_config.freq * 1e6)
                    if self.rig.set_freq(freq_hz):
                        qsy_parts.append(f"{self.aprs_config.freq:.3f} MHz")
                if self.aprs_config.qsy_mode:
                    target_mode = self.aprs_config.qsy_mode
                    # Ensure data mode is off (use base mode, not PKT variant)
                    base = self._DATA_MODE_REVERSE.get(target_mode, target_mode)
                    if self.rig.set_mode(base):
                        qsy_parts.append(base)
            # Start APRS sinks
            started = []
            for s in sinks:
                if not s.connected:
                    s.start()
                    started.append(str(s))
            status = "APRS ON"
            if qsy_parts:
                status += f" ({', '.join(qsy_parts)})"
            if started:
                status += f" — started {', '.join(started)}"
            # Start Direwolf with APRS profile
            self._start_direwolf("aprs")
            self._set_status(status)
        elif action == "off":
            for s in sinks:
                s.close()
            # Restore previous freq/mode
            restored: list[str] = []
            if self.rig is not None:
                if self._saved_freq is not None:
                    if self.rig.set_freq(self._saved_freq):
                        restored.append(f"{self._saved_freq / 1e6:.6f} MHz")
                    self._saved_freq = None
                if self._saved_mode is not None:
                    if self.rig.set_mode(self._saved_mode):
                        restored.append(self._saved_mode)
                    self._saved_mode = None
            status = f"APRS stopped ({len(sinks)} sink{'s' if len(sinks) != 1 else ''})"
            # Stop Direwolf
            self._stop_direwolf()
            if restored:
                status += f" — restored {', '.join(restored)}"
            self._set_status(status)
        else:
            self._set_status("Usage: :aprs [on|off]", style="red")

    def _cmd_igate(self, args: list[str]) -> None:
        """Toggle APRS-IS gateway sink only."""
        sinks = [p for p in self.peers if type(p).__name__ == "AprsIsSink"]
        if not sinks:
            self._set_status("No APRS-IS sink configured", style="red")
            return
        if not args:
            parts = []
            for s in sinks:
                state = "ON" if s.connected else "OFF"
                parts.append(f"{s}={state}")
            self._set_status(f"iGate: {', '.join(parts)}", style="cyan")
            return
        action = args[0].lower()
        if action == "on":
            started = []
            for s in sinks:
                if not s.connected:
                    s.start()
                    started.append(str(s))
            if started:
                self._set_status(f"iGate started: {', '.join(started)}")
            else:
                self._set_status("iGate already running", style="yellow")
        elif action == "off":
            for s in sinks:
                s.close()
            self._set_status("iGate stopped")
        else:
            self._set_status("Usage: :igate [on|off]", style="red")

    def _cmd_freq(self, args: list[str]) -> None:
        if self.rig is None:
            self._set_status("No rig connection", style="red")
            return
        if not args:
            freq = self.rig.get_frequency()
            if freq:
                self._set_status(f"Current freq: {float(freq) / 1e6:.6f} MHz", style="cyan")
            else:
                self._set_status("No frequency data", style="yellow")
            return
        try:
            val = float(args[0])
        except ValueError:
            self._set_status(f"Invalid frequency: {args[0]}", style="red")
            return
        # If value looks like MHz (< 1e6), convert to Hz
        if val < 1e6:
            freq_hz = int(val * 1e6)
        else:
            freq_hz = int(val)
        if self.rig.set_freq(freq_hz):
            self._set_status(f"Freq → {freq_hz / 1e6:.6f} MHz")
        else:
            self._set_status(f"Failed to set freq {freq_hz} Hz", style="red")

    def _cmd_msg(self, args: list[str]) -> None:
        """Send an APRS message: :msg CALLSIGN some text here."""
        if len(args) < 2:
            self._set_status("Usage: :msg <CALL> <text>", style="red")
            return
        aprsis = [s for s in self.peers if type(s).__name__ == "AprsIsSink"]
        if not aprsis:
            self._set_status("No APRS-IS sink configured", style="red")
            return
        sink = aprsis[0]
        if not sink.connected:
            self._set_status("APRS-IS not connected – use :aprs on", style="red")
            return
        dest = args[0].upper()
        text = " ".join(args[1:])
        if len(text) > 67:
            self._set_status("Message too long (max 67 chars)", style="red")
            return
        msgno = sink.send_message(dest, text)
        if msgno:
            self._set_status(f"Msg to {dest} sent {{{msgno}}}")
        else:
            self._set_status("Failed to send message", style="red")

    def _cmd_scan(self) -> None:
        """Scan LAN for radio services (runs in background thread)."""
        import threading

        from rigtop.discovery import scan_lan

        self._set_status("Scanning LAN…", style="yellow", duration=30.0)

        def _bg():
            results = scan_lan(timeout=0.3)
            if not results:
                self._set_status("No radio services found on LAN", duration=5.0)
                return
            parts = []
            for r in results:
                svc = r["service"]
                parts.append(f"{r['host']}:{r['port']} ({svc})")
            msg = f"Found: {',  '.join(parts)}"
            self._set_status(msg, style="cyan", duration=10.0)

        threading.Thread(target=_bg, daemon=True, name="lan-scan").start()

    def _set_status(self, msg: str, style: str = "green", duration: float = 3.0) -> None:
        self._status_msg = msg
        self._status_style = style
        self._status_until = _time.monotonic() + duration

    def _render_command_bar(self) -> Text:
        """Render k9s-style command/status bar.

        Normal mode:  hotkey hints like  <:>command  <ctrl-c>quit
        Command mode: prompt with input, completions, and helper keys
        Status flash: temporary message from a command result
        """
        bar = Text()
        if self.command_mode:
            bar.append(" :", style="bold white on grey30")
            bar.append(self.command_buf, style="bold white on grey30")
            bar.append("█", style="bold white on grey30 blink")
            # Completion hints
            completions = self._get_completions()
            if completions:
                bar.append("  ", style="on grey30")
                for i, c in enumerate(completions[:6]):
                    if i > 0:
                        bar.append(" ", style="on grey30")
                    bar.append(c, style="dim white on grey30")
            # Helper keys on the right
            bar.append("  ", style="on grey30")
            bar.append("<tab>", style="bold cyan on grey30")
            bar.append("complete ", style="dim on grey30")
            bar.append("<esc>", style="bold cyan on grey30")
            bar.append("cancel", style="dim on grey30")
            bar.append(" " * 200, style="on grey30")
        elif self._status_msg and _time.monotonic() < self._status_until:
            bar.append(f" {self._status_msg}", style=f"{self._status_style} on grey23")
            bar.append(" " * 200, style="on grey23")
        else:
            # k9s-style hotkey hints
            bar.append(" ", style="on grey23")
            hints = [
                (":", "command"),
                ("ctrl-c", "quit"),
            ]
            for key, label in hints:
                bar.append(f"<{key}>", style="bold cyan on grey23")
                bar.append(f"{label} ", style="dim on grey23")
            bar.append(" " * 200, style="on grey23")
        return bar

    def show_alert(self, message: str, detail: str = "") -> None:
        """Display a full-screen alert (e.g. connection lost)."""
        now = datetime.datetime.now().strftime("%H:%M:%S")

        alert = Text()
        alert.append("\n")
        alert.append("  ⚠  ", style="bold red blink")
        alert.append(f"{message}\n\n", style="bold red")
        if detail:
            alert.append(f"  {detail}\n", style="dim")
        alert.append("\n  Reconnecting…\n", style="yellow")
        alert.append(f"\n  {now}", style="dim")

        alert_panel = Panel(
            alert,
            title="[bold red]CONNECTION LOST[/bold red]",
            border_style="red",
            expand=True,
        )

        parts: list = [alert_panel]
        if self.aprs_buffer is not None:
            aprs_text = self.aprs_buffer.render(max_lines=8)
            parts.append(
                Panel(
                    aprs_text,
                    title="[bold]APRS-IS[/bold]",
                    border_style="cyan",
                    expand=True,
                )
            )
        alert_title = self._build_title()
        outer = self._build_layout(parts, alert_title, "red")

        if self._live:
            self._last_parts = parts
            self._last_title = alert_title
            self._last_border = "red"
            self._live.update(outer, refresh=False)

    def show_watchdog_alert(self, tx_duration: float, tx_timeout: int) -> None:
        """Display a full-screen TX watchdog alert."""
        self._wd_tripped = True
        now = datetime.datetime.now().strftime("%H:%M:%S")

        alert = Text()
        alert.append("\n")
        alert.append("  ⚠  ", style="bold red blink")
        alert.append("TX WATCHDOG TRIPPED\n\n", style="bold red")
        alert.append(
            f"  Radio was transmitting for {tx_duration:.0f}s (limit {tx_timeout}s)\n",
            style="bold yellow",
        )
        alert.append("  PTT forced OFF — radio returned to RX\n\n", style="bold green")
        alert.append(f"  {now}", style="dim")

        alert_panel = Panel(
            alert,
            title="[bold red]TX WATCHDOG[/bold red]",
            border_style="red",
            expand=True,
        )

        parts: list = [alert_panel]

        wd_title = self._build_title()
        outer = self._build_layout(parts, wd_title, "red")

        if self._live:
            self._last_parts = parts
            self._last_title = wd_title
            self._last_border = "red"
            self._live.update(outer, refresh=False)

    def _render_connections(self) -> Panel | None:
        """Build the Connections panel in two columns."""
        if not self.peers:
            return None
        all_conns = []
        for peer in self.peers:
            all_conns.extend(peer.connections())
        if not all_conns:
            return None

        def _fmt(c: dict) -> Text:
            status = c.get("status", "?")
            kind = c.get("kind", "")
            label = c.get("label", "?")
            clients = c.get("clients", [])
            t = Text()
            if status in ("open", "receiving"):
                t.append("● ", style="bold green")
            elif status == "listening":
                t.append("● ", style="bold cyan")
            elif status == "ready":
                t.append("● ", style="bold yellow")
            else:
                t.append("○ ", style="dim red")
            t.append(label, style="bold")
            t.append(f"  {kind}", style="dim")
            t.append(f"  {status}", style="dim")
            if isinstance(clients, int):
                if clients:
                    t.append(f"  ({clients} pkts)", style="dim")
            elif clients:
                t.append(f"  ({len(clients)})" if kind == "tcp" else "")
                for addr in clients:
                    t.append(f"\n  └ {addr}", style="dim")
            return t

        serial = [c for c in all_conns if c.get("kind") == "serial"]
        net = [c for c in all_conns if c.get("kind") != "serial"]
        left_items = [_fmt(c) for c in serial]
        right_items = [_fmt(c) for c in net]
        left = Text()
        for i, t in enumerate(left_items):
            left.append_text(t)
            if i < len(left_items) - 1:
                left.append("\n")
        right = Text()
        for i, t in enumerate(right_items):
            right.append_text(t)
            if i < len(right_items) - 1:
                right.append("\n")

        cols = (
            Columns(
                [left, right],
                equal=True,
                expand=True,
            )
            if right.plain.strip()
            else left
        )

        return Panel(
            cols,
            title="[bold]Connections[/bold]",
            border_style="magenta",
            expand=True,
        )

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        freq: str | None = kwargs.get("freq")
        mode: str | None = kwargs.get("mode")
        passband: int | None = kwargs.get("passband")
        ptt: bool | None = kwargs.get("ptt")
        meters: dict[str, float] = kwargs.get("meters") or {}
        source_label: str = kwargs.get("source_label", "")
        gps_src: str = kwargs.get("gps_src", "")
        now = datetime.datetime.now().strftime("%H:%M:%S")

        # TX hold: when PTT drops, hold TX display for a few seconds
        mono = _time.monotonic()
        if ptt is True:
            # Currently transmitting — snapshot meters for hold
            self._tx_hold_meters = dict(meters)
            self._tx_hold_until = mono + self._tx_hold_secs
        elif ptt is False and mono < self._tx_hold_until:
            # PTT just dropped but hold is active — show as TX with last meters
            ptt = True
            meters = self._tx_hold_meters

        # Clear watchdog badge once radio is back to RX
        if ptt is False and self._wd_tripped:
            self._wd_tripped = False

        # Snapshot for :info command
        self._last_info = {
            "source": source_label,
            "freq": freq or "",
            "mode": mode or "",
            "grid": grid,
            "gps": gps_src,
        }

        # ── Left pane: Station ──
        left = Text()
        if self.rig_name:
            left.append(f" {self.rig_name}\n", style="bold cyan")
        left.append(f" {format_position(pos.lat, pos.lon)}\n", style="bold white")
        left.append(f" {pos.lat:.6f}, {pos.lon:.6f}\n")
        left.append(" Grid  ", style="dim")
        left.append(f"{grid}\n", style="bold green")
        if pos.alt is not None:
            left.append(" Alt   ", style="dim")
            left.append(f"{pos.alt:.0f} m\n", style="bold")
        if gps_src:
            left.append(" GPS   ", style="dim")
            left.append(f"{gps_src}\n", style="bold" if gps_src == "rig" else "yellow")
        # Uptime
        uptime_s = int(_time.monotonic() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        left.append(" Up    ", style="dim")
        left.append(f"{h}:{m:02d}:{s:02d}\n", style="dim")
        left.append(f" {now}", style="dim")

        left_panel = Panel(
            left,
            title="[bold]Station[/bold]",
            border_style="green",
            expand=True,
        )

        # ── Right pane: Rig & Meters ──
        right = Text()

        # PTT indicator
        if ptt is True:
            if mono < self._tx_hold_until and kwargs.get("ptt") is False:
                # Held TX display (ptt was overridden by hold logic)
                hold_left = self._tx_hold_until - mono
                right.append(f" ● TX  (hold {hold_left:.0f}s)\n", style="bold red")
            else:
                right.append(" ● TX\n", style="bold red")
        elif ptt is False:
            right.append(" ● RX\n", style="bold green")

        # Frequency / mode / passband
        if freq or mode:
            freq_str = f"{float(freq) / 1e6:.6f} MHz" if freq else "—"
            mode_str = mode or "—"
            if passband and passband > 0:
                mode_str += f" ({passband} Hz)"
            right.append(f" {freq_str}   {mode_str}\n", style="bold yellow")
        right.append("\n")

        # Meter bars
        if meters:
            for name in METER_ORDER:
                if name in meters:
                    right.append_text(_meter_bar(name, meters[name]))
                    right.append("\n")
        else:
            right.append(" No meter data", style="dim")

        # Border turns red on TX with high SWR or ALC
        warn = ptt is True and (meters.get("SWR", 0) >= 3.0 or meters.get("ALC", 0) >= 0.8)
        right_panel = Panel(
            right,
            title="[bold]Rig / Meters[/bold]",
            border_style="red bold" if warn else "cyan",
            expand=True,
        )

        # ── Combine side by side: Rig | GPS ──
        title = self._build_title(source_label)
        top_row = Columns([right_panel, left_panel], equal=True, expand=True)

        # ── Connections (full-width row) ──
        conn_panel = self._render_connections()

        # ── APRS pane ──
        aprs_panel = None
        if self.aprs_buffer is not None:
            aprsis = [s for s in self.peers if type(s).__name__ == "AprsIsSink"]
            rx_info = ""
            if aprsis and aprsis[0].rx_count:
                rx_info += f"  IS:{aprsis[0].rx_count}"
            dw = [s for s in self.peers if type(s).__name__ == "DirewolfClient"]
            if dw and dw[0].rx_count:
                rx_info += f"  RF:{dw[0].rx_count}"
            aprs_text = self.aprs_buffer.render(max_lines=12)
            aprs_panel = Panel(
                aprs_text,
                title=f"[bold]APRS[/bold][dim]{rx_info}[/dim]",
                border_style="cyan",
                expand=True,
            )

        # ── Messages pane ──
        msg_panel = None
        if self.msg_buffer is not None:
            unread = self.msg_buffer.unread
            badge = f"  [bold yellow]({unread} new)[/bold yellow]" if unread else ""
            msg_text = self.msg_buffer.render(max_lines=8)
            msg_panel = Panel(
                msg_text,
                title=f"[bold]Messages[/bold]{badge}",
                border_style="yellow" if unread else "cyan",
                expand=True,
            )

        # ── Direwolf output pane ──
        dw_panel = None
        if self.dw_buffer is not None:
            lnchr = self.dw_launcher
            dw_state = ""
            if lnchr is not None:
                state = "running" if lnchr.running else "stopped"
                cfg = lnchr.active_config or "—"
                dw_state = f"  {state} ({cfg})"
            dw_text = self.dw_buffer.render(max_lines=8)
            dw_panel = Panel(
                dw_text,
                title=f"[bold]Direwolf[/bold][dim]{dw_state}[/dim]",
                border_style="green",
                expand=True,
            )

        parts: list = [top_row]
        if conn_panel is not None:
            parts.append(conn_panel)
        if aprs_panel is not None:
            parts.append(aprs_panel)
        if msg_panel is not None:
            parts.append(msg_panel)
        if dw_panel is not None:
            parts.append(dw_panel)

        outer = self._build_layout(parts, title, "blue")

        if self._live:
            self._last_parts = parts
            self._last_title = title
            self._last_border = "blue"
            self._live.update(outer, refresh=False)

        return None

    def close(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def __str__(self) -> str:
        return "tui"
