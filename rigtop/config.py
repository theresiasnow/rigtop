"""TOML configuration loader for rigtop."""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        import tomli as tomllib  # type: ignore[import-not-found, no-redef]


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class RigConfig(BaseModel):
    name: str = "default"
    host: str = "127.0.0.1"
    port: int = Field(default=4532, ge=1, le=65535)


class Parity(str, Enum):
    NONE = "None"
    ODD = "Odd"
    EVEN = "Even"
    MARK = "Mark"
    SPACE = "Space"


class Handshake(str, Enum):
    NONE = "None"
    XONXOFF = "XONXOFF"
    HARDWARE = "Hardware"


class DtrRtsState(str, Enum):
    UNSET = "Unset"
    ON = "ON"
    OFF = "OFF"


class PttType(str, Enum):
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
    host: str = "192.168.1.1"
    port: int = Field(default=11123, ge=1, le=65535)


class SinkConfig(BaseModel):
    type: Literal["console", "direwolf", "gpsd", "tui", "wsjtx"]
    host: str = "127.0.0.1"
    port: int = Field(default=2237, ge=1, le=65535)
    nmea: bool = False
    device: str = ""          # serial port for direwolf sink (e.g. COM10)
    baudrate: int = 4800       # serial baud rate for direwolf sink


class Config(BaseModel):
    """Parsed rigtop configuration."""

    model_config = {"validate_assignment": True}

    interval: float = Field(default=2.0, gt=0)
    once: bool = False
    meters: bool = True
    log_level: LogLevel = LogLevel.WARNING
    rigs: list[RigConfig] = Field(default_factory=lambda: [RigConfig()])
    rig: RigConfig = Field(default_factory=RigConfig)
    rigctld: RigctldConfig | None = None
    gps_fallback: GpsConfig | None = None
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
    """Parse sink entries."""
    raw = data.get("sink", [{"type": "tui"}])
    if isinstance(raw, dict):
        raw = [raw]
    return [SinkConfig(**s) for s in raw]


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

    with open(path, "rb") as f:
        data = tomllib.load(f)

    general = data.get("general", {})
    rigs = _parse_rigs(data)
    rigctld_raw = data.get("rigctld")
    gps_raw = data.get("gps_fallback")
    sinks = _parse_sinks(data)

    cfg = Config(
        interval=general.get("interval", 2.0),
        once=general.get("once", False),
        meters=general.get("meters", True),
        log_level=general.get("log_level", "WARNING").upper(),
        rigs=rigs,
        rigctld=RigctldConfig(**rigctld_raw) if rigctld_raw else None,
        gps_fallback=GpsConfig(**gps_raw) if gps_raw else None,
        sinks=sinks,
    )
    cfg.select_rig()
    return cfg
