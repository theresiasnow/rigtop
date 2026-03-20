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


def _key_listener(stop: threading.Event, tui_sink=None) -> None:
    """Background thread: ':q' to quit, route commands to TUI."""
    if sys.platform == "win32":
        import msvcrt

        while not stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                # Command mode
                if tui_sink is not None and tui_sink.command_mode:
                    if ch in ("\r", "\n"):
                        cmd = tui_sink.command_buf
                        tui_sink.command_buf = ""
                        tui_sink.command_mode = False
                        if cmd.strip() in ("q", "quit"):
                            stop.set()
                            return
                        tui_sink.execute_command(cmd)
                    elif ch == "\x1b":  # Escape
                        tui_sink.command_buf = ""
                        tui_sink.command_mode = False
                    elif ch == "\x08":  # Backspace
                        tui_sink.command_buf = tui_sink.command_buf[:-1]
                    elif ch == "\t":  # Tab completion
                        tui_sink.tab_complete()
                    elif ch == "\x03":  # Ctrl+C
                        tui_sink.command_buf = ""
                        tui_sink.command_mode = False
                    else:
                        tui_sink.command_buf += ch
                    tui_sink.refresh_command_bar()
                    continue
                # Normal mode
                if ch == ":" and tui_sink is not None:
                    tui_sink.command_mode = True
                    tui_sink.command_buf = ""
                    tui_sink.refresh_command_bar()
                    continue
                if ch == "\x03":  # Ctrl+C
                    stop.set()
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
                # Command mode
                if tui_sink is not None and tui_sink.command_mode:
                    if ch in ("\r", "\n"):
                        cmd = tui_sink.command_buf
                        tui_sink.command_buf = ""
                        tui_sink.command_mode = False
                        if cmd.strip() in ("q", "quit"):
                            stop.set()
                            return
                        tui_sink.execute_command(cmd)
                    elif ch == "\x1b":  # Escape
                        tui_sink.command_buf = ""
                        tui_sink.command_mode = False
                    elif ch in ("\x7f", "\x08"):  # Backspace
                        tui_sink.command_buf = tui_sink.command_buf[:-1]
                    elif ch == "\t":  # Tab completion
                        tui_sink.tab_complete()
                    elif ch == "\x03":  # Ctrl+C
                        tui_sink.command_buf = ""
                        tui_sink.command_mode = False
                    else:
                        tui_sink.command_buf += ch
                    tui_sink.refresh_command_bar()
                    continue
                # Normal mode
                if ch == ":" and tui_sink is not None:
                    tui_sink.command_mode = True
                    tui_sink.command_buf = ""
                    tui_sink.refresh_command_bar()
                    continue
                if ch == "\x03":  # Ctrl+C
                    stop.set()
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


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

    # TX watchdog state
    _tx_start: float | None = None  # monotonic timestamp when TX began
    _wd_tripped: bool = False  # True after watchdog has fired (until RX resumes)
    _prev_ptt: bool = False  # previous PTT state for edge detection

    if not has_tui:
        print(f"Rig:      {rig}")
        if gps_fallback:
            print(f"GPS fallback: {gps_fallback}")
        for sink in sinks:
            print(f"Sink:     {sink}")
        print(f"Poll interval: {interval}s  (Ctrl+C to stop)\n")

    while True:
        try:
            # GPS: try rig first, then fallback
            pos = rig.get_position()
            gps_src = "rig"
            if pos is None and gps_fallback:
                pos = gps_fallback.get_position()
                gps_src = "fallback"
            if pos is None and static_pos is not None:
                pos = static_pos
                gps_src = "static"

            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if pos is None:
                if not has_tui:
                    print(f"[{now_str}] No GPS fix available\n")
            else:
                grid = maidenhead(pos.lat, pos.lon)

                # Rig data (always from rigctld)
                mode, passband = rig.get_mode_and_passband()
                extras: dict = {
                    "source_label": str(rig),
                    "gps_src": gps_src,
                    "freq": rig.get_frequency(),
                    "mode": mode,
                    "passband": passband,
                    "ptt": rig.get_ptt(),
                }

                # ── TX watchdog ──
                ptt = extras["ptt"]
                if ptt and not _prev_ptt:
                    _tx_start = time.monotonic()
                    logger.info("TX started")
                elif not ptt and _prev_ptt:
                    if _tx_start is not None:
                        tx_dur = time.monotonic() - _tx_start
                        logger.info("TX ended after {:.1f}s", tx_dur)
                    else:
                        logger.info("TX ended")
                    if _wd_tripped:
                        logger.info("TX watchdog reset — radio back to RX")
                    _tx_start = None
                    _wd_tripped = False
                _prev_ptt = bool(ptt)
                if watchdog and ptt is not None:
                    if ptt:
                        if _tx_start is None:
                            _tx_start = time.monotonic()
                        tx_dur = time.monotonic() - _tx_start
                        if not _wd_tripped and tx_dur >= watchdog.tx_timeout:
                            _wd_tripped = True
                            logger.critical(
                                "TX WATCHDOG: transmitting for {:.0f}s "
                                "(limit {}s) — forcing PTT off",
                                tx_dur,
                                watchdog.tx_timeout,
                            )
                            rig.set_ptt(False)
                            extras["ptt"] = False
                            extras["wd_tripped"] = True
                            if tui_sink is not None:
                                tui_sink.show_watchdog_alert(
                                    tx_dur,
                                    watchdog.tx_timeout,
                                )
                    else:
                        pass  # reset handled above in PTT transition logic
                if meters:
                    # Only poll relevant meters for current PTT state
                    if ptt:
                        m = rig.get_meters(levels=["ALC", "SWR", "RFPOWER_METER", "COMP_METER"])
                        rfpower = rig.get_level("RFPOWER")
                        if rfpower is not None:
                            m["RFPOWER"] = rfpower
                    else:
                        m = rig.get_meters(levels=["STRENGTH"])
                    extras["meters"] = m

                # Print summary (non-TUI only)
                if not has_tui:
                    print(
                        f"[{now_str}] {pos.lat:.6f}, {pos.lon:.6f}  Grid: {grid}  (GPS: {gps_src})"
                    )
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

                for sink in sinks:
                    msg = sink.send(pos, grid, **extras)
                    if msg and not has_tui:
                        print(f"  {msg}")

            if not has_tui:
                print()

            if once:
                break

            # Sleep in short intervals so we can react to 'q' quickly
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
            # Wait before retrying, but stay responsive to 'q'
            for _ in range(50):  # ~5 seconds
                if stop.is_set():
                    break
                time.sleep(0.1)
            if stop.is_set():
                break
            # Try to reconnect
            try:
                rig.reconnect()
                logger.info("Reconnected to rigctld")
            except (ConnectionError, OSError) as re_err:
                logger.warning("Reconnect failed: {}", re_err)
