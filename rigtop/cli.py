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
from rigtop.direwolf_launcher import DirewolfLauncher
from rigtop.rigctld_launcher import RigctldLauncher
from rigtop.sinks import create_sink
from rigtop.sinks.tui import AprsBuffer, DirewolfBuffer, MessageBuffer
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
        "--no-direwolf", action="store_true", default=False,
        help="Don't auto-start Direwolf (assume it's already running)",
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
    parser.add_argument(
        "--scan", action="store_true", default=False,
        help="Scan LAN for radios and rigctld instances, then exit",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --- LAN scan mode (--scan) ---
    if args.scan:
        from rigtop.discovery import format_results, scan_lan
        print("Scanning LAN for radio services…")
        results = scan_lan(
            progress_cb=lambda done, total: print(
                f"  {done}/{total}", end="\r",
            ),
        )
        print(format_results(results))
        return

    # --- Load config file (auto-discovers rigtop.toml), then overlay CLI args ---
    print("[1/8] Loading config…")
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
    if args.no_direwolf and cfg.direwolf is not None:
        cfg.direwolf.install_path = None
    if args.no_gps:
        cfg.gps_fallback = None
    beacon_disabled = args.no_beacon
    if args.console:
        from rigtop.config import SinkConfig
        cfg.sinks = [SinkConfig(type="console")]

    # --- Create sinks early so the TUI log buffer exists before rigctld starts ---
    # All configured sinks are created (including disabled ones) so toggle commands
    # can enable them at runtime. Only enabled=True sinks are auto-started later.
    print("[2/8] Creating sinks…")
    _sink_cfgs = list(cfg.sinks)
    sinks = [create_sink(s.model_dump(exclude_defaults=False)) for s in _sink_cfgs]
    # Sinks with enabled=False are created but not auto-started (toggle via command)
    _disabled_at_start = {id(sinks[i]) for i, s in enumerate(_sink_cfgs) if not s.enabled}

    aprs_buf: AprsBuffer | None = None
    msg_buf: MessageBuffer | None = None

    # Shared APRS buffer: connect APRS-IS sink → TUI pane
    for sink in sinks:
        if hasattr(sink, "aprs_buffer") and hasattr(sink, "_receiver_loop"):
            aprs_buf = AprsBuffer()
            msg_buf = MessageBuffer()
            sink.aprs_buffer = aprs_buf
            sink.msg_buffer = msg_buf
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
            sink.msg_buffer = msg_buf
            peers = [s for s in sinks if s is not sink]
            if dw_client is not None:
                peers.append(dw_client)
            sink.peers = peers
            break

    # --- Auto-start rigctld if configured ---
    launcher: RigctldLauncher | None = None
    rigctld_buffer: DirewolfBuffer | None = None

    if cfg.rigctld is not None:
        print(f"[3/8] Starting rigctld (model {cfg.rigctld.model}, {cfg.rigctld.serial_port})…")
        rigctld_buffer = DirewolfBuffer()
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
            stderr_callback=rigctld_buffer.push,
        )
        try:
            launcher.start()
            atexit.register(launcher.stop)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    # --- Prepare Direwolf launcher (started on-demand by :aprs/:bbs) ---
    dw_launcher: DirewolfLauncher | None = None
    dw_buffer: DirewolfBuffer | None = None

    if cfg.direwolf is not None and cfg.direwolf.install_path:
        dwcfg = cfg.direwolf
        print("[4/8] Direwolf launcher ready (on-demand)")
        dw_buffer = DirewolfBuffer()
        dw_launcher = DirewolfLauncher(
            install_path=dwcfg.install_path,
            stderr_callback=dw_buffer.push,
            extra_args=dwcfg.extra_args,
        )
        atexit.register(dw_launcher.stop)
    else:
        print("[4/8] Direwolf launcher — disabled")

    # --- Connect to rig (always required) ---
    print(f"[5/8] Connecting to rig ({cfg.rig.name} @ {cfg.rig.host}:{cfg.rig.port})…")
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
            sink.aprs_config = cfg.aprs
            sink.packet_config = cfg.bbs
            sink.dw_launcher = dw_launcher
            sink.dw_buffer = dw_buffer
            sink.rigctld_buffer = rigctld_buffer
            break
    # Wire CI-V proxy sinks to rigctld for write commands
    for sink in sinks:
        if hasattr(sink, 'set_rigctld_callback'):
            sink.set_rigctld_callback(rig._send_command)
    # --- QSY: only if [aprs] enabled (otherwise :aprs on / :bbs on does it) ---
    if cfg.aprs and cfg.aprs.enabled:
        print(f"[6/8] QSY → {cfg.aprs.freq:.3f} MHz {cfg.aprs.qsy_mode}…")
        try:
            if cfg.aprs.freq > 0:
                freq_hz = int(cfg.aprs.freq * 1e6)
                if rig.set_freq(freq_hz):
                    logger.info("QSY → {:.6f} MHz", cfg.aprs.freq)
                else:
                    logger.error("Failed to QSY to {:.6f} MHz", cfg.aprs.freq)
            if cfg.aprs.qsy_mode:
                if rig.set_mode(cfg.aprs.qsy_mode):
                    logger.info("Mode → {}", cfg.aprs.qsy_mode)
                else:
                    logger.error("Failed to set mode {}", cfg.aprs.qsy_mode)
        except (ConnectionError, OSError) as e:
            print(f"⚠  Radio not responding — is it powered on and connected? ({e})")
            logger.warning("QSY failed — radio disconnected: {}", e)
    else:
        print("[6/8] QSY — skipped (use :aprs on / :bbs on)")

    # --- Optional GPS fallback ---
    gps_fallback = None
    if cfg.gps_fallback is not None and cfg.gps_fallback.enabled:
        print(f"[7/8] GPS fallback → {cfg.gps_fallback.host}:{cfg.gps_fallback.port}…")
        gps_fallback = Gps2ipSource(
            host=cfg.gps_fallback.host,
            port=cfg.gps_fallback.port,
            timeout=3.0,
        )
        try:
            gps_fallback.connect()
            print("      GPS fallback connected")
        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            print(f"      GPS fallback unavailable ({e}) — skipping")
            gps_fallback = None
    else:
        print("[7/8] GPS fallback — disabled")

    # --- Start sinks ---
    print("[8/8] Starting sinks…")
    if dw_client is not None:
        dw_client.start()
    for sink in sinks:
        if id(sink) in _disabled_at_start:
            logger.info("Sink {} disabled at startup (toggle with command)", sink)
            continue
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
    print("Ready ✓")
    tui_sink = next((s for s in sinks if getattr(s, "tui", False)), None)
    try:
        if tui_sink is not None:
            from rigtop.sinks.tui import RigtopApp
            app = RigtopApp(
                rig=rig,
                sinks=sinks,
                dw_launcher=dw_launcher,
                dw_client=dw_client,
                dw_buffer=dw_buffer,
                rigctld_buffer=rigctld_buffer,
                aprs_buffer=aprs_buf,
                msg_buffer=msg_buf,
                aprs_config=cfg.aprs,
                packet_config=cfg.bbs,
                rig_name=cfg.rig.name,
                interval=cfg.interval,
                meters=cfg.meters,
                gps_fallback=gps_fallback,
                static_pos=static_pos,
                watchdog=cfg.watchdog,
                beacon_disabled=beacon_disabled,
            )
            app.run()
        else:
            run(rig, sinks, interval=cfg.interval, once=cfg.once, meters=cfg.meters,
                gps_fallback=gps_fallback, watchdog=cfg.watchdog,
                static_pos=static_pos)
    except KeyboardInterrupt:
        pass
    finally:
        rig.close()
        if dw_client:
            dw_client.close()
        if dw_launcher:
            dw_launcher.stop()
        if launcher:
            launcher.stop()
        if gps_fallback:
            gps_fallback.close()
        for sink in sinks:
            sink.close()


if __name__ == "__main__":
    main()
