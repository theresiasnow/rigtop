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
        norm = (value + 54) / 114          # -54 .. +60 → 0 .. 1
    elif name == "SWR":
        norm = min((value - 1.0) / 4.0, 1.0)  # 1 .. 5 → 0 .. 1
    else:
        norm = value                        # already 0 .. 1

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
        "freq": [],
        "help": [],
        "igate": ["on", "off"],
        "info": [],
        "mode": ["USB", "LSB", "FM", "AM", "CW", "CWR", "RTTY", "RTTYR",
                 "PKTUSB", "PKTLSB", "PKTFM"],
        "q": [],
        "quit": [],
    }

    def __init__(self) -> None:
        self._console = Console()
        self._live: Live | None = None
        self.aprs_buffer: AprsBuffer | None = None
        self.peers: list = []  # sibling sinks (set by main.py)
        self.rig = None  # RigctldSource reference (set by main.py)
        self.rig_name: str = ""  # radio name from config (set by main.py)
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

    def start(self) -> None:
        self._live = Live(
            console=self._console,
            refresh_per_second=2,
            screen=True,
        )
        self._live.start()

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
                self._last_parts, self._last_title, self._last_border,
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
        else:
            self._set_status(f"Unknown command: {cmd}", style="red")

    def _cmd_help(self) -> None:
        cmds = (
            ":aprs [on|off] – toggle APRS-IS",
            ":freq <Hz|MHz> – set frequency",
            ":help – show this list",
            ":info – rig connection & status",
            ":mode <MODE> – set rig mode (FM, USB, …)",
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
            started = []
            for s in sinks:
                if not s.connected:
                    s.start()
                    started.append(str(s))
            if started:
                self._set_status(f"APRS started: {', '.join(started)}")
            else:
                self._set_status("APRS already running", style="yellow")
        elif action == "off":
            for s in sinks:
                s.close()
            self._set_status(f"APRS stopped ({len(sinks)} sink{'s' if len(sinks) != 1 else ''})")
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
            parts.append(Panel(
                aprs_text,
                title="[bold]APRS-IS[/bold]",
                border_style="cyan",
                expand=True,
            ))
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
            f"  Radio was transmitting for {tx_duration:.0f}s "
            f"(limit {tx_timeout}s)\n",
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
        """Build the Connections panel from peer sinks."""
        if not self.peers:
            return None
        all_conns = []
        for peer in self.peers:
            all_conns.extend(peer.connections())
        if not all_conns:
            return None

        txt = Text()
        for i, c in enumerate(all_conns):
            status = c.get("status", "?")
            kind = c.get("kind", "")
            label = c.get("label", "?")
            clients = c.get("clients", [])

            # Status dot
            if status in ("open", "receiving"):
                txt.append(" ● ", style="bold green")
            elif status == "listening":
                txt.append(" ● ", style="bold cyan")
            elif status == "ready":
                txt.append(" ● ", style="bold yellow")
            else:
                txt.append(" ○ ", style="dim red")

            txt.append(label, style="bold")
            txt.append(f"  {kind}", style="dim")
            txt.append(f"  {status}", style="dim")

            if isinstance(clients, int):
                if clients:
                    txt.append(f"  ({clients} pkts)", style="dim")
            elif clients:
                txt.append(f"  ({len(clients)})" if kind == "tcp" else "")
                for addr in clients:
                    txt.append(f"\n     └ {addr}", style="dim")

            if i < len(all_conns) - 1:
                txt.append("\n")

        return Panel(
            txt,
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
        warn = (ptt is True and
                (meters.get("SWR", 0) >= 3.0 or meters.get("ALC", 0) >= 0.8))
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

        parts: list = [top_row]
        if conn_panel is not None:
            parts.append(conn_panel)
        if aprs_panel is not None:
            parts.append(aprs_panel)

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
