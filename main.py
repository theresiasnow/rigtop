"""
nmead - Read GPS/NMEA position from various sources and forward to sinks.

Supports sources: rigctld (IC-705 etc.), gps2ip (iOS)
Supports sinks:   console, wsjtx
"""

import argparse
import sys
from pathlib import Path

from nmead.config import load_config
from nmead.sources import create_source
from nmead.sinks import create_sink
from nmead.app import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmead",
        description="Read GPS/NMEA position from various sources and forward to sinks.",
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

    # Source selection
    source_group = parser.add_argument_group("GPS source")
    source_group.add_argument(
        "--source", choices=["rigctld", "gps2ip"], default=None,
        help="GPS source type (default: rigctld)",
    )
    source_group.add_argument(
        "--source-host", default=None,
        help="Source host address",
    )
    source_group.add_argument(
        "--source-port", type=int, default=None,
        help="Source TCP port",
    )

    # Sink selection (can be repeated)
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
        help="Show rig meter values (ALC, SWR, power, etc.) from rigctld",
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

    # Source: CLI overrides config file
    if args.source is not None:
        cfg.source["type"] = args.source
    if args.source_host is not None:
        cfg.source["host"] = args.source_host
    if args.source_port is not None:
        cfg.source["port"] = args.source_port

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

    # --- Create source ---
    source = create_source(cfg.source)
    try:
        source.connect()
    except (ConnectionRefusedError, OSError) as e:
        print(f"Error: Could not connect to {cfg.source.get('type', 'rigctld')} "
              f"at {cfg.source.get('host', '?')}:{cfg.source.get('port', '?')}")
        print(f"       {e}")
        sys.exit(1)

    # --- Create and start sinks ---
    sinks = [create_sink(s) for s in cfg.sinks]
    for sink in sinks:
        sink.start()

    # --- Run ---
    try:
        run(source, sinks, interval=cfg.interval, once=cfg.once, meters=cfg.meters)
    finally:
        source.close()
        for sink in sinks:
            sink.close()


if __name__ == "__main__":
    main()
