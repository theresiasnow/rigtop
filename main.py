"""
rigtop - Ham radio rig dashboard.

Rigctld is always the primary source for GPS, frequency, mode, and meters.
Optional GPS fallback: gps2ip (iOS) when rig has no GPS fix.

Sinks: console, tui, wsjtx
"""

import argparse
import sys
from pathlib import Path

from loguru import logger

from rigtop.config import GpsConfig, SinkConfig, load_config
from rigtop.rigctld_launcher import RigctldLauncher
from rigtop.sources.rigctld import RigctldSource
from rigtop.sources.gps2ip import Gps2ipSource
from rigtop.sinks import create_sink
from rigtop.sinks.tui import TuiLogBuffer
from rigtop.app import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rigtop",
        description="Ham radio rig dashboard — GPS, frequency, mode, meters.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="Path to TOML config file (auto-discovers rigtop.toml)",
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
    rig_group.add_argument(
        "--no-rigctld", action="store_true", default=False,
        help="Don't auto-start rigctld (assume it's already running)",
    )
    rig_group.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (also controls rigctld verbosity)",
    )

    # GPS fallback
    gps_group = parser.add_argument_group("GPS fallback (gps2ip)")
    gps_group.add_argument(
        "--no-gps", action="store_true", default=False,
        help="Disable GPS fallback even if configured",
    )
    gps_group.add_argument(
        "--gps-host", default=None,
        help="GPS2IP host address (enables fallback)",
    )
    gps_group.add_argument(
        "--gps-port", type=int, default=None,
        help="GPS2IP TCP port (default: 11123)",
    )

    # Sink selection
    sink_group = parser.add_argument_group("Output")
    sink_group.add_argument(
        "--console", action="store_true", default=False,
        help="Use plain console output instead of TUI",
    )
    sink_group.add_argument(
        "--nmea", action="store_true", default=False,
        help="Include NMEA sentences in console output",
    )
    sink_group.add_argument(
        "--no-meters", action="store_true", default=False,
        help="Disable rig meters",
    )
    sink_group.add_argument(
        "--wsjtx", action="store_true", default=False,
        help="Also send grid locator to WSJT-X via UDP",
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

    # --- Load config file (auto-discovers rigtop.toml), then overlay CLI args ---
    cfg = load_config(args.config)

    if args.log_level is not None:
        cfg.log_level = args.log_level

    # Configure loguru: remove default stderr sink, add one at the right level
    logger.remove()
    logger.add(
        sys.stderr,
        level=cfg.log_level.value,
        format="<dim>{time:HH:mm:ss}</dim> <level>{level:<8}</level> | <cyan>{name}</cyan> - {message}",
    )

    if args.interval is not None:
        cfg.interval = args.interval
    if args.once is not None and args.once:
        cfg.once = True
    if args.no_meters:
        cfg.meters = False
    if args.no_rigctld:
        cfg.rigctld = None

    # Rig: CLI overrides config
    if args.rig_host is not None:
        cfg.rig.host = args.rig_host
    if args.rig_port is not None:
        cfg.rig.port = args.rig_port

    # GPS fallback: auto from config, --gps-host enables on CLI, --no-gps disables
    if args.no_gps:
        cfg.gps_fallback = None
    elif args.gps_host or args.gps_port:
        if cfg.gps_fallback is None:
            cfg.gps_fallback = GpsConfig()
        if args.gps_host is not None:
            cfg.gps_fallback.host = args.gps_host
        if args.gps_port is not None:
            cfg.gps_fallback.port = args.gps_port

    # Sinks: TUI by default; --console downgrades; --wsjtx adds
    if args.console or args.nmea:
        cfg.sinks = [SinkConfig(type="console", nmea=bool(args.nmea))]
    if args.wsjtx:
        wsjtx_sink = SinkConfig(type="wsjtx")
        if args.wsjtx_host:
            wsjtx_sink.host = args.wsjtx_host
        if args.wsjtx_port:
            wsjtx_sink.port = args.wsjtx_port
        if not any(s.type == "wsjtx" for s in cfg.sinks):
            cfg.sinks.append(wsjtx_sink)

    # --- Auto-start rigctld if configured ---
    launcher: RigctldLauncher | None = None

    if cfg.rigctld is not None:
        launcher = RigctldLauncher(
            model=cfg.rigctld.model,
            serial_port=cfg.rigctld.serial_port,
            baud_rate=cfg.rigctld.baud_rate,
            listen_host=cfg.rig.host,
            listen_port=cfg.rig.port,
            log_level=cfg.log_level.value,
        )
        try:
            launcher.start()
        except (FileNotFoundError, RuntimeError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    # --- Connect to rig (always required) ---
    rig = RigctldSource(host=cfg.rig.host, port=cfg.rig.port)
    try:
        rig.connect()
    except (ConnectionRefusedError, OSError) as e:
        if launcher:
            launcher.stop()
        print(f"Error: Could not connect to rigctld at {cfg.rig.host}:{cfg.rig.port}")
        print(f"       {e}")
        sys.exit(1)

    # --- Optional GPS fallback ---
    gps_fallback = None
    if cfg.gps_fallback is not None:
        gps_fallback = Gps2ipSource(
            host=cfg.gps_fallback.host,
            port=cfg.gps_fallback.port,
        )
        try:
            gps_fallback.connect()
        except (ConnectionRefusedError, OSError) as e:
            print(f"Warning: GPS fallback unavailable "
                  f"({cfg.gps_fallback.host}:{cfg.gps_fallback.port}): {e}")
            gps_fallback = None

    # --- Create and start sinks ---
    sinks = [create_sink(s.model_dump(exclude_defaults=False)) for s in cfg.sinks]

    # If a TUI sink is present, attach a log buffer and register it with loguru
    # Only show rigctld-related messages in the TUI log pane
    _tui_modules = {"rigtop.sources.rigctld", "rigtop.app"}
    for sink in sinks:
        if getattr(sink, "tui", False):
            log_buf = TuiLogBuffer()
            sink.log_buffer = log_buf
            logger.add(
                log_buf.write,
                level=cfg.log_level.value,
                format="{message}",
                filter=lambda record: record["name"] in _tui_modules,
            )
            break

    for sink in sinks:
        sink.start()

    # --- Run ---
    try:
        run(rig, sinks, interval=cfg.interval, once=cfg.once, meters=cfg.meters,
            gps_fallback=gps_fallback)
    finally:
        rig.close()
        if launcher:
            launcher.stop()
        if gps_fallback:
            gps_fallback.close()
        for sink in sinks:
            sink.close()


if __name__ == "__main__":
    main()
