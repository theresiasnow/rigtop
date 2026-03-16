"""
rigtop - Ham radio rig dashboard.

Rigctld is always the primary source for GPS, frequency, mode, and meters.
Optional GPS fallback: gps2ip (iOS) when rig has no GPS fix.

Sinks: console, tui, wsjtx
"""

import argparse
import atexit
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
    sink_group.add_argument(
        "--direwolf", action="store_true", default=False,
        help="Also serve NMEA GPS to Direwolf via TCP",
    )
    sink_group.add_argument(
        "--direwolf-host", default=None,
        help="Direwolf NMEA TCP listen host (default: 127.0.0.1)",
    )
    sink_group.add_argument(
        "--direwolf-port", type=int, default=None,
        help="Direwolf NMEA TCP listen port (default: 10110)",
    )
    sink_group.add_argument(
        "--direwolf-device", default=None,
        help="Serial port for Direwolf NMEA (e.g. COM10) — Windows mode",
    )
    sink_group.add_argument(
        "--gpsd", action="store_true", default=False,
        help="Also serve GPS via gpsd-compatible JSON protocol",
    )
    sink_group.add_argument(
        "--gpsd-host", default=None,
        help="gpsd TCP listen host (default: 127.0.0.1)",
    )
    sink_group.add_argument(
        "--gpsd-port", type=int, default=None,
        help="gpsd TCP listen port (default: 2947)",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --- Load config file (auto-discovers rigtop.toml), then overlay CLI args ---
    cfg = load_config(args.config)

    if args.log_level is not None:
        cfg.log_level = args.log_level

    # Configure loguru: remove default stderr sink, log to file
    logger.remove()
    log_file = Path("rigtop.log")
    logger.add(
        str(log_file),
        level=cfg.log_level.value,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} {level:<8} | {name} - {message}",
        rotation="5 MB",
        retention=3,
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
    if args.direwolf:
        dw_sink = SinkConfig(type="direwolf", port=10110)
        if args.direwolf_host:
            dw_sink.host = args.direwolf_host
        if args.direwolf_port:
            dw_sink.port = args.direwolf_port
        if args.direwolf_device:
            dw_sink.device = args.direwolf_device
        if not any(s.type == "direwolf" for s in cfg.sinks):
            cfg.sinks.append(dw_sink)
    if args.gpsd:
        gpsd_sink = SinkConfig(type="gpsd", port=2947)
        if args.gpsd_host:
            gpsd_sink.host = args.gpsd_host
        if args.gpsd_port:
            gpsd_sink.port = args.gpsd_port
        if not any(s.type == "gpsd" for s in cfg.sinks):
            cfg.sinks.append(gpsd_sink)

    # --- Create sinks early so the TUI log buffer exists before rigctld starts ---
    sinks = [create_sink(s.model_dump(exclude_defaults=False)) for s in cfg.sinks]

    tui_log_buf: TuiLogBuffer | None = None
    for sink in sinks:
        if getattr(sink, "tui", False):
            tui_log_buf = TuiLogBuffer()
            tui_log_buf.min_level = cfg.log_level.value
            sink.log_buffer = tui_log_buf
            sink.peers = [s for s in sinks if s is not sink]
            break

    # --- Auto-start rigctld if configured ---
    launcher: RigctldLauncher | None = None

    if cfg.rigctld is not None:
        launcher = RigctldLauncher(
            model=cfg.rigctld.model,
            serial_port=cfg.rigctld.serial_port,
            baud_rate=cfg.rigctld.baud_rate,
            data_bits=cfg.rigctld.data_bits,
            stop_bits=cfg.rigctld.stop_bits,
            serial_parity=cfg.rigctld.serial_parity.value,
            serial_handshake=cfg.rigctld.serial_handshake.value,
            dtr_state=cfg.rigctld.dtr_state.value,
            rts_state=cfg.rigctld.rts_state.value,
            ptt_type=cfg.rigctld.ptt_type.value,
            ptt_pathname=cfg.rigctld.ptt_pathname,
            ptt_share=cfg.rigctld.ptt_share,
            listen_host=cfg.rig.host,
            listen_port=cfg.rig.port,
            log_level=cfg.log_level.value,
            stderr_callback=tui_log_buf.push_line if tui_log_buf else None,
        )
        try:
            launcher.start()
            atexit.register(launcher.stop)
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

    # --- Start sinks ---
    for sink in sinks:
        try:
            sink.start()
        except Exception as e:
            logger.warning("Sink {} failed to start: {}", sink, e)

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
