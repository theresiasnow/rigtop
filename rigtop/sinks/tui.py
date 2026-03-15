"""Position sink: live terminal dashboard using rich."""

from __future__ import annotations

import datetime
from collections import deque

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


class TuiLogBuffer:
    """Loguru custom sink that keeps the last *maxlen* formatted messages."""

    def __init__(self, maxlen: int = 200) -> None:
        self._records: deque[tuple[str, str]] = deque(maxlen=maxlen)

    def write(self, message) -> None:
        record = message.record
        level = record["level"].name
        ts = record["time"].strftime("%H:%M:%S")
        mod = record["name"] or ""
        text = record["message"]
        self._records.append((level, f"{ts}  {level:<8} {mod} — {text}"))

    def render(self, max_lines: int = 12) -> Text:
        """Return a rich Text with the most recent log lines, coloured by level."""
        txt = Text()
        lines = list(self._records)[-max_lines:]
        if not lines:
            txt.append(" (no log messages)", style="dim")
            return txt
        for i, (level, line) in enumerate(lines):
            style = _LOG_STYLES.get(level, "")
            txt.append(f" {line}", style=style)
            if i < len(lines) - 1:
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

    def __init__(self) -> None:
        self._console = Console()
        self._live: Live | None = None
        self.log_buffer: TuiLogBuffer | None = None

    def start(self) -> None:
        self._live = Live(
            console=self._console,
            refresh_per_second=2,
            screen=True,
        )
        self._live.start()

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
            log_lines = max(3, term_h - 18)
            log_text = self.log_buffer.render(max_lines=log_lines)
            log_panel = Panel(
                log_text,
                title="[bold]Log[/bold]",
                border_style="dim",
                expand=True,
            )
            parts.append(log_panel)

        outer = Panel(
            Group(*parts),
            title="[bold]rigtop[/bold]",
            subtitle="[dim]q to quit[/dim]",
            border_style="red",
        )

        if self._live:
            self._live.update(outer)

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        freq: str | None = kwargs.get("freq")
        mode: str | None = kwargs.get("mode")
        passband: int | None = kwargs.get("passband")
        ptt: bool | None = kwargs.get("ptt")
        meters: dict[str, float] = kwargs.get("meters") or {}
        source_label: str = kwargs.get("source_label", "")
        gps_src: str = kwargs.get("gps_src", "")
        now = datetime.datetime.now().strftime("%H:%M:%S")

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

        # ── Bottom pane: Log messages (fills remaining terminal height) ──
        parts: list = [top_row]
        if self.log_buffer is not None:
            # Top row panels take ~14 rows, outer panel border takes 2,
            # log panel border takes 2.  Use the rest for log lines.
            term_h = self._console.size.height
            log_lines = max(3, term_h - 18)
            log_text = self.log_buffer.render(max_lines=log_lines)
            log_panel = Panel(
                log_text,
                title="[bold]Log[/bold]",
                border_style="dim",
                expand=True,
            )
            parts.append(log_panel)

        outer = Panel(
            Group(*parts),
            title=title,
            subtitle="[dim]q to quit[/dim]",
            border_style="blue",
        )

        if self._live:
            self._live.update(outer)

        return None

    def close(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def __str__(self) -> str:
        return "tui"
