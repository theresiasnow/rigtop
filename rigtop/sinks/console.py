"""Position sink: terminal/console output."""

from rigtop.geo import build_gga_sentence, build_rmc_sentence, format_position
from rigtop.sinks import PositionSink, register_sink
from rigtop.sources import Position


@register_sink("console")
class ConsoleSink(PositionSink):
    """Print position data to the terminal."""

    def __init__(self, nmea: bool = False):
        self.nmea = nmea

    def start(self) -> None:
        pass

    def send(self, pos: Position, grid: str, **kwargs) -> str | None:
        print(f"  Position: {format_position(pos.lat, pos.lon)}  Grid: {grid}")
        print(f"  Decimal:  {pos.lat:.6f}, {pos.lon:.6f}")
        if self.nmea:
            gga = build_gga_sentence(pos.lat, pos.lon)
            rmc = build_rmc_sentence(pos.lat, pos.lon)
            print(f"  GGA: {gga}")
            print(f"  RMC: {rmc}")
        return None

    def close(self) -> None:
        pass

    def __str__(self) -> str:
        return "console" + (" +nmea" if self.nmea else "")
