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
from textual.widget import Widget
from textual.widgets import Button, Header, Input, Label, RichLog, Select, Static
from textual.worker import get_current_worker

from rigtop.geo import format_position, maidenhead
from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position
from rigtop.zones import lookup as _zone_lookup

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

    def render(self, max_lines: int = 8, empty_text: str = "(no traffic)") -> Text:
        tail = list(self._lines)[-max_lines:]
        txt = Text()
        if not tail:
            txt.append(f" {empty_text}", style="dim")
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


# ── Rig control rendering ────────────────────────────────────────────────────

#: Continuous levels (0.0-1.0) shown as percentage bars in RigControlPanel
_CTRL_PCT: list[tuple[str, str]] = [
    ("AF",      "Vol"),
    ("RF",      "RF"),
    ("SQL",     "SQL"),
    ("MICGAIN", "Mic"),
    ("RFPOWER", "Pwr"),
]
#: All levels polled from the rig (incl. ATT/PREAMP for RigCommandPanel)
_CTRL_ALL: list[str] = [k for k, _ in _CTRL_PCT] + ["ATT", "PREAMP"]

_CTRL_STEP = 0.05   # 5 % per arrow keypress


def _control_bar(label: str, value: float, width: int = 16, selected: bool = False) -> Text:
    norm  = max(0.0, min(1.0, value))
    filled = int(norm * width)
    bar   = "█" * filled + "░" * (width - filled)
    pct   = f"{value * 100:.0f}%"
    line  = Text()
    arrow = "▶ " if selected else "  "
    line.append(f" {arrow}{label:<5}", style="bold cyan" if selected else "bold")
    line.append(bar, style="bold cyan" if selected else "cyan")
    line.append(f"  {pct:>4}", style="bold" if selected else "")
    return line


class RigControlPanel(Static, can_focus=True):
    """Horizontal continuous-level bars — Tab to focus, ◄► select control, ▲▼ adjust."""

    BINDINGS: ClassVar[list] = [
        Binding("left",   "prev_ctrl",  "Prev",  show=False),
        Binding("right",  "next_ctrl",  "Next",  show=False),
        Binding("up",     "increase",   "+5%",   show=False),
        Binding("down",   "decrease",   "-5%",   show=False),
        Binding("escape", "blur_pane",  "Done",  show=False),
    ]

    def __init__(self, rig, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rig = rig
        self._controls: dict[str, float | None] = {}
        self._available: list[str] = []
        self._sel: int = 0

    def update_rig(self, rig) -> None:
        self._rig = rig

    def render_data(self, controls: dict[str, float | None]) -> None:
        self._controls = controls
        self._available = [k for k, _ in _CTRL_PCT if controls.get(k) is not None]
        if self._sel >= len(self._available):
            self._sel = max(0, len(self._available) - 1)
        self._redraw()

    def _redraw(self) -> None:
        sel_key = self._available[self._sel] if self._available and self.has_focus else None
        txt = Text()
        parts: list[Text] = []
        for key, label in _CTRL_PCT:
            val = self._controls.get(key)
            if val is None:
                continue
            parts.append(_control_bar(label, val, width=8, selected=(sel_key == key)))
        if parts:
            txt.append(" ")
            for i, part in enumerate(parts):
                txt.append_text(part)
                if i < len(parts) - 1:
                    txt.append("   ")
        else:
            txt.append(" No control data", style="dim")
        hint = "  [◄► select · ▲▼ adjust · Esc]" if self.has_focus else ""
        self.border_title = f"Controls{hint}"
        self.update(txt)

    def on_focus(self) -> None:
        self._redraw()

    def on_blur(self) -> None:
        self._redraw()

    def action_prev_ctrl(self) -> None:
        if self._available:
            self._sel = (self._sel - 1) % len(self._available)
            self._redraw()

    def action_next_ctrl(self) -> None:
        if self._available:
            self._sel = (self._sel + 1) % len(self._available)
            self._redraw()

    def action_increase(self) -> None:
        self._adjust(+1)

    def action_decrease(self) -> None:
        self._adjust(-1)

    def action_blur_pane(self) -> None:
        self.blur()

    def _adjust(self, direction: int) -> None:
        if not self._available:
            return
        key = self._available[self._sel]
        current = self._controls.get(key)
        if current is None:
            return
        new_val = round(max(0.0, min(1.0, current + direction * _CTRL_STEP)), 2)
        if self._rig.set_level(key, new_val):
            self._controls[key] = new_val
            self._redraw()


class RigCommandPanel(Widget):
    """Freq step buttons, mode dropdown, ATT/Pre/NB/NR controls."""

    _MODES: ClassVar[list[str]] = [
        "FM", "USB", "LSB", "AM", "CW", "CWR", "PKTFM", "PKTUSB", "PKTLSB",
    ]
    _ATT_STEPS: ClassVar[list[int]] = [0, 6, 12, 18]
    _PRE_STEPS: ClassVar[list[int]] = [0, 10, 20]

    def __init__(self, rig, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rig = rig
        self._freq_hz: int | None = None
        self._att_idx: int = 0
        self._pre_idx: int = 0
        self._nb_on: bool = False
        self._nr_on: bool = False
        self._updating = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="cmd-row"):
            yield Button("◄◄", id="step-m10k", classes="step")
            yield Button("◄",  id="step-m1k",  classes="step")
            yield Label("—", id="freq-lbl")
            yield Button("►",  id="step-p1k",  classes="step")
            yield Button("►►", id="step-p10k", classes="step")
            yield Select([(m, m) for m in self._MODES], id="mode-sel")
            yield Button("ATT: off", id="att-btn", classes="cycle")
            yield Button("Pre: off", id="pre-btn", classes="cycle")
        with Horizontal(classes="cmd-extra"):
            yield Button("NB: off", id="nb-btn", classes="cycle")
            yield Button("NR: off", id="nr-btn", classes="cycle")

    def render_data(
        self,
        freq: str | None,
        mode: str | None,
        controls: dict[str, float | None],
    ) -> None:
        if freq:
            try:
                self._freq_hz = int(float(freq))
                mhz = f"{self._freq_hz / 1e6:.6f} MHz"
            except (ValueError, TypeError):
                mhz = "—"
        else:
            mhz = "—"
        try:
            self.query_one("#freq-lbl", Label).update(mhz)
        except Exception:
            return  # not yet mounted

        self._updating = True
        try:
            if mode and mode in self._MODES:
                self.query_one("#mode-sel", Select).value = mode
        except Exception:
            pass
        finally:
            self._updating = False

        att_raw = controls.get("ATT")
        if att_raw is not None:
            self._att_idx = min(
                range(len(self._ATT_STEPS)),
                key=lambda i: abs(self._ATT_STEPS[i] - att_raw),
            )
            try:
                self.query_one("#att-btn", Button).label = self._att_label()
            except Exception:
                pass

        pre_raw = controls.get("PREAMP")
        if pre_raw is not None:
            self._pre_idx = min(
                range(len(self._PRE_STEPS)),
                key=lambda i: abs(self._PRE_STEPS[i] - pre_raw),
            )
            try:
                self.query_one("#pre-btn", Button).label = self._pre_label()
            except Exception:
                pass

        nb_raw = controls.get("NB")
        if nb_raw is not None:
            self._nb_on = bool(nb_raw)
            try:
                self.query_one("#nb-btn", Button).label = self._nb_label()
            except Exception:
                pass

        nr_raw = controls.get("NR")
        if nr_raw is not None:
            self._nr_on = bool(nr_raw)
            try:
                self.query_one("#nr-btn", Button).label = self._nr_label()
            except Exception:
                pass

    def _att_label(self) -> str:
        v = self._ATT_STEPS[self._att_idx]
        return f"ATT: {v} dB" if v else "ATT: off"

    def _pre_label(self) -> str:
        v = self._PRE_STEPS[self._pre_idx]
        return f"Pre: {v} dB" if v else "Pre: off"

    def _nb_label(self) -> str:
        return "NB: on" if self._nb_on else "NB: off"

    def _nr_label(self) -> str:
        return "NR: on" if self._nr_on else "NR: off"

    @on(Button.Pressed)
    def _handle_button(self, event: Button.Pressed) -> None:
        btn_id = str(event.button.id)
        step_map = {
            "step-m10k": -10_000,
            "step-m1k":  -1_000,
            "step-p1k":  +1_000,
            "step-p10k": +10_000,
        }
        if btn_id in step_map and self._freq_hz is not None:
            self._rig.set_freq(self._freq_hz + step_map[btn_id])
        elif btn_id == "att-btn":
            self._att_idx = (self._att_idx + 1) % len(self._ATT_STEPS)
            if self._rig.set_level("ATT", float(self._ATT_STEPS[self._att_idx])):
                self.query_one("#att-btn", Button).label = self._att_label()
        elif btn_id == "pre-btn":
            self._pre_idx = (self._pre_idx + 1) % len(self._PRE_STEPS)
            if self._rig.set_level("PREAMP", float(self._PRE_STEPS[self._pre_idx])):
                self.query_one("#pre-btn", Button).label = self._pre_label()
        elif btn_id == "nb-btn":
            self._nb_on = not self._nb_on
            if self._rig.set_func("NB", self._nb_on):
                self.query_one("#nb-btn", Button).label = self._nb_label()
            else:
                self._nb_on = not self._nb_on  # revert on failure
        elif btn_id == "nr-btn":
            self._nr_on = not self._nr_on
            if self._rig.set_func("NR", self._nr_on):
                self.query_one("#nr-btn", Button).label = self._nr_label()
            else:
                self._nr_on = not self._nr_on  # revert on failure

    @on(Select.Changed, "#mode-sel")
    def _mode_changed(self, event: Select.Changed) -> None:
        if not self._updating and event.value is not Select.BLANK:
            self._rig.set_mode(str(event.value))


# ── Waterfall panel ─────────────────────────────────────────────────────────

class WaterfallPanel(Static):
    """Equalizer-style waterfall — vertical bars per poll, newest column on the left."""

    # 9-step vertical block characters (0 = empty … 8 = full)
    _VBLOCKS: ClassVar[str] = " ▁▂▃▄▅▆▇█"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: deque[float | None] = deque(maxlen=300)
        self.border_title = "Signal"

    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self) -> None:
        self._redraw()

    def push(self, strength: float | None) -> None:
        self._history.appendleft(strength)
        self._redraw()

    def _redraw(self) -> None:
        w, h = self.size
        if w == 0 or h == 0:
            self.update("")
            return
        width  = max(1, w - 2)   # 1-char border each side
        height = max(1, h - 2)   # 1-char border top and bottom

        cols = list(self._history)[:width]
        while len(cols) < width:
            cols.append(None)

        txt = Text(overflow="fold")
        for r in range(height):
            rbf = height - 1 - r          # rows-from-bottom for this display row
            frac = rbf / max(height - 1, 1)
            for val in cols:
                if val is None:
                    txt.append(" ")
                    continue
                norm = max(0.0, min(1.0, (val + 54) / 114))
                total_sub  = max(1, int(norm * height * 8))  # always at least 1 sub-block
                sub_in_row = max(0, min(8, total_sub - rbf * 8))
                char = self._VBLOCKS[sub_in_row]
                # Classic equalizer gradient: green base → yellow mid → red peak
                if sub_in_row == 0:
                    style = ""
                elif frac < 0.6:
                    style = "green"
                elif frac < 0.85:
                    style = "yellow"
                else:
                    style = "bold red"
                txt.append(char, style=style)
            if r < height - 1:
                txt.append("\n")
        self.update(txt)


# ── Command completion ──────────────────────────────────────────────────────

class CommandSuggester(Suggester):
    """Inline ghost-text completion for rigtop commands."""

    _COMMANDS: ClassVar[dict[str, list[str]]] = {
        "aprs":   ["on", "off"],
        "aprsis": ["on", "off"],
        "packet": ["on", "off"],
        "wsjtx":  ["on", "off"],
        "nmea":   ["on", "off"],
        "gpsd":   ["on", "off"],
        "civ":    ["on", "off"],
        "data":   ["on", "off"],
        "dw":     ["aprs"],
        "freq":   [],
        "help":   [],
        "beacon": ["on", "off"],
        "vol":    [],
        "rf":     [],
        "sql":    [],
        "mic":    [],
        "pwr":    [],
        "att":    ["off", "6", "12", "18"],
        "pre":    ["off", "on", "10", "20"],

        "info":   [],
        "mode":   ["USB", "LSB", "FM", "AM", "CW", "CWR", "PKTUSB", "PKTLSB", "PKTFM"],
        "msg":    ["<CALL> <text>"],
        "send":   ["<CALL> <text>"],
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

class AprsPanel(Static):
    """Bottom-left pane: live RF/APRS traffic from buffer."""

    def render_data(self, buf: AprsBuffer | None, title: str = "RF Traffic") -> None:
        self.border_title = title
        is_aprs = "APRS" in title
        empty = "(no APRS traffic)" if is_aprs else "(no packets received)"
        if buf is None:
            txt = Text()
            txt.append(f" {empty}", style="dim")
            self.update(txt)
        else:
            self.update(buf.render(max_lines=8, empty_text=empty))


class MsgPanel(Static):
    """Bottom-right pane: packet message log (sent + received)."""

    def render_data(self, buf: MessageBuffer | None, title: str = "Messages") -> None:
        unread = f"  {buf.unread} unread" if buf and buf.unread else ""
        self.border_title = f"{title}{unread}"
        if buf is None:
            txt = Text()
            txt.append(" (no messages)", style="dim")
            self.update(txt)
        else:
            self.update(buf.render(max_lines=8))


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
    """Right top pane: GPS position, grid, zones, country, altitude, uptime."""

    def render_data(
        self,
        pos: Position | None,
        grid: str,
        gps_src: str,
        start_time: float,
        location: dict | None = None,
        beacon_enabled: bool | None = None,
    ) -> None:
        COL = 8   # label column width (including trailing space)
        M   = " "  # left margin

        txt = Text()

        def lbl(text: str) -> None:
            txt.append(f"{M}{text:<{COL - 1}} ", style="dim")

        if pos is None:
            txt.append(f"{M}No GPS fix\n", style="yellow")
        else:
            lbl("Pos")
            txt.append(f"{format_position(pos.lat, pos.lon)}\n", style="bold white")

            lbl("Grid")
            txt.append(f"{grid}\n", style="bold green")

            if location:
                cq      = location.get("cq", "?")
                iaru    = location.get("iaru", "?")
                cc      = location.get("cc", "")
                country = location.get("country", "")
                lbl("Zones")
                txt.append(f"CQ {cq}  ITU {iaru}\n", style="cyan")
                if country:
                    lbl("Country")
                    if cc:
                        txt.append(f"{cc}  ", style="bold cyan")
                    txt.append(f"{country}\n", style="cyan")

            if pos.alt is not None:
                lbl("Alt")
                txt.append(f"{pos.alt:.0f} m\n", style="bold")

        lbl("GPS")
        gps_style = "bold" if gps_src == "rig" else ("yellow" if gps_src else "dim")
        txt.append(f"{gps_src or '—'}\n", style=gps_style)

        if beacon_enabled is not None:
            lbl("Beacon")
            if beacon_enabled:
                txt.append("● ON\n", style="bold green")
            else:
                txt.append("○ OFF\n", style="dim red")

        uptime_s = int(_time.monotonic() - start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        lbl("Up")
        txt.append(f"{h:02d}:{m:02d}:{s:02d}\n", style="dim")
        self.update(txt)


class ConnectionBar(Static):
    """Multi-line connection status for all active sinks and sources."""

    _ACTIVE: ClassVar[set[str]] = {"receiving", "open", "listening", "ready"}

    def render_data(self, sinks: list, dw_launcher=None, dw_client=None, rig=None) -> None:
        self.border_title = "Connections"
        lines: list[Text] = []

        if rig is not None and hasattr(rig, "connections"):
            lines.extend(self._fmt_conn(c) for c in rig.connections())

        for sink in sinks:
            if type(sink).__name__ == "TuiSink":
                continue
            if hasattr(sink, "labelled_connections"):
                lines.extend(self._fmt_conn(c) for c in sink.labelled_connections())
            elif hasattr(sink, "connections"):
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

    # Column widths: icon(1) name(14) kind(6) status(11) extra
    _COL_NAME   = 14
    _COL_KIND   =  8
    _COL_STATUS = 11

    def _fmt_conn(self, conn: dict) -> Text:
        status  = conn.get("status", "")
        label   = conn.get("label", "")
        kind    = conn.get("kind", "")
        clients = conn.get("clients", [])
        address = conn.get("address", "")

        active = status in self._ACTIVE
        icon   = "●" if active else "○"
        colour = "green" if active else ("dim red" if status == "closed" else "dim")

        row = Text()
        row.append(f" {icon} ", style=colour)
        # Name column — pad to fixed width
        name_col = label[:self._COL_NAME]
        row.append(f"{name_col:<{self._COL_NAME}}", style="bold" if active else "dim")
        # Kind column
        kind_col = (f"[{kind}]" if kind else "")[:self._COL_KIND]
        row.append(f"   {kind_col:<{self._COL_KIND}}", style="dim")
        # Status column
        status_col = status[:self._COL_STATUS]
        row.append(f"   {status_col:<{self._COL_STATUS}}", style=colour)
        # Extra: address / packet count / connected clients
        if address:
            row.append(f"   {address}", style="dim")
        if isinstance(clients, int) and clients > 0:
            row.append(f"  {clients} pkts", style="dim cyan")
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
    RigControlPanel {
        height: 3;
        border: round $surface;
        padding: 0 1;
    }
    RigControlPanel:focus {
        border: round $accent;
    }
    RigCommandPanel {
        height: 7;
        border: round $surface;
        layout: vertical;
        padding: 0 1;
    }
    RigCommandPanel .cmd-row {
        height: 3;
        align: center middle;
    }
    RigCommandPanel .cmd-extra {
        height: 1;
        align: right middle;
    }
    RigCommandPanel Button {
        height: 1;
        min-width: 6;
        border: none;
        margin: 0 1;
    }
    RigCommandPanel #freq-lbl {
        width: 1fr;
        content-align: center middle;
        text-style: bold;
    }
    RigCommandPanel Select {
        width: 16;
        margin: 0 1;
    }
    WaterfallPanel {
        height: 1fr;
        border: round $surface;
        padding: 0;
    }
    #aprs-row {
        height: 9;
        display: none;
    }
    AprsPanel {
        width: 2fr;
        border: round yellow;
        padding: 0 1;
        height: 100%;
        overflow-y: auto;
    }
    MsgPanel {
        width: 1fr;
        border: round cyan;
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
        self._last_controls: dict[str, float | None] = {}
        self._ctrl_poll_n: int = 0

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top-row"):
            yield RigPanel(id="rig-panel")
            yield StationPanel(id="station-panel")
        yield WaterfallPanel(id="waterfall")
        yield RigControlPanel(self._rig, id="ctrl-panel")
        yield RigCommandPanel(self._rig, id="cmd-panel")
        yield ConnectionBar(id="conn-bar")
        with Horizontal(id="aprs-row"):
            yield AprsPanel(id="aprs-panel")
            yield MsgPanel(id="msg-panel")
        yield RichLog(id="dw-log", highlight=False, markup=False, auto_scroll=True)
        with Horizontal(id="cmd-bar"):
            yield Label("❯ ", id="cmd-prompt")
            yield Input(
                placeholder=(
                    "aprs | aprsis | packet | beacon on/off"
                    "  •  nmea | gpsd | civ | wsjtx  •  q"
                ),
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
            detail = " IG" if self._aprs_is else (" RF" if self._dw_running else "")
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
                if "not running" in str(e).lower():
                    break
                logger.warning("Poll error: {}", e)
                try:
                    self.call_from_thread(self._show_conn_error, str(e))
                except Exception:
                    break
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
            result["location"] = _zone_lookup(pos.lat, pos.lon)

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

        # Read control levels and functions every 5th poll (they change rarely)
        self._ctrl_poll_n = (self._ctrl_poll_n + 1) % 5
        if self._ctrl_poll_n == 0:
            controls = {lvl: rig.get_level(lvl) for lvl in _CTRL_ALL}
            for func in ("NB", "NR"):
                val = rig.get_func(func)
                if val is not None:
                    controls[func] = 1.0 if val else 0.0
            result["controls"] = controls

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
        location: dict | None = data.get("location")

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
        aprsis_sinks = [
            s for s in self._sinks
            if type(s).__name__ == "AprsIsSink" and getattr(s, "enabled", True)
        ]
        if aprsis_sinks:
            s = aprsis_sinks[0]
            beacon_enabled = s._beacon_enabled and s.connected
        else:
            beacon_enabled = None
        self.query_one(StationPanel).render_data(
            pos, grid, gps_src, self._start_time, location, beacon_enabled,
        )

        # Update traffic / messages panes when APRS or packet mode is active
        if self._aprs_active or self._packet_active:
            mode_label = "APRS Traffic" if self._aprs_active else "Packet Traffic"
            self.query_one(AprsPanel).render_data(self._aprs_buffer, title=mode_label)
            self.query_one(MsgPanel).render_data(self._msg_buffer)

        # Update rig control panel
        if "controls" in data:
            self._last_controls = data["controls"]
        self.query_one(RigControlPanel).render_data(self._last_controls)
        self.query_one(RigCommandPanel).render_data(freq, mode, self._last_controls)
        # Only feed the waterfall during RX — S-meter is meaningless while TX
        self.query_one(WaterfallPanel).push(None if ptt else meters.get("STRENGTH"))

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
            dw_log.styles.border = ("round", "green" if dw_now else "grey")


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
            self._sinks, self._dw_launcher, self._dw_client, self._rig
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
            "gpsd":   self._cmd_gpsd,
            "civ":    self._cmd_civ,
            "data":   self._cmd_data,
            "dw":     self._cmd_dw,
            "beacon": self._cmd_beacon,
            "vol":    self._cmd_vol,
            "rf":     self._cmd_rf,
            "sql":    self._cmd_sql,
            "mic":    self._cmd_mic,
            "pwr":    self._cmd_pwr,
            "att":    self._cmd_att,
            "pre":    self._cmd_pre,
            "freq":   self._cmd_freq,
            "mode":   self._cmd_mode,
            "msg":    self._cmd_msg,
            "send":   self._cmd_msg,
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
        return [s for s in self._sinks if type(s).__name__ in names and getattr(s, "enabled", True)]

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
                if self._beacon_disabled:
                    s._beacon_enabled = False
            self._aprs_active = True
            self._update_title()
            self.query_one("#aprs-row").display = True
            self.query_one(AprsPanel).render_data(self._aprs_buffer)
            self.query_one(MsgPanel).render_data(self._msg_buffer)
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
            self.query_one("#aprs-row").display = False
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
            self.query_one("#aprs-row").display = True
            self.query_one(AprsPanel).render_data(self._aprs_buffer, title="Packet Traffic")
            self.query_one(MsgPanel).render_data(self._msg_buffer)
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
            if not self._aprs_active:
                self.query_one("#aprs-row").display = False
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
        sinks = [s for s in self._sinks if type(s).__name__ == "NmeaSink"]
        if not sinks:
            self.notify("No nmea sink configured", severity="warning")
            return
        if not args:
            def _id(s) -> str:
                return s.name or s.device or f"{s.host}:{s.port}"
            parts = [f"{_id(s)}={'ON' if self._sink_is_active(s) else 'OFF'}" for s in sinks]
            self.notify(f"NMEA: {', '.join(parts)}")
            return
        action = args[0].lower()
        if action == "on":
            for s in sinks:
                if not self._sink_is_active(s):
                    s.start()
            self.notify(f"NMEA ON ({len(sinks)} sink{'s' if len(sinks) > 1 else ''})")
        elif action == "off":
            for s in sinks:
                s.close()
            self.notify(f"NMEA OFF ({len(sinks)} sink{'s' if len(sinks) > 1 else ''})")
        else:
            self.notify("Usage: nmea [on|off]", severity="warning")

    def _cmd_gpsd(self, args: list[str]) -> None:
        self._cmd_sink_toggle("GpsdSink", "gpsd", args)

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

    def _cmd_beacon(self, args: list[str]) -> None:
        sinks = [
            s for s in self._sinks
            if type(s).__name__ == "AprsIsSink" and getattr(s, "enabled", True)
        ]
        if not sinks:
            self.notify("No APRS-IS sink configured", severity="warning")
            return
        sink = sinks[0]
        if not args:
            active = sink._beacon_enabled and sink.connected
            state = "ON" if active else ("READY" if sink._beacon_enabled else "OFF")
            self.notify(f"Beacon: {state}  (interval {sink._interval}s)")
            return
        action = args[0].lower()
        if action == "on":
            sink._beacon_enabled = True
            self.notify("Beacon ON — position will be sent to APRS-IS", title="Beacon")
        elif action == "off":
            sink._beacon_enabled = False
            self.notify("Beacon OFF — position not sent to APRS-IS", title="Beacon")
        else:
            self.notify("Usage: beacon [on|off]", severity="warning")

    # ── Rig control commands ──────────────────────────────────────────────────

    def _set_pct_level(self, level: str, label: str, args: list[str]) -> None:
        """Helper for 0-100% level commands (vol, rf, sql, mic, pwr)."""
        if not args:
            val = self._rig.get_level(level)
            if val is not None:
                self.notify(f"{label}: {val * 100:.0f}%")
            else:
                self.notify(f"{label}: not supported by this rig", severity="warning")
            return
        try:
            pct = float(args[0])
            if not 0 <= pct <= 100:
                raise ValueError
        except ValueError:
            self.notify(f"Usage: {label.lower()} 0–100", severity="warning")
            return
        new_val = round(pct / 100, 2)
        if self._rig.set_level(level, new_val):
            self.notify(f"{label}: {pct:.0f}%")
            self._last_controls[level] = new_val
            self.query_one(RigControlPanel).render_data(self._last_controls)
        else:
            self.notify(f"{label}: set failed", severity="error")

    def _cmd_vol(self, args: list[str]) -> None:
        self._set_pct_level("AF", "Vol", args)

    def _cmd_rf(self, args: list[str]) -> None:
        self._set_pct_level("RF", "RF gain", args)

    def _cmd_sql(self, args: list[str]) -> None:
        self._set_pct_level("SQL", "SQL", args)

    def _cmd_mic(self, args: list[str]) -> None:
        self._set_pct_level("MICGAIN", "Mic", args)

    def _cmd_pwr(self, args: list[str]) -> None:
        self._set_pct_level("RFPOWER", "Pwr", args)

    def _cmd_att(self, args: list[str]) -> None:
        if not args:
            val = self._rig.get_level("ATT")
            if val is None:
                self.notify("ATT: not supported by this rig", severity="warning")
            else:
                self.notify(f"ATT: {int(val)} dB" if val > 0 else "ATT: off")
            return
        arg = args[0].lower()
        db = 0 if arg == "off" else None
        if db is None:
            try:
                db = int(arg)
            except ValueError:
                self.notify("Usage: att [off|0|6|12|18]", severity="warning")
                return
        if self._rig.set_level("ATT", float(db)):
            self.notify(f"ATT: {db} dB" if db else "ATT: off")
            self._last_controls["ATT"] = float(db)
            self.query_one(RigControlPanel).render_data(self._last_controls)
            freq = self._last_info.get("freq")
            mode = self._last_info.get("mode")
            self.query_one(RigCommandPanel).render_data(freq, mode, self._last_controls)
        else:
            self.notify("ATT: set failed", severity="error")

    def _cmd_pre(self, args: list[str]) -> None:
        if not args:
            val = self._rig.get_level("PREAMP")
            if val is None:
                self.notify("Pre: not supported by this rig", severity="warning")
            else:
                self.notify(f"Pre: {int(val)} dB" if val > 0 else "Pre: off")
            return
        arg = args[0].lower()
        db = {"off": 0, "on": 10}.get(arg)
        if db is None:
            try:
                db = int(arg)
            except ValueError:
                self.notify("Usage: pre [off|on|0|10|20]", severity="warning")
                return
        if self._rig.set_level("PREAMP", float(db)):
            self.notify(f"Pre: {db} dB" if db else "Pre: off")
            self._last_controls["PREAMP"] = float(db)
            self.query_one(RigControlPanel).render_data(self._last_controls)
            freq = self._last_info.get("freq")
            mode = self._last_info.get("mode")
            self.query_one(RigCommandPanel).render_data(freq, mode, self._last_controls)
        else:
            self.notify("Pre: set failed", severity="error")

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
            if val < 1_000:
                hz = int(val * 1_000_000)   # MHz  e.g. 144.800
            elif val < 1_000_000:
                hz = int(val * 1_000)       # kHz  e.g. 144800
            else:
                hz = int(val)               # Hz   e.g. 144800000
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
        _ALIASES = {"SSB": "USB", "PKT": "PKTUSB", "DIG": "PKTUSB", "DATA": "PKTUSB"}
        mode = _ALIASES.get(args[0].upper(), args[0].upper())
        pb = int(args[1]) if len(args) > 1 else 0
        if self._rig.set_mode(mode, pb):
            self.notify(f"Mode → {mode}" + (f" ({pb} Hz)" if pb else ""))
        else:
            self.notify(f"Failed to set mode {mode}", severity="error")

    def _cmd_msg(self, args: list[str]) -> None:
        if not self._aprs_active:
            self.notify("APRS not active — use :aprs on first", severity="warning")
            return
        if len(args) < 2:
            self.notify("Usage: msg <CALL> <text>", severity="warning")
            return
        sinks = [
            s for s in self._sinks
            if type(s).__name__ == "AprsIsSink" and getattr(s, "enabled", True)
        ]
        if not sinks:
            self.notify("No APRS-IS sink configured", severity="warning")
            return
        dest, text = args[0].upper(), " ".join(args[1:])
        sinks[0].send_message(dest, text)
        if self._msg_buffer:
            self._msg_buffer.push_tx(dest, text)
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
            "beacon [on|off]", "wsjtx [on|off]",
            "nmea [on|off] (NMEA sentences)", "gpsd [on|off] (gpsd server)",
            "civ [on|off]", "data [on|off]", "dw [aprs]", "freq <MHz>",
            "mode <MODE>", "send <CALL> <text>", "info", "scan", "q",
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
