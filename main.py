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

from rigtop.app import run
from rigtop.config import load_config
from rigtop.rigctld_launcher import RigctldLauncher
from rigtop.sinks import create_sink
from rigtop.sinks.tui import AprsBuffer
from rigtop.sources.direwolf import DirewolfClient
from rigtop.sources.gps2ip import Gps2ipSource
from rigtop.sources.rigctld import RigctldSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rigtop",
        description="Ham radio rig dashboard — GPS, frequency, mode, meters.\n"
                    "Configure sinks and settings in rigtop.toml.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="Path to TOML config file (auto-discovers rigtop.toml)",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (also controls rigctld verbosity)",
    )
    parser.add_argument(
        "--console", action="store_true", default=False,
        help="Use plain console output instead of TUI",
    )
    parser.add_argument(
        "--once", action="store_true", default=None,
        help="Read position once and exit",
    )
    parser.add_argument(
        "--no-rigctld", action="store_true", default=False,
        help="Don't auto-start rigctld (assume it's already running)",
    )
    parser.add_argument(
        "--no-gps", action="store_true", default=False,
        help="Disable GPS fallback even if configured",
    )
    parser.add_argument(
        "--no-meters", action="store_true", default=False,
        help="Disable rig meters",
    )
    parser.add_argument(
        "--no-beacon", action="store_true", default=False,
        help="Disable APRS-IS position beaconing (still receives traffic)",
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

    if args.once is not None and args.once:
        cfg.once = True
    if args.no_meters:
        cfg.meters = False
    if args.no_rigctld:
        cfg.rigctld = None
    if args.no_gps:
        cfg.gps_fallback = None
    beacon_disabled = args.no_beacon
    if args.console:
        from rigtop.config import SinkConfig
        cfg.sinks = [SinkConfig(type="console")]

    # --- Create sinks early so the TUI log buffer exists before rigctld starts ---
    sinks = [create_sink(s.model_dump(exclude_defaults=False)) for s in cfg.sinks]

    aprs_buf: AprsBuffer | None = None

    # Shared APRS buffer: connect APRS-IS sink → TUI pane
    for sink in sinks:
        if hasattr(sink, "aprs_buffer") and hasattr(sink, "_receiver_loop"):
            aprs_buf = AprsBuffer()
            sink.aprs_buffer = aprs_buf
            if beacon_disabled:
                sink._beacon_enabled = False
            break

    # --- Direwolf KISS client (optional) ---
    dw_client: DirewolfClient | None = None
    if cfg.direwolf is not None:
        if aprs_buf is None:
            aprs_buf = AprsBuffer()
        dw_client = DirewolfClient(host=cfg.direwolf.host, port=cfg.direwolf.port)
        dw_client.aprs_buffer = aprs_buf

    for sink in sinks:
        if getattr(sink, "tui", False):
            sink.aprs_buffer = aprs_buf
            peers = [s for s in sinks if s is not sink]
            if dw_client is not None:
                peers.append(dw_client)
            sink.peers = peers
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
            stderr_callback=None,
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

    # Give TUI sink a reference to the rig for :mode / :freq commands
    for sink in sinks:
        if getattr(sink, "tui", False):
            sink.rig = rig
            sink.rig_name = cfg.rig.name
            break

    # --- QSY: if [aprs] section has qsy_freq/qsy_mode, apply to rig ---
    if cfg.aprs:
        try:
            if cfg.aprs.qsy_freq > 0:
                freq_hz = int(cfg.aprs.qsy_freq * 1e6)
                if rig.set_freq(freq_hz):
                    logger.info("QSY → {:.6f} MHz", cfg.aprs.qsy_freq)
                else:
                    logger.error("Failed to QSY to {:.6f} MHz", cfg.aprs.qsy_freq)
            if cfg.aprs.qsy_mode:
                if rig.set_mode(cfg.aprs.qsy_mode):
                    logger.info("Mode → {}", cfg.aprs.qsy_mode)
                else:
                    logger.error("Failed to set mode {}", cfg.aprs.qsy_mode)
        except (ConnectionError, OSError) as e:
            print(f"⚠  Radio not responding — is it powered on and connected? ({e})")
            logger.warning("QSY failed — radio disconnected: {}", e)

    # --- Optional GPS fallback ---
    gps_fallback = None
    if cfg.gps_fallback is not None and cfg.gps_fallback.enabled:
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
    if dw_client is not None:
        dw_client.start()
    for sink in sinks:
        try:
            sink.start()
        except Exception as e:
            logger.warning("Sink {} failed to start: {}", sink, e)

    # --- Static GPS fallback (last resort) ---
    static_pos = None
    if cfg.gps_static is not None and cfg.gps_static.enabled:
        from rigtop.sources import Position
        static_pos = Position(
            lat=cfg.gps_static.lat,
            lon=cfg.gps_static.lon,
            alt=cfg.gps_static.alt,
        )

    # --- Run ---
    try:
        run(rig, sinks, interval=cfg.interval, once=cfg.once, meters=cfg.meters,
            gps_fallback=gps_fallback, watchdog=cfg.watchdog,
            static_pos=static_pos)
    finally:
        rig.close()
        if dw_client:
            dw_client.close()
        if launcher:
            launcher.stop()
        if gps_fallback:
            gps_fallback.close()
        for sink in sinks:
            sink.close()


if __name__ == "__main__":
    main()
