"""Application loop: polls source, dispatches to sinks."""

from __future__ import annotations

import datetime
import time

from nmead.geo import maidenhead
from nmead.sinks import PositionSink
from nmead.sources import GpsSource
from nmead.sources.rigctld import RigctldSource


def _is_tui(sink: PositionSink) -> bool:
    return getattr(sink, "tui", False)


def run(
    source: GpsSource,
    sinks: list[PositionSink],
    interval: float = 2.0,
    once: bool = False,
    meters: bool = False,
) -> None:
    """Main polling loop."""
    has_tui = any(_is_tui(s) for s in sinks)
    source_label = str(source)

    if not has_tui:
        print(f"Source: {source}")
        for sink in sinks:
            print(f"Sink:   {sink}")
        print(f"Poll interval: {interval}s  (Ctrl+C to stop)\n")

    while True:
        try:
            pos = source.get_position()
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if pos is None:
                if not has_tui:
                    print(f"[{now_str}] No GPS fix available\n")
            else:
                grid = maidenhead(pos.lat, pos.lon)

                # Gather rig extras
                extras: dict = {"source_label": source_label}
                if isinstance(source, RigctldSource):
                    extras["freq"] = source.get_frequency()
                    extras["mode"] = source.get_mode()
                    if meters:
                        extras["meters"] = source.get_meters()

                # Print summary line (non-TUI only)
                if not has_tui:
                    print(f"[{now_str}] {pos.lat:.6f}, {pos.lon:.6f}  Grid: {grid}")
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

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except ConnectionError as e:
            print(f"Connection lost: {e}")
            break
