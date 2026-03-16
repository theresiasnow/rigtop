"""Position sink abstraction and registry."""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any

from rigtop.sources import Position


class PositionSink(ABC):
    """Base class for position output destinations."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        """Send position data. Returns optional status message for logging.

        Optional kwargs (sources may provide):
            freq:    Frequency string (Hz)
            mode:    Operating mode string
            meters:  dict[str, float] of rig meter readings
        """

    @abstractmethod
    def close(self) -> None: ...

    def connections(self) -> list[dict[str, Any]]:
        """Return a list of active connection descriptors for the TUI.

        Each dict has keys: label, kind (serial/tcp/udp), status, clients.
        Override in subclasses that manage connections.
        """
        return []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()


# --- Sink registry ---

_SINK_TYPES: dict[str, type[PositionSink]] = {}


def register_sink(name: str):
    """Decorator to register a position sink type."""

    def decorator(cls: type[PositionSink]):
        _SINK_TYPES[name] = cls
        return cls

    return decorator


def create_sink(config: dict[str, Any]) -> PositionSink:
    """Create a position sink from a config dict.  Expects a 'type' key."""
    sink_type = config.get("type", "console")
    cls = _SINK_TYPES.get(sink_type)
    if cls is None:
        available = ", ".join(sorted(_SINK_TYPES))
        raise ValueError(
            f"Unknown sink type '{sink_type}'. Available: {available}"
        )
    kwargs = {k: v for k, v in config.items() if k != "type"}
    # Only pass kwargs the constructor actually accepts.
    sig = inspect.signature(cls.__init__)
    if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        accepted = set(sig.parameters) - {"self"}
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    return cls(**kwargs)


# Import concrete sinks to trigger registration
import rigtop.sinks.aprsis as _aprsis  # noqa: F401, E402
import rigtop.sinks.console as _console  # noqa: F401, E402
import rigtop.sinks.direwolf as _direwolf  # noqa: F401, E402
import rigtop.sinks.gpsd as _gpsd  # noqa: F401, E402
import rigtop.sinks.tui as _tui  # noqa: F401, E402
import rigtop.sinks.wsjtx as _wsjtx  # noqa: F401, E402
