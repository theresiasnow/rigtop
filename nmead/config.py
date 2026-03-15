"""TOML configuration loader for nmead."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        import tomli as tomllib  # type: ignore[import-not-found, no-redef]


@dataclass
class Config:
    """Parsed nmead configuration."""

    interval: float = 2.0
    once: bool = False
    meters: bool = False
    rig: dict = field(default_factory=lambda: {"host": "127.0.0.1", "port": 4532})
    gps_fallback: dict | None = None
    sinks: list[dict] = field(default_factory=lambda: [{"type": "console"}])


def load_config(path: Path | None) -> Config:
    """Load configuration from a TOML file. Returns defaults if path is None."""
    if path is None:
        return Config()

    with open(path, "rb") as f:
        data = tomllib.load(f)

    general = data.get("general", {})
    rig = data.get("rig", {"host": "127.0.0.1", "port": 4532})
    gps_fallback = data.get("gps_fallback")
    sinks = data.get("sink", [{"type": "console"}])

    # Ensure sinks is a list (TOML [[sink]] gives a list, [sink] gives a dict)
    if isinstance(sinks, dict):
        sinks = [sinks]

    return Config(
        interval=general.get("interval", 2.0),
        once=general.get("once", False),
        meters=general.get("meters", False),
        rig=rig,
        gps_fallback=gps_fallback,
        sinks=sinks,
    )
