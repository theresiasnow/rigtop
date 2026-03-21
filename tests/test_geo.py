"""Unit tests for rigtop.geo — pure functions, no I/O."""

import pytest

from rigtop.geo import (
    _nmea_checksum,
    decimal_to_nmea_lat,
    decimal_to_nmea_lon,
    format_position,
    maidenhead,
)


class TestMaidenhead:
    def test_known_grid_london(self):
        # London: ~51.5°N, 0°W → IO91wm
        grid = maidenhead(51.5, -0.1)
        assert grid == "IO91wm"

    def test_known_grid_new_york(self):
        # New York: ~40.7°N, 74°W → FN30aq
        grid = maidenhead(40.7, -74.0)
        assert grid == "FN30aq"

    def test_returns_six_chars(self):
        grid = maidenhead(0.0, 0.0)
        assert len(grid) == 6

    def test_field_letters_uppercase(self):
        grid = maidenhead(10.0, 20.0)
        assert grid[0].isupper() and grid[1].isupper()

    def test_subsquare_letters_lowercase(self):
        grid = maidenhead(10.0, 20.0)
        assert grid[4].islower() and grid[5].islower()

    def test_square_digits(self):
        grid = maidenhead(10.0, 20.0)
        assert grid[2].isdigit() and grid[3].isdigit()

    def test_north_pole_boundary(self):
        # Should not raise at extreme latitudes
        grid = maidenhead(89.9, 0.0)
        assert len(grid) == 6

    def test_south_pole_boundary(self):
        grid = maidenhead(-89.9, 0.0)
        assert len(grid) == 6

    def test_antimeridian(self):
        grid = maidenhead(0.0, 179.9)
        assert len(grid) == 6

    def test_west_antimeridian(self):
        grid = maidenhead(0.0, -179.9)
        assert len(grid) == 6


class TestFormatPosition:
    def test_north_east(self):
        result = format_position(59.33, 18.07)
        assert "N" in result
        assert "E" in result

    def test_south_west(self):
        result = format_position(-33.86, -70.65)
        assert "S" in result
        assert "W" in result

    def test_equator_prime_meridian(self):
        result = format_position(0.0, 0.0)
        # 0° is treated as N/E
        assert "N" in result or "S" in result
        assert "E" in result or "W" in result

    def test_degrees_minutes_format(self):
        result = format_position(60.0, 25.0)
        assert "°" in result
        assert "'" in result


class TestNmeaChecksum:
    def test_known_checksum(self):
        # $GPGLL,5300.0000,N,00600.0000,E,*checksum
        body = "GPGLL,5300.0000,N,00600.0000,E,"
        cs = _nmea_checksum(body)
        # XOR of all chars
        expected = 0
        for ch in body:
            expected ^= ord(ch)
        assert cs == f"{expected:02X}"

    def test_empty_string(self):
        assert _nmea_checksum("") == "00"

    def test_two_hex_digits(self):
        cs = _nmea_checksum("GPRMC,anything")
        assert len(cs) == 2
        assert all(c in "0123456789ABCDEF" for c in cs)


class TestDecimalToNmea:
    def test_lat_north(self):
        val, direction = decimal_to_nmea_lat(59.5)
        assert direction == "N"
        assert "." in val

    def test_lat_south(self):
        val, direction = decimal_to_nmea_lat(-33.9)
        assert direction == "S"

    def test_lat_format_ddmm(self):
        val, _ = decimal_to_nmea_lat(59.5)
        # Should be DDMM.MMMM — degrees part is 2 digits
        degrees_part = val.split(".")[0][:2]
        assert degrees_part.isdigit()

    def test_lon_east(self):
        val, direction = decimal_to_nmea_lon(18.07)
        assert direction == "E"

    def test_lon_west(self):
        val, direction = decimal_to_nmea_lon(-74.0)
        assert direction == "W"

    def test_lon_format_dddmm(self):
        val, _ = decimal_to_nmea_lon(18.07)
        # Should be DDDMM.MMMM — degrees part is 3 digits
        degrees_part = val.split(".")[0][:3]
        assert degrees_part.isdigit()

    def test_zero_lat(self):
        val, direction = decimal_to_nmea_lat(0.0)
        assert direction == "N"

    def test_zero_lon(self):
        val, direction = decimal_to_nmea_lon(0.0)
        assert direction == "E"
