"""
rigtop - Ham radio rig dashboard.

Rigctld is always the primary source for GPS, frequency, mode, and meters.
Optional GPS fallback: gps2ip (iOS) when rig has no GPS fix.

Sinks: console, tui, wsjtx
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from rich.console import Console
from rich.rule import Rule

from rigtop.app import run
from rigtop.config import Config, SinkConfig, load_config
from rigtop.direwolf_launcher import DirewolfLauncher
from rigtop.rigctld_launcher import RigctldLauncher
from rigtop.sinks import PositionSink, create_sink
from rigtop.sinks.tui import AprsBuffer, DirewolfBuffer, MessageBuffer
from rigtop.sources import Position
from rigtop.sources.direwolf import DirewolfClient
from rigtop.sources.gps2ip import Gps2ipSource
from rigtop.sources.rigctld import RigctldSource

_console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Resources dataclass — everything that needs to be shut down on exit
# ---------------------------------------------------------------------------


@dataclass
class AppResources:
    rig: RigctldSource
    sinks: list[PositionSink] = field(default_factory=list)
    launcher: RigctldLauncher | None = None
    dw_launcher: DirewolfLauncher | None = None
    dw_client: DirewolfClient | None = None
    gps_fallback: Gps2ipSource | None = None
    static_pos: Position | None = None


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from rigtop import __version__

    parser = argparse.ArgumentParser(
        prog="rigtop",
        description="Ham radio rig dashboard — GPS, frequency, mode, meters.\n"
        "Configure sinks and settings in rigtop.toml.",
    )
    parser.add_argument("--version", action="version", version=f"rigtop {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to TOML config file (auto-discovers rigtop.toml)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (also controls rigctld verbosity)",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        default=False,
        help="Use plain console output instead of TUI",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=None,
        help="Read position once and exit",
    )
    parser.add_argument(
        "--no-rigctld",
        action="store_true",
        default=False,
        help="Don't auto-start rigctld (assume it's already running)",
    )
    parser.add_argument(
        "--no-direwolf",
        action="store_true",
        default=False,
        help="Don't auto-start Direwolf (assume it's already running)",
    )
    parser.add_argument(
        "--no-gps",
        action="store_true",
        default=False,
        help="Disable GPS fallback even if configured",
    )
    parser.add_argument(
        "--no-meters",
        action="store_true",
        default=False,
        help="Disable rig meters",
    )
    parser.add_argument(
        "--no-beacon",
        action="store_true",
        default=False,
        help="(default) Beacon is disabled at startup; use :beacon on to enable transmission.",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        default=False,
        help="Scan LAN for radios and rigctld instances, then exit",
    )
    return parser


# ---------------------------------------------------------------------------
# Startup phases
# ---------------------------------------------------------------------------


def _setup_logging(cfg: Config) -> None:
    logger.remove()
    log_file = Path("rigtop.log")
    logger.add(
        str(log_file),
        level=cfg.log_level.value,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} {level:<8} | {name} - {message}",
        rotation="5 MB",
        retention=3,
    )


def _apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> tuple[Config, bool]:
    """Apply CLI flag overrides to *cfg*. Returns (cfg, beacon_disabled)."""
    if args.log_level is not None:
        cfg.log_level = args.log_level
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
    if args.console:
        cfg.sinks = [SinkConfig(type="console")]
    # Beacon is always disabled at startup; user enables explicitly with :beacon on.
    # --no-beacon is now redundant but kept for backwards compatibility.
    return cfg, True


def _create_sinks(cfg: Config) -> tuple[list[PositionSink], set[int]]:
    """Create all configured sinks. Returns (sinks, disabled_ids)."""
    sink_cfgs = list(cfg.sinks)
    sinks = [create_sink(s.model_dump(exclude_defaults=False)) for s in sink_cfgs]
    disabled_ids = {id(sinks[i]) for i, s in enumerate(sink_cfgs) if not s.enabled}
    return sinks, disabled_ids


def _wire_buffers(
    sinks: list[PositionSink],
    dw_client: DirewolfClient | None,
    beacon_disabled: bool,
) -> tuple[AprsBuffer | None, MessageBuffer | None]:
    """Attach APRS/message buffers to the APRS-IS sink and Direwolf client."""
    aprs_buf: AprsBuffer | None = None
    msg_buf: MessageBuffer | None = None

    for sink in sinks:
        if hasattr(sink, "aprs_buffer") and hasattr(sink, "_receiver_loop"):
            aprs_buf = AprsBuffer()
            msg_buf = MessageBuffer()
            sink.aprs_buffer = aprs_buf
            sink.msg_buffer = msg_buf
            if beacon_disabled:
                sink._beacon_enabled = False
            break

    if dw_client is not None:
        if aprs_buf is None:
            aprs_buf = AprsBuffer()
        dw_client.aprs_buffer = aprs_buf

    return aprs_buf, msg_buf


def _start_rigctld(cfg: Config) -> tuple[RigctldLauncher | None, DirewolfBuffer | None]:
    """Create and start the rigctld launcher if configured."""
    if cfg.rigctld is None:
        return None, None

    rc = cfg.rigctld
    _console.print(f"  [cyan][3/8][/cyan] Starting rigctld (model {rc.model}, {rc.serial_port})…")
    buf = DirewolfBuffer()
    launcher = RigctldLauncher(
        model=rc.model,
        serial_port=rc.serial_port,
        baud_rate=rc.baud_rate,
        data_bits=rc.data_bits,
        stop_bits=rc.stop_bits,
        serial_parity=rc.serial_parity.value,
        serial_handshake=rc.serial_handshake.value,
        dtr_state=rc.dtr_state.value,
        rts_state=rc.rts_state.value,
        ptt_type=rc.ptt_type.value,
        ptt_pathname=rc.ptt_pathname,
        ptt_share=rc.ptt_share,
        listen_host=cfg.rig.host,
        listen_port=cfg.rig.port,
        log_level=cfg.log_level.value,
        stderr_callback=buf.push,
    )
    try:
        launcher.start()
    except (FileNotFoundError, RuntimeError) as e:
        _console.print(f"  [bold red]✗  Error:[/bold red] {e}")
        sys.exit(1)
    return launcher, buf


def _make_dw_launcher(
    cfg: Config, config_dir: Path
) -> tuple[DirewolfLauncher | None, DirewolfBuffer | None]:
    """Create a Direwolf launcher (started on-demand by :aprs / :packet)."""
    if cfg.direwolf is None or not cfg.direwolf.install_path:
        _console.print("  [cyan][4/8][/cyan] Direwolf launcher [dim]— disabled[/dim]")
        return None, None

    dwcfg = cfg.direwolf
    source_configs: dict[str, Path] = {}
    if dwcfg.aprs_config:
        source_configs["aprs"] = Path(dwcfg.aprs_config)
    if dwcfg.packet_config:
        source_configs["packet"] = Path(dwcfg.packet_config)

    _console.print("  [cyan][4/8][/cyan] Direwolf launcher [green]ready[/green] (on-demand)")
    buf = DirewolfBuffer()
    launcher = DirewolfLauncher(
        install_path=dwcfg.install_path,
        config_dir=config_dir,
        source_configs=source_configs,
        stderr_callback=buf.push,
        extra_args=dwcfg.extra_args,
    )
    return launcher, buf


def _connect_rig(cfg: Config, launcher: RigctldLauncher | None) -> RigctldSource:
    """Connect to rigctld; exit on failure."""
    _console.print(
        f"  [cyan][5/8][/cyan] Connecting to rig"
        f" ([bold]{cfg.rig.name}[/bold] @ {cfg.rig.host}:{cfg.rig.port})…"
    )
    rig = RigctldSource(host=cfg.rig.host, port=cfg.rig.port)
    try:
        rig.connect()
    except (ConnectionRefusedError, OSError) as e:
        if launcher:
            launcher.stop()
        _console.print(
            f"  [bold red]✗  Error:[/bold red] Could not connect to rigctld"
            f" at {cfg.rig.host}:{cfg.rig.port}\n         {e}"
        )
        sys.exit(1)
    return rig


def _wire_tui_sink(
    sinks: list[PositionSink],
    rig: RigctldSource,
    cfg: Config,
    dw_launcher: DirewolfLauncher | None,
    dw_buffer: DirewolfBuffer | None,
    rigctld_buffer: DirewolfBuffer | None,
    aprs_buf: AprsBuffer | None,
    msg_buf: MessageBuffer | None,
    dw_client: DirewolfClient | None,
) -> None:
    """Wire all references into the TUI sink and set up peer list."""
    for sink in sinks:
        if not getattr(sink, "tui", False):
            continue
        sink.aprs_buffer = aprs_buf
        sink.msg_buffer = msg_buf
        peers = [s for s in sinks if s is not sink]
        if dw_client is not None:
            peers.append(dw_client)
        sink.peers = peers
        sink.rig = rig
        sink.rig_name = cfg.rig.name
        sink.aprs_config = cfg.aprs
        sink.packet_config = cfg.bbs
        sink.dw_launcher = dw_launcher
        sink.dw_buffer = dw_buffer
        sink.rigctld_buffer = rigctld_buffer
        break


def _apply_qsy(cfg: Config, rig: RigctldSource) -> None:
    """QSY to APRS frequency/mode on startup if [aprs] is enabled."""
    if not (cfg.aprs and cfg.aprs.enabled):
        _console.print("  [cyan][6/8][/cyan] QSY [dim]— skipped (use :aprs on / :packet on)[/dim]")
        return

    _console.print(f"  [cyan][6/8][/cyan] QSY → {cfg.aprs.freq:.3f} MHz {cfg.aprs.qsy_mode}…")
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
        _console.print(
            f"  [yellow]⚠  Radio not responding[/yellow]"
            f" — is it powered on and connected? ({e})"
        )
        logger.warning("QSY failed — radio disconnected: {}", e)


def _setup_gps_fallback(cfg: Config) -> Gps2ipSource | None:
    """Connect to the GPS fallback (gps2ip) if configured."""
    if cfg.gps_fallback is None or not cfg.gps_fallback.enabled:
        _console.print("  [cyan][7/8][/cyan] GPS fallback [dim]— disabled[/dim]")
        return None

    _console.print(
        f"  [cyan][7/8][/cyan] GPS fallback → {cfg.gps_fallback.host}:{cfg.gps_fallback.port}…"
    )
    gps = Gps2ipSource(host=cfg.gps_fallback.host, port=cfg.gps_fallback.port, timeout=3.0)
    try:
        gps.connect()
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        _console.print(f"         [yellow]⚠  GPS fallback unavailable[/yellow] ({e}) — skipping")
        return None
    else:
        _console.print("         [green]✓[/green] GPS fallback connected")
        return gps


def _build_static_pos(cfg: Config) -> Position | None:
    if cfg.gps_static is not None and cfg.gps_static.enabled:
        return Position(lat=cfg.gps_static.lat, lon=cfg.gps_static.lon, alt=cfg.gps_static.alt)
    return None


def _start_sinks(
    sinks: list[PositionSink],
    dw_client: DirewolfClient | None,
    disabled_ids: set[int],
) -> None:
    if dw_client is not None:
        dw_client.start()
    for sink in sinks:
        if id(sink) in disabled_ids:
            logger.info("Sink {} disabled at startup (toggle with command)", sink)
            continue
        try:
            sink.start()
        except Exception as e:
            logger.warning("Sink {} failed to start: {}", sink, e)


def _shutdown(res: AppResources) -> None:
    _console.print(Rule("[dim]Shutting down[/dim]", style="dim"))
    _console.print(f"  [dim]Closing rig connection ({res.rig})[/dim]")
    res.rig.close()
    if res.dw_client:
        _console.print(f"  [dim]Stopping Direwolf KISS client ({res.dw_client})[/dim]")
        res.dw_client.close()
    if res.dw_launcher:
        _console.print("  [dim]Stopping Direwolf[/dim]")
        res.dw_launcher.stop()
    if res.launcher:
        _console.print("  [dim]Stopping rigctld[/dim]")
        res.launcher.stop()
    if res.gps_fallback:
        _console.print("  [dim]Closing GPS fallback[/dim]")
        res.gps_fallback.close()
    for sink in res.sinks:
        if not getattr(sink, "tui", False):
            _console.print(f"  [dim]Closing sink {sink}[/dim]")
        sink.close()
    _console.print("[green]✓ Done.[/green]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # --- LAN scan mode ---
    if args.scan:
        from rigtop.discovery import format_results, scan_lan

        _console.print("[bold]Scanning LAN for radio services…[/bold]")
        results = scan_lan(
            progress_cb=lambda done, total: _console.print(
                f"  [dim]{done}/{total}[/dim]", end="\r"
            ),
        )
        _console.print(format_results(results))
        return

    _console.print(Rule("[bold cyan]rigtop[/bold cyan] starting", style="cyan dim"))

    # [1/8] Config
    _console.print("  [cyan][1/8][/cyan] Loading config…")
    cfg = load_config(args.config)
    cfg, beacon_disabled = _apply_cli_overrides(cfg, args)
    _setup_logging(cfg)

    # [2/8] Sinks
    _console.print("  [cyan][2/8][/cyan] Creating sinks…")
    sinks, disabled_ids = _create_sinks(cfg)

    # Wire APRS-IS sink buffers
    dw_client: DirewolfClient | None = None
    if cfg.direwolf is not None:
        dw_client = DirewolfClient(host=cfg.direwolf.host, port=cfg.direwolf.port)
    aprs_buf, msg_buf = _wire_buffers(sinks, dw_client, beacon_disabled)

    # [3/8] rigctld
    launcher, rigctld_buffer = _start_rigctld(cfg)
    if launcher is None:
        _console.print("  [cyan][3/8][/cyan] rigctld [dim]— skipped (--no-rigctld)[/dim]")

    # [4/8] Direwolf launcher
    config_dir = args.config.parent if args.config else Path.cwd()
    dw_launcher, dw_buffer = _make_dw_launcher(cfg, config_dir)

    # [5/8] Connect to rig
    rig = _connect_rig(cfg, launcher)

    # Wire TUI sink
    _wire_tui_sink(
        sinks,
        rig,
        cfg,
        dw_launcher,
        dw_buffer,
        rigctld_buffer,
        aprs_buf,
        msg_buf,
        dw_client,
    )

    # [6/8] QSY
    _apply_qsy(cfg, rig)

    # [7/8] GPS fallback
    gps_fallback = _setup_gps_fallback(cfg)

    # [8/8] Start sinks
    _console.print("  [cyan][8/8][/cyan] Starting sinks…")
    _start_sinks(sinks, dw_client, disabled_ids)

    static_pos = _build_static_pos(cfg)

    res = AppResources(
        rig=rig,
        sinks=sinks,
        launcher=launcher,
        dw_launcher=dw_launcher,
        dw_client=dw_client,
        gps_fallback=gps_fallback,
        static_pos=static_pos,
    )

    _console.print(Rule("[bold green]Ready ✓[/bold green]", style="green dim"))
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
                rig_config=cfg.rig,
                interval=cfg.interval,
                meters=cfg.meters,
                gps_fallback=gps_fallback,
                static_pos=static_pos,
                watchdog=cfg.watchdog,
                beacon_disabled=beacon_disabled,
            )
            app.run()
        else:
            run(
                rig,
                sinks,
                interval=cfg.interval,
                once=cfg.once,
                meters=cfg.meters,
                gps_fallback=gps_fallback,
                watchdog=cfg.watchdog,
                static_pos=static_pos,
            )
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error — rigtop exiting")
        sys.exit(1)
    finally:
        _shutdown(res)


if __name__ == "__main__":
    main()
