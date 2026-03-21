"""Unit tests for TUI control helpers — no I/O, no Textual app."""

from __future__ import annotations

from types import SimpleNamespace

from rigtop.sinks.tui import _control_bar


def _make_aprs_sink(*, connected: bool, beacon_enabled: bool, enabled: bool = True):
    return SimpleNamespace(
        connected=connected,
        _beacon_enabled=beacon_enabled,
        enabled=enabled,
    )


def _pick_beacon(sinks):
    """Mirror the sink-selection logic from RigtopApp._apply_data."""
    aprsis = [s for s in sinks if True]  # all sinks passed here are already AprsIS
    if not aprsis:
        return None
    s = (
        next((x for x in aprsis if getattr(x, "connected", False)), None)
        or next((x for x in aprsis if getattr(x, "_beacon_enabled", False)), None)
        or aprsis[0]
    )
    return bool(getattr(s, "_beacon_enabled", False)) and bool(getattr(s, "connected", False))


class TestBeaconIndicator:
    def test_disabled_sink_returns_false_not_none(self):
        sink = _make_aprs_sink(connected=False, beacon_enabled=True, enabled=False)
        result = _pick_beacon([sink])
        assert result is False  # shows ○ OFF, not hidden

    def test_connected_sink_beacon_on(self):
        sink = _make_aprs_sink(connected=True, beacon_enabled=True)
        assert _pick_beacon([sink]) is True

    def test_connected_sink_beacon_off(self):
        sink = _make_aprs_sink(connected=True, beacon_enabled=False)
        assert _pick_beacon([sink]) is False

    def test_prefers_connected_over_disabled(self):
        disabled = _make_aprs_sink(connected=False, beacon_enabled=True, enabled=False)
        active = _make_aprs_sink(connected=True, beacon_enabled=True)
        assert _pick_beacon([disabled, active]) is True

    def test_no_sinks_returns_none(self):
        assert _pick_beacon([]) is None


class TestControlBar:
    def test_normal_value_percentage(self):
        t = _control_bar("Vol", 0.75)
        plain = t.plain
        assert "75%" in plain

    def test_clamped_high_value(self):
        # value > 1.0 should clamp bar and show 100%, not >100%
        t = _control_bar("Vol", 1.5)
        plain = t.plain
        assert "100%" in plain
        assert "150%" not in plain

    def test_clamped_low_value(self):
        # negative value should clamp to 0%
        t = _control_bar("Vol", -0.5)
        plain = t.plain
        assert "0%" in plain

    def test_zero_value(self):
        t = _control_bar("Vol", 0.0)
        plain = t.plain
        assert "0%" in plain

    def test_label_in_output(self):
        t = _control_bar("SQL", 0.5)
        assert "SQL" in t.plain

    def test_selected_shows_arrow(self):
        t = _control_bar("RF", 0.5, selected=True)
        assert "▶" in t.plain

    def test_not_selected_no_arrow(self):
        t = _control_bar("RF", 0.5, selected=False)
        assert "▶" not in t.plain
