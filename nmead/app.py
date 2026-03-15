"""Application loop: polls source, dispatches to sinks."""

from __future__ import annotations

import datetime
import time

from nmead.geo import maidenhead
from nmead.sinks import PositionSink
from nmead.sources import GpsSource
from nmead.sources.rigctld import RigctldSource


def run(
    source: GpsSource,
    sinks: list[PositionSink],
    interval: float = 2.0,
    once: bool = False,
) -> None:
    """Main polling loop."""
    print(f"Source: {source}")
    for sink in sinks:
        print(f"Sink:   {sink}")
    print(f"Poll interval: {interval}s  (Ctrl+C to stop)\n")

    while True:
        try:
            pos = source.get_position()
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if pos is None:
                print(f"[{now_str}] No GPS fix available")
            else:
                grid = maidenhead(pos.lat, pos.lon)
                print(f"[{now_str}] {pos.lat:.6f}, {pos.lon:.6f}  Grid: {grid}")

                # Show rig info if source supports it
                if isinstance(source, RigctldSource):
                    freq = source.get_frequency()
                    mode = source.get_mode()
                    if freq or mode:
                        freq_mhz = f"{float(freq) / 1e6:.6f} MHz" if freq else "?"
                        print(f"  Rig: {freq_mhz}  {mode or '?'}")

                for sink in sinks:
                    msg = sink.send(pos, grid)
                    if msg:
                        print(f"  {msg}")

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
