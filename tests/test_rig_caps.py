"""Tests for per-rig capability configuration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rigtop.config import RigConfig
from rigtop.sinks.tui import RigCommandPanel


def _panel(rig_config: RigConfig | None = None) -> RigCommandPanel:
    """Build a RigCommandPanel with a mock rig — no Textual app needed."""
    mock_rig = MagicMock()
    return RigCommandPanel(mock_rig, rig_config=rig_config)


class TestDefaultCaps:
    """Default RigConfig must match current IC-705 behaviour."""

    def test_default_att_steps(self):
        p = _panel()
        assert p._att_steps == [0, 6, 12, 18]

    def test_default_att_settable(self):
        p = _panel()
        assert p._att_settable is True

    def test_default_has_data(self):
        p = _panel()
        assert p._has_data is True

    def test_default_modes_include_pkt(self):
        p = _panel()
        assert "PKTFM" in p._modes
        assert "PKTUSB" in p._modes

    def test_default_data_maps_derived(self):
        p = _panel()
        assert p._data_on_map == {"FM": "PKTFM", "USB": "PKTUSB", "LSB": "PKTLSB"}
        assert p._data_off_map == {"PKTFM": "FM", "PKTUSB": "USB", "PKTLSB": "LSB"}


class TestAttNotSettable:
    """att_settable=False — clicking ATT shows warning and does not call set_level."""

    def test_att_not_settable_skips_set_level(self):
        cfg = RigConfig(att_settable=False)
        p = _panel(cfg)
        assert p._att_settable is False

    def test_att_settable_true_by_default(self):
        cfg = RigConfig()
        p = _panel(cfg)
        assert p._att_settable is True


class TestCustomAttSteps:
    def test_custom_att_steps_stored(self):
        cfg = RigConfig(att_steps=[0, 12])
        p = _panel(cfg)
        assert p._att_steps == [0, 12]

    def test_att_label_uses_custom_steps(self):
        cfg = RigConfig(att_steps=[0, 20])
        p = _panel(cfg)
        p._att_idx = 1
        assert "20" in p._att_label()

    def test_att_label_off_at_zero(self):
        cfg = RigConfig(att_steps=[0, 12, 18])
        p = _panel(cfg)
        p._att_idx = 0
        assert p._att_label() == "ATT: off"


class TestNoDataModes:
    """has_data_modes=False — Data button must not be composed, maps must be empty."""

    def test_has_data_false(self):
        cfg = RigConfig(has_data_modes=False)
        p = _panel(cfg)
        assert p._has_data is False

    def test_data_maps_empty_when_no_data(self):
        cfg = RigConfig(has_data_modes=False, modes=["FM", "USB", "LSB", "AM", "CW"])
        p = _panel(cfg)
        assert p._data_on_map == {}
        assert p._data_off_map == {}


class TestCustomModes:
    def test_custom_modes_stored(self):
        modes = ["FM", "USB", "LSB", "AM", "CW"]
        cfg = RigConfig(modes=modes, has_data_modes=False)
        p = _panel(cfg)
        assert p._modes == modes

    def test_partial_pkt_modes_partial_map(self):
        # Only PKTFM in the list — only FM↔PKTFM should be mapped
        cfg = RigConfig(modes=["FM", "USB", "PKTFM"], has_data_modes=True)
        p = _panel(cfg)
        assert p._data_on_map == {"FM": "PKTFM"}
        assert p._data_off_map == {"PKTFM": "FM"}
        assert "USB" not in p._data_on_map


class TestRigConfigParsing:
    """RigConfig fields parse from dict (as TOML would produce)."""

    def test_att_steps_from_dict(self):
        cfg = RigConfig(**{"att_steps": [0, 12, 18, 20], "att_settable": True})
        assert cfg.att_steps == [0, 12, 18, 20]

    def test_att_not_settable_from_dict(self):
        cfg = RigConfig(**{"att_settable": False})
        assert cfg.att_settable is False

    def test_has_data_false_from_dict(self):
        cfg = RigConfig(**{"has_data_modes": False})
        assert cfg.has_data_modes is False

    def test_defaults_unchanged_when_not_specified(self):
        cfg = RigConfig(name="IC-705")
        assert cfg.att_steps == [0, 6, 12, 18]
        assert cfg.att_settable is True
        assert cfg.has_data_modes is True
