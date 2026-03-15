"""Position sink: live terminal dashboard using rich."""

from __future__ import annotations

import datetime

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from nmead.geo import format_position
from nmead.sinks import PositionSink, register_sink
from nmead.sources import Position

METER_ORDER = [
    "STRENGTH",
    "ALC",
    "SWR",
    "RFPOWER_METER",
    "COMP_METER",
    "ID_METER",
    "VD_METER",
]

METER_LABELS = {
    "STRENGTH": "S-meter",
    "ALC": "ALC",
    "SWR": "SWR",
    "RFPOWER_METER": "Power",
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
    else:
        val_str = f"{value:.2f}"

    label = METER_LABELS.get(name, name)
    line = Text()
    line.append(f"  {label:<8} ", style="bold")
    line.append(bar, style=colour)
    line.append(f"  {val_str}")
    return line


@register_sink("tui")
class TuiSink(PositionSink):
    """Full-screen live dashboard using rich."""

    # Sentinel so app.py can detect TUI mode
    tui = True

    def __init__(self) -> None:
        self._console = Console()
        self._live: Live | None = None

    def start(self) -> None:
        self._live = Live(
            console=self._console,
            refresh_per_second=2,
            screen=False,
        )
        self._live.start()

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        freq: str | None = kwargs.get("freq")
        mode: str | None = kwargs.get("mode")
        meters: dict[str, float] = kwargs.get("meters") or {}
        source_label: str = kwargs.get("source_label", "")
        now = datetime.datetime.now().strftime("%H:%M:%S")

        parts: list[Text | str] = []

        # ── Position ──
        parts.append(
            Text(f"  {format_position(pos.lat, pos.lon)}", style="bold white")
        )
        parts.append(f"  {pos.lat:.6f}, {pos.lon:.6f}")
        parts.append(Text(f"  Grid  {grid}", style="bold green"))
        parts.append("")

        # ── Rig info ──
        if freq or mode:
            freq_str = f"{float(freq) / 1e6:.6f} MHz" if freq else "—"
            parts.append(
                Text(f"  {freq_str}   {mode or '—'}", style="bold yellow")
            )
            parts.append("")

        # ── Meters ──
        if meters:
            for name in METER_ORDER:
                if name in meters:
                    parts.append(_meter_bar(name, meters[name]))
            parts.append("")

        # ── Footer ──
        parts.append(Text(f"  {now}", style="dim"))

        # Assemble into a single Text renderable
        body = Text()
        for i, part in enumerate(parts):
            if i > 0:
                body.append("\n")
            if isinstance(part, Text):
                body.append_text(part)
            else:
                body.append(part)

        title = "[bold]nmead[/bold]"
        if source_label:
            title += f"  [dim]{source_label}[/dim]"

        panel = Panel(
            body,
            title=title,
            subtitle="[dim]Ctrl+C to stop[/dim]",
            border_style="blue",
        )

        if self._live:
            self._live.update(panel)

        return None

    def close(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def __str__(self) -> str:
        return "tui"
