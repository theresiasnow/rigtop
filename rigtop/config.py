"""TOML configuration loader for rigtop."""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class RigConfig(BaseModel):
    name: str = "default"
    host: str = "127.0.0.1"
    port: int = Field(default=4532, ge=1, le=65535)


class Parity(StrEnum):
    NONE = "None"
    ODD = "Odd"
    EVEN = "Even"
    MARK = "Mark"
    SPACE = "Space"


class Handshake(StrEnum):
    NONE = "None"
    XONXOFF = "XONXOFF"
    HARDWARE = "Hardware"


class DtrRtsState(StrEnum):
    UNSET = "Unset"
    ON = "ON"
    OFF = "OFF"


class PttType(StrEnum):
    RIG = "RIG"
    RIGMICDATA = "RIGMICDATA"
    DTR = "DTR"
    RTS = "RTS"
    PARALLEL = "Parallel"
    CM108 = "CM108"
    GPIO = "GPIO"
    GPION = "GPION"
    NONE = "None"


class RigctldConfig(BaseModel):
    model: int = 3085
    serial_port: str = "COM9"
    baud_rate: int = 19200
    data_bits: Literal[5, 6, 7, 8] = 8
    stop_bits: Literal[1, 2] = 1
    serial_parity: Parity = Parity.NONE
    serial_handshake: Handshake = Handshake.NONE
    dtr_state: DtrRtsState = DtrRtsState.UNSET
    rts_state: DtrRtsState = DtrRtsState.UNSET
    ptt_type: PttType = PttType.RIG
    ptt_pathname: str = ""
    ptt_share: bool = False

    @field_validator("baud_rate")
    @classmethod
    def _valid_baud(cls, v: int) -> int:
        valid = {1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200}
        if v not in valid:
            raise ValueError(f"baud_rate must be one of {sorted(valid)}")
        return v


class GpsConfig(BaseModel):
    enabled: bool = True
    host: str = "192.168.1.1"
    port: int = Field(default=11123, ge=1, le=65535)


class GpsStaticConfig(BaseModel):
    """Hard-coded GPS position — used when rig and fallback have no fix."""
    enabled: bool = True
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    alt: float | None = None


class WatchdogConfig(BaseModel):
    """TX watchdog — force PTT off if transmitting too long."""
    tx_timeout: int = Field(default=120, ge=10)  # seconds


class DirewolfConfig(BaseModel):
    """Direwolf KISS TCP + optional launcher."""
    host: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)
    install_path: str | None = None   # set to enable launcher
    extra_args: list[str] = Field(default_factory=list)


class BbsConfig(BaseModel):
    """Packet BBS settings — frequency/mode for :bbs command."""
    enabled: bool = False
    freq: float = 144.675     # packet frequency in MHz
    mode: str = "PKTFM"       # rig mode (PKTFM for 1200 baud, PKTUSB for 9600)


class AprsConfig(BaseModel):
    """APRS settings — QSY frequency/mode for :aprs on."""
    enabled: bool = False
    freq: float = 0            # QSY rig to this freq (MHz)
    qsy_mode: str = ""         # rig mode (e.g. FM, USB)


class SinkConfig(BaseModel):
    type: Literal["aprsis", "civ_proxy", "console", "nmea", "gpsd", "tui", "wsjtx"]
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)  # 0 = use sink-type default
    nmea: bool = False
    device: str = ""          # serial port for nmea/civ_proxy sink
    baudrate: int = 4800       # serial baud rate for nmea/civ_proxy sink
    rig_addr: int = 0          # CI-V address override (0 = auto from rig_name)
    rig_name: str = ""         # rig model for CI-V address lookup
    callsign: str = ""        # APRS-IS callsign (aprsis sink)
    server: str = ""           # APRS-IS server host (aprsis sink)
    passcode: str = ""         # APRS-IS passcode (aprsis sink)
    comment: str = "rigtop"   # beacon comment (aprsis sink)
    interval: int = 120        # beacon interval seconds (aprsis sink)
    aprs_filter: str = ""     # APRS-IS server filter (e.g. r/59.2/18.1/200)

    _PORT_DEFAULTS: dict[str, int] = {
        "aprsis": 14580,
        "gpsd": 2947,
        "wsjtx": 2237,
        "nmea": 10110,
    }

    @model_validator(mode="after")
    def _apply_default_port(self) -> SinkConfig:
        if self.port == 0:
            self.port = self._PORT_DEFAULTS.get(self.type, 0)
        return self


class Config(BaseModel):
    """Parsed rigtop configuration."""

    model_config = {"validate_assignment": True}

    interval: float = Field(default=0.5, gt=0)
    once: bool = False
    meters: bool = True
    log_level: LogLevel = LogLevel.WARNING
    rigs: list[RigConfig] = Field(default_factory=lambda: [RigConfig()])
    rig: RigConfig = Field(default_factory=RigConfig)
    rigctld: RigctldConfig | None = None
    gps_fallback: GpsConfig | None = None
    gps_static: GpsStaticConfig | None = None
    aprs: AprsConfig | None = None
    bbs: BbsConfig | None = None
    direwolf: DirewolfConfig | None = None
    watchdog: WatchdogConfig | None = None
    sinks: list[SinkConfig] = Field(default_factory=lambda: [SinkConfig(type="tui")])

    @model_validator(mode="after")
    def _select_first_rig(self) -> Config:
        if self.rigs and self.rig == RigConfig():
            # Bypass validate_assignment to avoid infinite recursion.
            object.__setattr__(self, "rig", self.rigs[0])
        return self

    def select_rig(self, name: str | None = None) -> None:
        """Set *self.rig* to the rig matching *name* (first rig if None)."""
        if name is None:
            object.__setattr__(self, "rig", self.rigs[0])
            return
        for r in self.rigs:
            if r.name.lower() == name.lower():
                object.__setattr__(self, "rig", r)
                return
        available = ", ".join(r.name for r in self.rigs)
        raise ValueError(f"Unknown rig '{name}'. Available: {available}")


def _parse_rigs(data: dict) -> list[RigConfig]:
    """Parse rig entries.  Supports both ``[rig]`` (single) and ``[[rig]]`` (list)."""
    raw = data.get("rig")
    if raw is None:
        return [RigConfig()]
    if isinstance(raw, dict):
        raw.setdefault("name", "default")
        return [RigConfig(**raw)]
    rigs: list[RigConfig] = []
    for i, entry in enumerate(raw):
        entry.setdefault("name", f"rig{i + 1}")
        rigs.append(RigConfig(**entry))
    return rigs if rigs else [RigConfig()]


def _parse_sinks(data: dict) -> list[SinkConfig]:
    """Parse all sink entries (including disabled ones)."""
    raw = data.get("sink", [{"type": "tui"}])
    if isinstance(raw, dict):
        raw = [raw]
    return [SinkConfig(**entry) for entry in raw]


def load_config(path: Path | None) -> Config:
    """Load configuration from a TOML file.

    If *path* is None, auto-discovers ``rigtop.toml`` in the current directory.
    Returns defaults if no file is found.
    """
    if path is None:
        auto = Path("rigtop.toml")
        if auto.is_file():
            path = auto
        else:
            return Config()

    with path.open("rb") as f:
        data = tomllib.load(f)

    general = data.get("general", {})
    rigs = _parse_rigs(data)
    rigctld_raw = data.get("rigctld")
    gps_raw = data.get("gps_fallback")
    gps_static_raw = data.get("gps_static")
    aprs_raw = data.get("aprs")
    bbs_raw = data.get("bbs")
    direwolf_raw = data.get("direwolf")
    watchdog_raw = data.get("watchdog")
    sinks = _parse_sinks(data)

    cfg = Config(
        interval=general.get("interval", 2.0),
        once=general.get("once", False),
        meters=general.get("meters", True),
        log_level=general.get("log_level", "WARNING").upper(),
        rigs=rigs,
        rigctld=RigctldConfig(**rigctld_raw) if rigctld_raw else None,
        gps_fallback=GpsConfig(**gps_raw) if gps_raw else None,
        gps_static=GpsStaticConfig(**gps_static_raw) if gps_static_raw else None,
        aprs=AprsConfig(**aprs_raw) if aprs_raw else None,
        bbs=BbsConfig(**bbs_raw) if bbs_raw else None,
        direwolf=DirewolfConfig(**direwolf_raw) if direwolf_raw else None,
        watchdog=WatchdogConfig(**watchdog_raw) if watchdog_raw else None,
        sinks=sinks,
    )
    cfg.select_rig()
    return cfg
