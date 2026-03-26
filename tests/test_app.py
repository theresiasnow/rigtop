"""Unit tests for rigtop.app — TxWatchdog, resolve_position, collect_meters."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rigtop.app import TxWatchdog, collect_meters, resolve_position
from rigtop.config import WatchdogConfig
from rigtop.sources import Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rig(pos=None, freq=145_500_000, mode="FM", ptt=False, strength=None):
    """Minimal RigctldSource mock."""
    rig = MagicMock()
    rig.get_position.return_value = pos
    rig.get_frequency.return_value = freq
    rig.get_mode_and_passband.return_value = (mode, 15000)
    rig.get_ptt.return_value = ptt
    rig.get_strength.return_value = strength
    rig.get_meters.return_value = {}
    rig.get_level.return_value = None
    return rig


def _gps(pos=None):
    gps = MagicMock()
    gps.get_position.return_value = pos
    return gps


# ---------------------------------------------------------------------------
# resolve_position
# ---------------------------------------------------------------------------

class TestResolvePosition:
    _pos = Position(lat=59.33, lon=18.07)
    _fallback_pos = Position(lat=60.0, lon=20.0)
    _static_pos = Position(lat=0.0, lon=0.0)

    def test_rig_position_used_first(self):
        rig = _rig(pos=self._pos)
        pos, src = resolve_position(rig, None, None)
        assert pos == self._pos
        assert src == "rig"

    def test_fallback_used_when_rig_has_no_fix(self):
        rig = _rig(pos=None)
        gps = _gps(pos=self._fallback_pos)
        pos, src = resolve_position(rig, gps, None)
        assert pos == self._fallback_pos
        assert src == "fallback"

    def test_static_used_when_both_missing(self):
        rig = _rig(pos=None)
        gps = _gps(pos=None)
        pos, src = resolve_position(rig, gps, self._static_pos)
        assert pos == self._static_pos
        assert src == "static"

    def test_returns_none_when_all_missing(self):
        rig = _rig(pos=None)
        pos, src = resolve_position(rig, None, None)
        assert pos is None
        assert src == "none"

    def test_fallback_not_called_when_rig_has_fix(self):
        rig = _rig(pos=self._pos)
        gps = _gps(pos=self._fallback_pos)
        resolve_position(rig, gps, None)
        gps.get_position.assert_not_called()

    def test_static_not_used_when_fallback_works(self):
        rig = _rig(pos=None)
        gps = _gps(pos=self._fallback_pos)
        pos, src = resolve_position(rig, gps, self._static_pos)
        assert src == "fallback"


# ---------------------------------------------------------------------------
# collect_meters
# ---------------------------------------------------------------------------

class TestCollectMeters:
    def test_strength_included(self):
        rig = _rig(strength=-10.0)
        m = collect_meters(rig)
        assert m["STRENGTH"] == pytest.approx(-10.0)

    def test_no_strength_skipped(self):
        rig = _rig(strength=None)
        m = collect_meters(rig)
        assert "STRENGTH" not in m

    def test_rfpower_included_when_present(self):
        rig = _rig()
        rig.get_level.return_value = 0.75
        m = collect_meters(rig)
        assert m["RFPOWER"] == pytest.approx(0.75)

    def test_rfpower_skipped_when_none(self):
        rig = _rig()
        rig.get_level.return_value = None
        m = collect_meters(rig)
        assert "RFPOWER" not in m

    def test_get_meters_values_merged(self):
        rig = _rig()
        rig.get_meters.return_value = {"SWR": 1.5, "ALC": 0.3}
        m = collect_meters(rig)
        assert m["SWR"] == pytest.approx(1.5)
        assert m["ALC"] == pytest.approx(0.3)

    def test_returns_empty_dict_when_nothing_available(self):
        rig = _rig(strength=None)
        m = collect_meters(rig)
        assert isinstance(m, dict)


# ---------------------------------------------------------------------------
# TxWatchdog
# ---------------------------------------------------------------------------

class TestTxWatchdog:
    def _wd(self, timeout=30):
        return TxWatchdog(WatchdogConfig(tx_timeout=timeout))

    def test_not_tripped_initially(self):
        wd = self._wd()
        assert not wd.tripped

    def test_no_trip_within_timeout(self):
        wd = self._wd(timeout=30)
        rig = _rig()
        extras = {"ptt": True}
        with patch("rigtop.app.time.monotonic", side_effect=[0.0, 10.0, 10.0]):
            wd.update(True, rig, extras, None)
        assert not wd.tripped
        rig.set_ptt.assert_not_called()

    def test_trips_and_forces_ptt_off_after_timeout(self):
        wd = self._wd(timeout=30)
        rig = _rig()
        extras = {"ptt": True}
        # First call: TX starts (edge: False→True triggers _tx_start = t0)
        # monotonic calls: edge detection t0, then watchdog check t1 > timeout
        with patch("rigtop.app.time.monotonic", side_effect=[0.0, 0.0, 60.0, 60.0]):
            wd.update(True, rig, extras, None)   # starts TX, t_start=0
            wd.update(True, rig, extras, None)   # 60s later → trip
        assert wd.tripped
        rig.set_ptt.assert_called_once_with(False)
        assert extras.get("wd_tripped") is True

    def test_resets_after_ptt_off(self):
        wd = self._wd(timeout=30)
        rig = _rig()
        extras = {"ptt": True}
        # Trip the watchdog
        with patch("rigtop.app.time.monotonic", side_effect=[0.0, 0.0, 60.0, 60.0]):
            wd.update(True, rig, extras, None)
            wd.update(True, rig, extras, None)
        assert wd.tripped
        # Now PTT goes off — should reset
        extras2 = {"ptt": False}
        with patch("rigtop.app.time.monotonic", return_value=61.0):
            wd.update(False, rig, extras2, None)
        assert not wd.tripped

    def test_no_config_never_trips(self):
        wd = TxWatchdog(None)
        rig = _rig()
        extras = {"ptt": True}
        with patch("rigtop.app.time.monotonic", return_value=9999.0):
            wd.update(True, rig, extras, None)
        assert not wd.tripped
        rig.set_ptt.assert_not_called()

    def test_tui_sink_alert_called_on_trip(self):
        wd = self._wd(timeout=10)
        rig = _rig()
        tui = MagicMock()
        extras = {"ptt": True}
        with patch("rigtop.app.time.monotonic", side_effect=[0.0, 0.0, 20.0, 20.0]):
            wd.update(True, rig, extras, tui)
            wd.update(True, rig, extras, tui)
        tui.show_watchdog_alert.assert_called_once()

    def test_second_trip_not_repeated(self):
        wd = self._wd(timeout=10)
        rig = _rig()
        extras = {"ptt": True}
        side_effects = [0.0, 0.0, 20.0, 20.0, 30.0, 30.0]
        with patch("rigtop.app.time.monotonic", side_effect=side_effects):
            wd.update(True, rig, extras, None)
            wd.update(True, rig, extras, None)  # trips here
            wd.update(True, rig, extras, None)  # already tripped, no second call
        assert rig.set_ptt.call_count == 1


# ---------------------------------------------------------------------------
# _print_cycle
# ---------------------------------------------------------------------------


class TestPrintCycle:
    _pos = Position(lat=59.33, lon=18.07)

    def test_prints_zones_when_location_present(self, capsys):
        from rigtop.app import _print_cycle
        location = {"cq": "18", "iaru": "18", "cc": "SE", "country": "Sweden"}
        _print_cycle("12:00:00", self._pos, "JP90qd", {"gps_src": "rig", "location": location})
        out = capsys.readouterr().out
        assert "CQ 18" in out
        assert "ITU 18" in out

    def test_no_zones_printed_when_location_missing(self, capsys):
        from rigtop.app import _print_cycle
        _print_cycle("12:00:00", self._pos, "JP90qd", {"gps_src": "static"})
        out = capsys.readouterr().out
        assert "CQ" not in out
        assert "ITU" not in out

    def test_no_output_when_no_fix(self, capsys):
        from rigtop.app import _print_cycle
        _print_cycle("12:00:00", None, "", {})
        out = capsys.readouterr().out
        assert "No GPS fix" in out
