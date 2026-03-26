"""Unit tests for propagation panel helpers — no I/O, no Textual app."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rigtop.sinks.tui import PropagationPanel, _fetch_propagation

_SAMPLE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<solar>
  <solardata>
    <solarflux>150</solarflux>
    <aindex>5</aindex>
    <kindex>2</kindex>
    <xray>B5.2</xray>
    <sunspots>120</sunspots>
    <updated>2024 Jan 01 1200 UTC</updated>
    <calculatedconditions>
      <band name="80m-40m" time="day">Good</band>
      <band name="80m-40m" time="night">Fair</band>
      <band name="30m-20m" time="day">Good</band>
      <band name="30m-20m" time="night">Good</band>
      <band name="17m-15m" time="day">Fair</band>
      <band name="17m-15m" time="night">Poor</band>
      <band name="12m-10m" time="day">Poor</band>
      <band name="12m-10m" time="night">Poor</band>
    </calculatedconditions>
  </solardata>
</solar>
"""


class TestFetchPropagation:
    def _make_response(self, data: bytes):
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        mock.read.return_value = data
        return mock

    def test_returns_solar_indices(self):
        with patch("urllib.request.urlopen", return_value=self._make_response(_SAMPLE_XML)):
            result = _fetch_propagation()
        assert result is not None
        assert result["sfi"] == "150"
        assert result["sn"] == "120"
        assert result["aindex"] == "5"
        assert result["kindex"] == "2"
        assert result["xray"] == "B5.2"
        assert result["updated"] == "2024 Jan 01 1200 UTC"

    def test_returns_band_conditions(self):
        with patch("urllib.request.urlopen", return_value=self._make_response(_SAMPLE_XML)):
            result = _fetch_propagation()
        assert result is not None
        bands = result["bands"]
        assert bands["80m-40m"]["day"] == "Good"
        assert bands["80m-40m"]["night"] == "Fair"
        assert bands["17m-15m"]["day"] == "Fair"
        assert bands["12m-10m"]["night"] == "Poor"

    def test_returns_none_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("unreachable")):
            result = _fetch_propagation()
        assert result is None

    def test_returns_none_on_bad_xml(self):
        with patch("urllib.request.urlopen", return_value=self._make_response(b"not xml")):
            result = _fetch_propagation()
        assert result is None

    def test_returns_none_when_solardata_missing(self):
        xml = b"<solar><other/></solar>"
        with patch("urllib.request.urlopen", return_value=self._make_response(xml)):
            result = _fetch_propagation()
        assert result is None

    def test_empty_bands_when_no_conditions(self):
        xml = b"<solar><solardata><solarflux>100</solarflux></solardata></solar>"
        with patch("urllib.request.urlopen", return_value=self._make_response(xml)):
            result = _fetch_propagation()
        assert result is not None
        assert result["bands"] == {}


class TestPropagationPanelRenderData:
    def _panel(self) -> PropagationPanel:
        p = PropagationPanel()
        p._reactive_renderable__default = None  # avoid Textual init issues
        p.update = MagicMock()
        return p

    def test_render_none_shows_no_data_message(self):
        p = self._panel()
        p.render_data(None)
        p.update.assert_called_once()
        rendered = str(p.update.call_args[0][0])
        assert "No propagation data" in rendered

    def test_render_good_data_shows_sfi(self):
        p = self._panel()
        data = {
            "sfi": "150",
            "sn": "120",
            "aindex": "5",
            "kindex": "2",
            "xray": "B5.2",
            "updated": "2024 Jan 01",
            "bands": {
                "80m-40m": {"day": "Good", "night": "Fair"},
                "30m-20m": {"day": "Good", "night": "Good"},
            },
        }
        p.render_data(data)
        p.update.assert_called_once()
        rendered = str(p.update.call_args[0][0])
        assert "150" in rendered
        assert "120" in rendered
        assert "80m-40m" in rendered
        assert "Good" in rendered
        assert "Fair" in rendered

    def test_render_shows_updated_timestamp(self):
        p = self._panel()
        data = {
            "sfi": "120",
            "sn": "80",
            "aindex": "3",
            "kindex": "1",
            "updated": "2024 Jun 15 0600 UTC",
            "bands": {},
        }
        p.render_data(data)
        rendered = str(p.update.call_args[0][0])
        assert "2024 Jun 15" in rendered

    def test_render_high_a_index_no_crash(self):
        p = self._panel()
        data = {"sfi": "80", "sn": "20", "aindex": "50", "kindex": "7", "bands": {}}
        p.render_data(data)
        p.update.assert_called_once()

    def test_render_missing_bands_no_crash(self):
        p = self._panel()
        data = {"sfi": "100", "sn": "50", "aindex": "8", "kindex": "3", "bands": {}}
        p.render_data(data)
        p.update.assert_called_once()

    def test_render_xray_m_class(self):
        p = self._panel()
        data = {
            "sfi": "200", "sn": "200", "aindex": "30", "kindex": "6",
            "xray": "M1.5", "bands": {},
        }
        p.render_data(data)
        rendered = str(p.update.call_args[0][0])
        assert "M1.5" in rendered
