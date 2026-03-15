"""GPS source abstraction and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Position:
    """A GPS position fix."""

    lat: float
    lon: float


class GpsSource(ABC):
    """Base class for GPS position sources."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def get_position(self) -> Position | None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()


# --- Source registry ---

_SOURCE_TYPES: dict[str, type[GpsSource]] = {}


def register_source(name: str):
    """Decorator to register a GPS source type."""

    def decorator(cls: type[GpsSource]):
        _SOURCE_TYPES[name] = cls
        return cls

    return decorator


def create_source(config: dict[str, Any]) -> GpsSource:
    """Create a GPS source from a config dict.  Expects a 'type' key."""
    source_type = config.get("type", "rigctld")
    cls = _SOURCE_TYPES.get(source_type)
    if cls is None:
        available = ", ".join(sorted(_SOURCE_TYPES))
        raise ValueError(
            f"Unknown source type '{source_type}'. Available: {available}"
        )
    # Pass remaining config as kwargs
    kwargs = {k: v for k, v in config.items() if k != "type"}
    return cls(**kwargs)


# Import concrete sources to trigger registration
import rigtop.sources.gps2ip as _gps2ip  # noqa: F401, E402
import rigtop.sources.rigctld as _rigctld  # noqa: F401, E402
