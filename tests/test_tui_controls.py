"""Unit tests for TUI control helpers — no I/O, no Textual app."""

from __future__ import annotations

from rigtop.sinks.tui import _control_bar


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
