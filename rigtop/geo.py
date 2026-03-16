"""Geographic utility functions: Maidenhead, NMEA sentence building, formatting."""

import datetime


def maidenhead(lat: float, lon: float) -> str:
    """Convert lat/lon to Maidenhead grid locator (6 chars)."""
    lon += 180
    lat += 90
    field_lon = int(lon / 20)
    field_lat = int(lat / 10)
    square_lon = int((lon % 20) / 2)
    square_lat = int(lat % 10)
    sub_lon = int((lon - field_lon * 20 - square_lon * 2) * 12)
    sub_lat = int((lat - field_lat * 10 - square_lat) * 24)
    return (
        chr(ord("A") + field_lon)
        + chr(ord("A") + field_lat)
        + str(square_lon)
        + str(square_lat)
        + chr(ord("a") + sub_lon)
        + chr(ord("a") + sub_lat)
    )


def format_position(lat: float, lon: float) -> str:
    """Format position as human-readable degrees/minutes string."""
    lat_dir = "N" if lat >= 0 else "S"
    lon_dir = "E" if lon >= 0 else "W"
    lat_deg = int(abs(lat))
    lat_min = (abs(lat) - lat_deg) * 60
    lon_deg = int(abs(lon))
    lon_min = (abs(lon) - lon_deg) * 60
    return f"{lat_deg}°{lat_min:06.3f}'{lat_dir}  {lon_deg}°{lon_min:06.3f}'{lon_dir}"


def _nmea_checksum(body: str) -> str:
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"{checksum:02X}"


def decimal_to_nmea_lat(lat: float) -> tuple[str, str]:
    """Convert decimal degrees latitude to NMEA format (DDMM.MMMM, N/S)."""
    direction = "N" if lat >= 0 else "S"
    lat = abs(lat)
    degrees = int(lat)
    minutes = (lat - degrees) * 60
    return (f"{degrees:02d}{minutes:07.4f}", direction)


def decimal_to_nmea_lon(lon: float) -> tuple[str, str]:
    """Convert decimal degrees longitude to NMEA format (DDDMM.MMMM, E/W)."""
    direction = "E" if lon >= 0 else "W"
    lon = abs(lon)
    degrees = int(lon)
    minutes = (lon - degrees) * 60
    return (f"{degrees:03d}{minutes:07.4f}", direction)


def build_gga_sentence(lat: float, lon: float) -> str:
    """Build a GPGGA NMEA sentence from position data."""
    now = datetime.datetime.now(datetime.UTC)
    time_str = now.strftime("%H%M%S.00")
    lat_str, lat_dir = decimal_to_nmea_lat(lat)
    lon_str, lon_dir = decimal_to_nmea_lon(lon)
    body = f"GPGGA,{time_str},{lat_str},{lat_dir},{lon_str},{lon_dir},1,00,1.0,0.0,M,0.0,M,,"
    return f"${body}*{_nmea_checksum(body)}"


def build_rmc_sentence(lat: float, lon: float) -> str:
    """Build a GPRMC NMEA sentence from position data."""
    now = datetime.datetime.now(datetime.UTC)
    time_str = now.strftime("%H%M%S.00")
    date_str = now.strftime("%d%m%y")
    lat_str, lat_dir = decimal_to_nmea_lat(lat)
    lon_str, lon_dir = decimal_to_nmea_lon(lon)
    body = f"GPRMC,{time_str},A,{lat_str},{lat_dir},{lon_str},{lon_dir},0.0,0.0,{date_str},,,"
    return f"${body}*{_nmea_checksum(body)}"
