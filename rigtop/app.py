"""Application loop: polls rig and optional GPS fallback, dispatches to sinks."""

from __future__ import annotations

import datetime
import sys
import threading
import time

from loguru import logger

from rigtop.geo import maidenhead
from rigtop.sinks import PositionSink
from rigtop.sources import GpsSource
from rigtop.sources.rigctld import RigctldSource


def _is_tui(sink: PositionSink) -> bool:
    return getattr(sink, "tui", False)


def _key_listener(stop: threading.Event) -> None:
    """Background thread: watch for 'q' or ':q' to signal exit."""
    if sys.platform == "win32":
        import msvcrt
        buf = ""
        while not stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    if buf.strip() in ("q", ":q"):
                        stop.set()
                        return
                    buf = ""
                elif ch == "\x03":  # Ctrl+C
                    stop.set()
                    return
                else:
                    buf += ch
                    # Single 'q' without needing Enter
                    if buf == "q":
                        stop.set()
                        return
            else:
                time.sleep(0.1)
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            buf = ""
            while not stop.is_set():
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    if buf.strip() in ("q", ":q"):
                        stop.set()
                        return
                    buf = ""
                elif ch == "\x03":
                    stop.set()
                    return
                else:
                    buf += ch
                    if buf == "q":
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
) -> None:
    """Main polling loop.

    GPS is read from *rig* first.  If no fix, *gps_fallback* is tried.
    Frequency, mode, and meters always come from *rig*.
    """
    has_tui = any(_is_tui(s) for s in sinks)

    stop = threading.Event()
    key_thread = threading.Thread(target=_key_listener, args=(stop,), daemon=True)
    key_thread.start()

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
                if meters:
                    m = rig.get_meters()
                    # Also grab TX power setting (0-1)
                    rfpower = rig.get_level("RFPOWER")
                    if rfpower is not None:
                        m["RFPOWER"] = rfpower
                    extras["meters"] = m

                # Print summary (non-TUI only)
                if not has_tui:
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
