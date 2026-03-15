"""
nmead - Read GPS/NMEA position from rigctld and forward to sinks.

Rigctld is always the primary source for GPS, frequency, mode, and meters.
Optional GPS fallback: gps2ip (iOS) when rig has no GPS fix.

Sinks: console, tui, wsjtx
"""

import argparse
import sys
from pathlib import Path

from nmead.config import load_config
from nmead.sources.rigctld import RigctldSource
from nmead.sources.gps2ip import Gps2ipSource
from nmead.sinks import create_sink
from nmead.app import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmead",
        description="Read GPS/NMEA position from rigctld and forward to sinks.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="Path to TOML config file",
    )

    # General
    parser.add_argument(
        "--interval", type=float, default=None,
        help="Poll interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--once", action="store_true", default=None,
        help="Read position once and exit",
    )

    # Rig (rigctld) — always used
    rig_group = parser.add_argument_group("Rig (rigctld)")
    rig_group.add_argument(
        "--rig-host", default=None,
        help="rigctld host address (default: 127.0.0.1)",
    )
    rig_group.add_argument(
        "--rig-port", type=int, default=None,
        help="rigctld TCP port (default: 4532)",
    )

    # GPS fallback
    gps_group = parser.add_argument_group("GPS fallback (gps2ip)")
    gps_group.add_argument(
        "--gps-fallback", action="store_true", default=False,
        help="Enable GPS2IP as fallback when rig has no GPS fix",
    )
    gps_group.add_argument(
        "--gps-host", default=None,
        help="GPS2IP host address",
    )
    gps_group.add_argument(
        "--gps-port", type=int, default=None,
        help="GPS2IP TCP port (default: 11123)",
    )

    # Sink selection
    sink_group = parser.add_argument_group("Position sinks")
    sink_group.add_argument(
        "--console", action="store_true", default=False,
        help="Output to console (default if no sink specified)",
    )
    sink_group.add_argument(
        "--nmea", action="store_true", default=False,
        help="Include NMEA sentences in console output",
    )
    sink_group.add_argument(
        "--meters", action="store_true", default=False,
        help="Show rig meter values (ALC, SWR, power, etc.)",
    )
    sink_group.add_argument(
        "--tui", action="store_true", default=False,
        help="Live dashboard with meters and rig info (implies --meters)",
    )
    sink_group.add_argument(
        "--wsjtx", action="store_true", default=False,
        help="Send grid locator to WSJT-X via UDP",
    )
    sink_group.add_argument(
        "--wsjtx-host", default=None,
        help="WSJT-X UDP host (default: 127.0.0.1)",
    )
    sink_group.add_argument(
        "--wsjtx-port", type=int, default=None,
        help="WSJT-X UDP port (default: 2237)",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --- Load config file (if any), then overlay CLI args ---
    cfg = load_config(args.config)

    if args.interval is not None:
        cfg.interval = args.interval
    if args.once is not None and args.once:
        cfg.once = True
    if args.meters or args.tui:
        cfg.meters = True

    # Rig: CLI overrides config
    if args.rig_host is not None:
        cfg.rig["host"] = args.rig_host
    if args.rig_port is not None:
        cfg.rig["port"] = args.rig_port

    # GPS fallback: CLI overrides config
    if args.gps_fallback or args.gps_host or args.gps_port:
        if cfg.gps_fallback is None:
            cfg.gps_fallback = {}
        if args.gps_host is not None:
            cfg.gps_fallback["host"] = args.gps_host
        if args.gps_port is not None:
            cfg.gps_fallback["port"] = args.gps_port

    # Sinks: CLI flags build sink list; if none given, fall back to config
    cli_sinks: list[dict] = []
    if args.tui:
        cli_sinks.append({"type": "tui"})
    elif args.console or args.nmea:
        sink_cfg: dict = {"type": "console"}
        if args.nmea:
            sink_cfg["nmea"] = True
        cli_sinks.append(sink_cfg)
    if args.wsjtx:
        wsjtx_cfg: dict = {"type": "wsjtx"}
        if args.wsjtx_host:
            wsjtx_cfg["host"] = args.wsjtx_host
        if args.wsjtx_port:
            wsjtx_cfg["port"] = args.wsjtx_port
        cli_sinks.append(wsjtx_cfg)

    if cli_sinks:
        cfg.sinks = cli_sinks

    # --- Connect to rig (always required) ---
    rig = RigctldSource(
        host=cfg.rig.get("host", "127.0.0.1"),
        port=cfg.rig.get("port", 4532),
    )
    try:
        rig.connect()
    except (ConnectionRefusedError, OSError) as e:
        print(f"Error: Could not connect to rigctld "
              f"at {cfg.rig.get('host', '127.0.0.1')}:{cfg.rig.get('port', 4532)}")
        print(f"       {e}")
        sys.exit(1)

    # --- Optional GPS fallback ---
    gps_fallback = None
    if cfg.gps_fallback is not None:
        gps_fallback = Gps2ipSource(
            host=cfg.gps_fallback.get("host", "192.168.1.1"),
            port=cfg.gps_fallback.get("port", 11123),
        )
        try:
            gps_fallback.connect()
        except (ConnectionRefusedError, OSError) as e:
            print(f"Warning: GPS fallback unavailable "
                  f"({cfg.gps_fallback.get('host', '?')}:{cfg.gps_fallback.get('port', '?')}): {e}")
            gps_fallback = None

    # --- Create and start sinks ---
    sinks = [create_sink(s) for s in cfg.sinks]
    for sink in sinks:
        sink.start()

    # --- Run ---
    try:
        run(rig, sinks, interval=cfg.interval, once=cfg.once, meters=cfg.meters,
            gps_fallback=gps_fallback)
    finally:
        rig.close()
        if gps_fallback:
            gps_fallback.close()
        for sink in sinks:
            sink.close()


if __name__ == "__main__":
    main()
