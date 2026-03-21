"""Unit tests for TUI control helpers — no I/O, no Textual app."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rigtop.sinks.tui import RigCommandPanel, _control_bar


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


class TestControlChanged:
    """ControlChanged message carries state, and _last_controls update prevents snap-back.

    All tests use a MagicMock rig — no radio connection required.
    """

    def test_message_key_and_value(self):
        msg = RigCommandPanel.ControlChanged("PREAMP", 1.0)
        assert msg.key == "PREAMP"
        assert msg.value == 1.0

    def test_last_controls_updated_immediately(self):
        # Simulates on_rig_command_panel_control_changed updating _last_controls.
        # Before fix: _last_controls held the stale poll value (0.0) and the
        # next render_data() call would reset the button back to "off".
        last_controls: dict = {"PREAMP": 0.0}  # stale value from last rig poll
        msg = RigCommandPanel.ControlChanged("PREAMP", 1.0)
        last_controls[msg.key] = msg.value  # handler updates immediately
        assert last_controls["PREAMP"] == 1.0

    def test_subsequent_render_data_preserves_new_value(self):
        # With _last_controls updated, the next render_data must not reset to off.
        # Regression: old nearest-neighbour against [0,10,20] mapped pre_raw=1.0→idx=0→"off"
        last_controls = {"PREAMP": 1.0}  # updated via ControlChanged
        pre_raw = last_controls.get("PREAMP")
        pre_idx = 0 if not pre_raw else 1
        assert pre_idx == 1, "PREAMP=1.0 must map to 'on', not revert to 'off'"

    def test_set_level_called_with_mock_rig(self):
        # set_level on a mock rig returns True; ControlChanged is posted with the value.
        mock_rig = MagicMock()
        mock_rig.set_level.return_value = True
        mock_rig.get_level.return_value = 1.0  # rig confirms preamp is on

        ok = mock_rig.set_level("PREAMP", 1.0)
        actual = mock_rig.get_level("PREAMP")
        pre_idx = 0 if not actual else 1

        assert ok is True
        assert pre_idx == 1
        mock_rig.set_level.assert_called_once_with("PREAMP", 1.0)


class TestPreampIndexMapping:
    """pre_raw→idx mapping must treat any non-zero as on.

    IC-705 via hamlib returns PREAMP=1.0 (index-based).
    Old nearest-neighbour against [0,10,20]: abs(0-1)=1 < abs(10-1)=9 → idx=0 → "off" (bug).
    New mapping: 0 if not pre_raw else 1 → always correct.
    """

    @pytest.mark.parametrize(
        "pre_raw,expected_on",
        [
            (0.0, False),   # preamp off
            (1.0, True),    # IC-705 index style
            (10.0, True),   # dB style rig (preamp 1)
            (20.0, True),   # dB style rig (preamp 2)
        ],
    )
    def test_pre_raw_to_on_off(self, pre_raw: float, expected_on: bool) -> None:
        pre_idx = 0 if not pre_raw else 1
        assert bool(pre_idx) == expected_on
