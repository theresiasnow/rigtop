"""Position sink: live terminal dashboard using rich."""

from __future__ import annotations

import datetime
import time as _time
from collections import deque

from loguru import logger
from rich.console import Console, Group
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from rigtop.geo import format_position
from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position


# ── Loguru sink that buffers records for the TUI ──

_LOG_STYLES = {
    "TRACE": "dim",
    "DEBUG": "dim cyan",
    "INFO": "green",
    "SUCCESS": "bold green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


# Numeric ordering for level filtering
_LEVEL_ORDER = {
    "TRACE": 0,
    "DEBUG": 1,
    "INFO": 2,
    "SUCCESS": 3,
    "WARNING": 4,
    "ERROR": 5,
    "CRITICAL": 6,
}


class TuiLogBuffer:
    """Ring buffer of ``(level, line)`` tuples for the TUI log pane.

    Accepts both loguru ``message`` objects (via :meth:`write`) and raw
    text lines (via :meth:`push_line`).
    """

    def __init__(self, maxlen: int = 500) -> None:
        self._records: deque[tuple[str, str]] = deque(maxlen=maxlen)
        self.min_level: str = "DEBUG"  # render-time filter

    # -- loguru sink interface --
    def write(self, message) -> None:
        record = message.record
        level = record["level"].name
        ts = record["time"].strftime("%H:%M:%S")
        mod = record["name"] or ""
        text = record["message"]
        self._records.append((level, f"{ts}  {level:<8} {mod} — {text}"))

    # -- raw text (e.g. rigctld stderr) --
    def push_line(self, line: str, level: str = "DEBUG") -> None:
        """Append a plain-text line tagged with *level*."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._records.append((level, f"{ts}  {line}"))

    def render(self, max_lines: int = 12) -> Text:
        """Return a rich Text with the most recent log lines, coloured by level."""
        threshold = _LEVEL_ORDER.get(self.min_level, 0)
        visible = [
            (lvl, line)
            for lvl, line in self._records
            if _LEVEL_ORDER.get(lvl, 0) >= threshold
        ]
        tail = visible[-max_lines:]
        txt = Text()
        if not tail:
            txt.append(" (no log messages)", style="dim")
            return txt
        for i, (level, line) in enumerate(tail):
            style = _LOG_STYLES.get(level, "")
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
    if name == "SWR" and value >= 3.0:
        line.append("  ⚠ HIGH", style="bold red blink")
    elif name == "ALC" and value >= 0.8:
        line.append("  ⚠ HIGH", style="bold red blink")

    return line


@register_sink("tui")
class TuiSink(PositionSink):
    """Full-screen live dashboard using rich."""

    # Sentinel so app.py can detect TUI mode
    tui = True

    # Known commands and their arguments for tab completion
    _COMMANDS: dict[str, list[str]] = {
        "clear": [],
        "help": [],
        "info": [],
        "log": ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
        "q": [],
        "quit": [],
    }

    def __init__(self) -> None:
        self._console = Console()
        self._live: Live | None = None
        self.log_buffer: TuiLogBuffer | None = None
        self.peers: list = []  # sibling sinks (set by main.py)
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

    def start(self) -> None:
        self._live = Live(
            console=self._console,
            refresh_per_second=2,
            screen=True,
        )
        self._live.start()

    # ── Command handling ──

    def _build_layout(self, content_parts: list, title: str, border_style: str) -> Panel:
        """Build the outer panel with command bar inserted before the log pane."""
        # Insert command bar before the last item (log pane) if present,
        # otherwise append at the end.
        parts = list(content_parts)
        cmd_bar = self._render_command_bar()
        if self.log_buffer is not None and len(parts) >= 2:
            # parts = [top_row, log_panel] → insert bar between them
            parts.insert(-1, cmd_bar)
        else:
            parts.append(cmd_bar)
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

    _VALID_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}

    def execute_command(self, raw: str) -> None:
        """Parse and execute a colon-command (e.g. ':log DEBUG')."""
        parts = raw.strip().split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "log":
            self._cmd_log(args)
        elif cmd == "help":
            self._cmd_help()
        elif cmd == "info":
            self._cmd_info()
        elif cmd == "clear":
            self._cmd_clear()
        else:
            self._set_status(f"Unknown command: {cmd}", style="red")

    def _cmd_log(self, args: list[str]) -> None:
        if self.log_buffer is None:
            self._set_status("No log buffer attached", style="red")
            return
        if not args:
            self._set_status(f"Log level: {self.log_buffer.min_level}", style="cyan")
            return
        level = args[0].upper()
        if level not in self._VALID_LEVELS:
            self._set_status(
                f"Invalid level '{args[0]}'. Use: {', '.join(sorted(self._VALID_LEVELS))}",
                style="red",
            )
            return
        self.log_buffer.min_level = level
        self._set_status(f"Log level → {level}")

    def _cmd_help(self) -> None:
        cmds = (
            ":clear – clear log buffer",
            ":help – show this list",
            ":info – rig connection & status",
            ":log [LEVEL] – show/set log filter level",
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

    def _cmd_clear(self) -> None:
        if self.log_buffer is None:
            self._set_status("No log buffer attached", style="red")
            return
        self.log_buffer._records.clear()
        self._set_status("Log cleared")

    def _set_status(self, msg: str, style: str = "green", duration: float = 3.0) -> None:
        self._status_msg = msg
        self._status_style = style
        self._status_until = _time.monotonic() + duration

    def _render_command_bar(self) -> Text:
        """Render the command input bar with completion hints.

        Uses a light background so the bar is always clearly visible.
        """
        bar = Text()
        if self.command_mode:
            bar.append(" :", style="bold black on grey82")
            bar.append(self.command_buf, style="bold black on grey82")
            bar.append("█", style="bold black on grey82 blink")
            # Pad to fill width then add hints
            completions = self._get_completions()
            hints = ""
            if completions:
                hints = "   " + "  ".join(completions)
            hints += "   Tab complete  Esc cancel"
            bar.append(hints, style="black on grey82")
            # Fill rest of line with background
            bar.append(" " * 200, style="on grey82")
        elif self._status_msg and _time.monotonic() < self._status_until:
            bar.append(f" {self._status_msg}", style=f"{self._status_style} on grey23")
            bar.append(" " * 200, style="on grey23")
        else:
            bar.append(" :help  :info  :log  :q", style="dim on grey23")
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
        if self.log_buffer is not None:
            term_h = self._console.size.height
            log_lines = max(3, term_h - 19)
            log_text = self.log_buffer.render(max_lines=log_lines)
            log_panel = Panel(
                log_text,
                title="[bold]Log[/bold]",
                border_style="dim",
                expand=True,
            )
            parts.append(log_panel)

        outer = self._build_layout(parts, "[bold]rigtop[/bold]", "red")

        if self._live:
            self._last_parts = parts
            self._last_title = "[bold]rigtop[/bold]"
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
            if status == "open":
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

            if clients:
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

        # Snapshot for :info command
        self._last_info = {
            "source": source_label,
            "freq": freq or "",
            "mode": mode or "",
            "grid": grid,
            "gps": gps_src,
        }

        # ── Left pane: GPS ──
        left = Text()
        left.append(f" {format_position(pos.lat, pos.lon)}\n", style="bold white")
        left.append(f" {pos.lat:.6f}, {pos.lon:.6f}\n")
        left.append(f" Grid  ", style="dim")
        left.append(f"{grid}\n", style="bold green")
        if gps_src:
            left.append(f" GPS   ", style="dim")
            left.append(f"{gps_src}\n", style="bold" if gps_src == "rig" else "yellow")
        left.append(f"\n {now}", style="dim")

        left_panel = Panel(
            left,
            title="[bold]GPS[/bold]",
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

        # ── Combine side by side ──
        title = "[bold]rigtop[/bold]"
        if source_label:
            title += f"  [dim]{source_label}[/dim]"

        top_row = Columns([right_panel, left_panel], equal=True, expand=True)

        # ── Middle pane: Connections ──
        conn_panel = self._render_connections()

        # ── Bottom pane: Log messages (fills remaining terminal height) ──
        parts: list = [top_row]
        if conn_panel is not None:
            parts.append(conn_panel)
        if self.log_buffer is not None:
            # Top row panels ~14 rows, outer border 2, log border 2, cmd bar 1.
            term_h = self._console.size.height
            conn_overhead = 4 + len(self.peers) * 2 if conn_panel else 0
            log_lines = max(3, term_h - 19 - conn_overhead)
            log_text = self.log_buffer.render(max_lines=log_lines)
            log_panel = Panel(
                log_text,
                title="[bold]Log[/bold]",
                border_style="dim",
                expand=True,
            )
            parts.append(log_panel)

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
