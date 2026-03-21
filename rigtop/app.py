"""Application loop: polls rig and optional GPS fallback, dispatches to sinks."""

from __future__ import annotations

import datetime
import sys
import threading
import time

from loguru import logger

from rigtop.config import WatchdogConfig
from rigtop.geo import maidenhead
from rigtop.sinks import PositionSink
from rigtop.sources import GpsSource, Position
from rigtop.sources.rigctld import RigctldSource


def _is_tui(sink: PositionSink) -> bool:
    return getattr(sink, "tui", False)


# ---------------------------------------------------------------------------
# TX watchdog
# ---------------------------------------------------------------------------


class TxWatchdog:
    """Tracks TX duration and trips PTT off when the timeout is exceeded."""

    def __init__(self, cfg: WatchdogConfig | None) -> None:
        self._cfg = cfg
        self._tx_start: float | None = None
        self._tripped: bool = False
        self._prev_ptt: bool = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    def update(self, ptt: bool | None, rig: RigctldSource, extras: dict, tui_sink) -> None:
        """Update watchdog state; force PTT off and mutate *extras* if tripped."""
        ptt_bool = bool(ptt)

        # Edge detection
        if ptt_bool and not self._prev_ptt:
            self._tx_start = time.monotonic()
            logger.info("TX started")
        elif not ptt_bool and self._prev_ptt:
            if self._tx_start is not None:
                tx_dur = time.monotonic() - self._tx_start
                logger.info("TX ended after {:.1f}s", tx_dur)
            else:
                logger.info("TX ended")
            if self._tripped:
                logger.info("TX watchdog reset — radio back to RX")
            self._tx_start = None
            self._tripped = False
        self._prev_ptt = ptt_bool

        if self._cfg is None or ptt is None:
            return

        if ptt_bool:
            if self._tx_start is None:
                self._tx_start = time.monotonic()
            tx_dur = time.monotonic() - self._tx_start
            if not self._tripped and tx_dur >= self._cfg.tx_timeout:
                self._tripped = True
                logger.critical(
                    "TX WATCHDOG: transmitting for {:.0f}s (limit {}s) — forcing PTT off",
                    tx_dur,
                    self._cfg.tx_timeout,
                )
                rig.set_ptt(False)
                extras["ptt"] = False
                extras["wd_tripped"] = True
                if tui_sink is not None:
                    tui_sink.show_watchdog_alert(tx_dur, self._cfg.tx_timeout)


# ---------------------------------------------------------------------------
# GPS resolution
# ---------------------------------------------------------------------------


def resolve_position(
    rig: RigctldSource,
    gps_fallback: GpsSource | None,
    static_pos: Position | None,
) -> tuple[Position | None, str]:
    """Return the best available position and its source label."""
    pos = rig.get_position()
    if pos is not None:
        return pos, "rig"
    if gps_fallback is not None:
        pos = gps_fallback.get_position()
        if pos is not None:
            return pos, "fallback"
    if static_pos is not None:
        return static_pos, "static"
    return None, "none"


# ---------------------------------------------------------------------------
# Meters
# ---------------------------------------------------------------------------


def collect_meters(rig: RigctldSource) -> dict[str, float]:
    """Poll all rig meters and return a flat dict."""
    m: dict[str, float] = {}
    strength = rig.get_strength()
    if strength is not None:
        m["STRENGTH"] = strength
    m.update(rig.get_meters(levels=["ALC", "SWR", "RFPOWER_METER", "COMP_METER"]))
    rfpower = rig.get_level("RFPOWER")
    if rfpower is not None:
        m["RFPOWER"] = rfpower
    return m


# ---------------------------------------------------------------------------
# Console output (non-TUI mode)
# ---------------------------------------------------------------------------


def _print_cycle(
    now_str: str,
    pos: Position | None,
    grid: str,
    extras: dict,
) -> None:
    """Print one poll cycle to stdout (non-TUI mode only)."""
    if pos is None:
        print(f"[{now_str}] No GPS fix available\n")
        return
    gps_src = extras.get("gps_src", "?")
    print(f"[{now_str}] {pos.lat:.6f}, {pos.lon:.6f}  Grid: {grid}  (GPS: {gps_src})")
    freq = extras.get("freq")
    mode = extras.get("mode")
    if freq or mode:
        freq_mhz = f"{float(freq) / 1e6:.6f} MHz" if freq else "?"
        print(f"  Rig: {freq_mhz}  {mode or '?'}")
    meter_vals = extras.get("meters")
    if meter_vals:
        parts = []
        for name, val in meter_vals.items():
            if name == "STRENGTH":
                parts.append(f"S-meter: {val:+.0f}dB")
            elif name == "SWR":
                parts.append(f"SWR: {val:.1f}")
            else:
                label = name.replace("_METER", "").replace("_", " ")
                parts.append(f"{label}: {val:.2f}")
        print(f"  Meters: {', '.join(parts)}")
    print()


# ---------------------------------------------------------------------------
# Key listener (console mode)
# ---------------------------------------------------------------------------


def _dispatch_key(ch: str, stop: threading.Event, tui_sink) -> bool:
    """Handle one keypress. Returns True if the stop event was set."""
    if tui_sink is not None and tui_sink.command_mode:
        if ch in ("\r", "\n"):
            cmd = tui_sink.command_buf
            tui_sink.command_buf = ""
            tui_sink.command_mode = False
            if cmd.strip() in ("q", "quit"):
                stop.set()
                return True
            tui_sink.execute_command(cmd)
        elif ch == "\x1b":
            tui_sink.command_buf = ""
            tui_sink.command_mode = False
        elif ch in ("\x7f", "\x08"):  # Backspace (both Unix and Windows)
            tui_sink.command_buf = tui_sink.command_buf[:-1]
        elif ch == "\t":
            tui_sink.tab_complete()
        elif ch == "\x03":  # Ctrl+C
            tui_sink.command_buf = ""
            tui_sink.command_mode = False
        else:
            tui_sink.command_buf += ch
        tui_sink.refresh_command_bar()
        return False

    # Normal mode
    if ch == ":" and tui_sink is not None:
        tui_sink.command_mode = True
        tui_sink.command_buf = ""
        tui_sink.refresh_command_bar()
        return False
    if ch == "\x03":  # Ctrl+C
        stop.set()
        return True
    return False


def _key_listener(stop: threading.Event, tui_sink=None) -> None:
    """Background thread: ':q' to quit, route commands to TUI sink."""
    if sys.platform == "win32":
        import msvcrt

        while not stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if _dispatch_key(ch, stop, tui_sink):
                    return
            else:
                time.sleep(0.1)
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop.is_set():
                ch = sys.stdin.read(1)
                if _dispatch_key(ch, stop, tui_sink):
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def run(
    rig: RigctldSource,
    sinks: list[PositionSink],
    interval: float = 2.0,
    once: bool = False,
    meters: bool = False,
    gps_fallback: GpsSource | None = None,
    watchdog: WatchdogConfig | None = None,
    static_pos: Position | None = None,
) -> None:
    """Main polling loop.

    GPS is read from *rig* first.  If no fix, *gps_fallback* is tried.
    If neither has a fix, *static_pos* (from config) is used as last resort.
    Frequency, mode, and meters always come from *rig*.
    """
    has_tui = any(_is_tui(s) for s in sinks)
    tui_sink = next((s for s in sinks if _is_tui(s)), None)

    stop = threading.Event()
    key_thread = threading.Thread(target=_key_listener, args=(stop, tui_sink), daemon=True)
    key_thread.start()

    wd = TxWatchdog(watchdog)

    if not has_tui:
        print(f"Rig:      {rig}")
        if gps_fallback:
            print(f"GPS fallback: {gps_fallback}")
        for sink in sinks:
            print(f"Sink:     {sink}")
        print(f"Poll interval: {interval}s  (Ctrl+C to stop)\n")

    while True:
        try:
            pos, gps_src = resolve_position(rig, gps_fallback, static_pos)
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if pos is not None:
                grid = maidenhead(pos.lat, pos.lon)
                mode, passband = rig.get_mode_and_passband()
                extras: dict = {
                    "source_label": str(rig),
                    "gps_src": gps_src,
                    "freq": rig.get_frequency(),
                    "mode": mode,
                    "passband": passband,
                    "ptt": rig.get_ptt(),
                }

                wd.update(extras["ptt"], rig, extras, tui_sink)

                if meters:
                    extras["meters"] = collect_meters(rig)

                if not has_tui:
                    _print_cycle(now_str, pos, grid, extras)

                for sink in sinks:
                    msg = sink.send(pos, grid, **extras)
                    if msg and not has_tui:
                        print(f"  {msg}")
            else:
                if not has_tui:
                    _print_cycle(now_str, None, "", {})

            if not has_tui and pos is not None:
                print()

            if once:
                break

            for _ in range(int(interval * 10)):
                if stop.is_set():
                    break
                time.sleep(0.1)
            if stop.is_set():
                break

        except KeyboardInterrupt:
            break
        except (ConnectionError, OSError) as e:
            logger.warning("Connection error: {}", e)
            if has_tui:
                for s in sinks:
                    if _is_tui(s):
                        s.show_alert(str(e), f"rigctld @ {rig.host}:{rig.port}")
            else:
                print(f"Connection error: {e} — retrying…")
            for _ in range(50):  # ~5 seconds
                if stop.is_set():
                    break
                time.sleep(0.1)
            if stop.is_set():
                break
            try:
                rig.reconnect()
                logger.info("Reconnected to rigctld")
            except (ConnectionError, OSError) as re_err:
                logger.warning("Reconnect failed: {}", re_err)
