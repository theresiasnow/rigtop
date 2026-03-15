"""
nmead - Read GPS/NMEA data from IC-705 via rigctld (Hamlib)

Connects to a running rigctld instance and polls GPS position data
from an Icom IC-705's built-in GPS receiver.
"""

import argparse
import datetime
import socket
import sys
import time


class RigctldClient:
    """TCP client for communicating with rigctld."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def _send_command(self, cmd: str) -> str:
        if not self._sock:
            raise ConnectionError("Not connected to rigctld")
        self._sock.sendall((cmd + "\n").encode())
        response = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                # rigctld terminates responses with newline
                if response.endswith(b"\n"):
                    break
            except socket.timeout:
                break
        return response.decode().strip()

    def get_position(self) -> tuple[float, float] | None:
        """Get GPS position from rig. Returns (latitude, longitude) or None."""
        resp = self._send_command("+\\get_position")
        lines = resp.splitlines()
        lat = None
        lon = None
        for line in lines:
            line = line.strip()
            if line.startswith("Latitude:"):
                try:
                    lat = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("Longitude:"):
                try:
                    lon = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        # Fallback: simple two-line response (lat\nlon)
        if lat is None and lon is None and len(lines) >= 2:
            try:
                lat = float(lines[0])
                lon = float(lines[1])
            except ValueError:
                return None
        if lat is not None and lon is not None:
            return (lat, lon)
        return None

    def get_frequency(self) -> str | None:
        """Get current frequency from rig."""
        resp = self._send_command("+\\get_freq")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("Frequency:"):
                return line.split(":", 1)[1].strip()
        # Fallback: simple response
        lines = resp.splitlines()
        if lines:
            try:
                float(lines[0])
                return lines[0].strip()
            except ValueError:
                pass
        return None

    def get_mode(self) -> str | None:
        """Get current mode from rig."""
        resp = self._send_command("+\\get_mode")
        for line in resp.splitlines():
            line = line.strip()
            if line.startswith("Mode:"):
                return line.split(":", 1)[1].strip()
        lines = resp.splitlines()
        return lines[0].strip() if lines else None


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
    now = datetime.datetime.now(datetime.timezone.utc)
    time_str = now.strftime("%H%M%S.00")
    lat_str, lat_dir = decimal_to_nmea_lat(lat)
    lon_str, lon_dir = decimal_to_nmea_lon(lon)
    # GGA: time, lat, N/S, lon, E/W, quality(1=GPS fix), sats, hdop, alt, M, geoid, M, ...
    body = f"GPGGA,{time_str},{lat_str},{lat_dir},{lon_str},{lon_dir},1,00,1.0,0.0,M,0.0,M,,"
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


def build_rmc_sentence(lat: float, lon: float) -> str:
    """Build a GPRMC NMEA sentence from position data."""
    now = datetime.datetime.now(datetime.timezone.utc)
    time_str = now.strftime("%H%M%S.00")
    date_str = now.strftime("%d%m%y")
    lat_str, lat_dir = decimal_to_nmea_lat(lat)
    lon_str, lon_dir = decimal_to_nmea_lon(lon)
    # RMC: time, status, lat, N/S, lon, E/W, speed, course, date, mag_var, E/W
    body = f"GPRMC,{time_str},A,{lat_str},{lat_dir},{lon_str},{lon_dir},0.0,0.0,{date_str},,,"
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


def format_position(lat: float, lon: float) -> str:
    """Format position as human-readable string."""
    lat_dir = "N" if lat >= 0 else "S"
    lon_dir = "E" if lon >= 0 else "W"
    lat_deg = int(abs(lat))
    lat_min = (abs(lat) - lat_deg) * 60
    lon_deg = int(abs(lon))
    lon_min = (abs(lon) - lon_deg) * 60
    return f"{lat_deg}°{lat_min:06.3f}'{lat_dir}  {lon_deg}°{lon_min:06.3f}'{lon_dir}"


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


def run_loop(client: RigctldClient, interval: float, nmea_output: bool, one_shot: bool):
    """Main polling loop."""
    print(f"Connected to rigctld at {client.host}:{client.port}")
    print(f"Polling GPS position every {interval}s  (Ctrl+C to stop)\n")

    while True:
        try:
            pos = client.get_position()
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if pos is None:
                print(f"[{now_str}] No GPS fix available")
            else:
                lat, lon = pos
                grid = maidenhead(lat, lon)
                print(f"[{now_str}] Position: {format_position(lat, lon)}  Grid: {grid}")
                print(f"           Decimal:  {lat:.6f}, {lon:.6f}")

                freq = client.get_frequency()
                mode = client.get_mode()
                if freq or mode:
                    freq_mhz = f"{float(freq) / 1e6:.6f} MHz" if freq else "?"
                    print(f"           Rig:      {freq_mhz}  {mode or '?'}")

                if nmea_output:
                    gga = build_gga_sentence(lat, lon)
                    rmc = build_rmc_sentence(lat, lon)
                    print(f"           GGA: {gga}")
                    print(f"           RMC: {rmc}")

            print()

            if one_shot:
                break

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except ConnectionError as e:
            print(f"Connection lost: {e}")
            break


def main():
    parser = argparse.ArgumentParser(
        description="Read GPS/NMEA data from IC-705 via rigctld (Hamlib)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="rigctld host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=4532, help="rigctld port (default: 4532)"
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="Poll interval in seconds (default: 2.0)"
    )
    parser.add_argument(
        "--nmea", action="store_true", help="Output NMEA GGA/RMC sentences"
    )
    parser.add_argument(
        "--once", action="store_true", help="Read position once and exit"
    )
    args = parser.parse_args()

    client = RigctldClient(args.host, args.port)
    try:
        client.connect()
    except (ConnectionRefusedError, OSError) as e:
        print(f"Error: Could not connect to rigctld at {args.host}:{args.port}")
        print(f"       {e}")
        print()
        print("Make sure rigctld is running, e.g.:")
        print("  rigctld -m 3085 -r /dev/ttyUSB0 -s 115200")
        print("  rigctld -m 3085 -r COM3 -s 115200")
        sys.exit(1)

    try:
        run_loop(client, args.interval, args.nmea, args.once)
    finally:
        client.close()


if __name__ == "__main__":
    main()
