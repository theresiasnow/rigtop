"""Unit tests for propagation panel helpers — no I/O, no Textual app."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

from rigtop.sinks.tui import PropagationPanel, _fetch_propagation


def _render(obj) -> str:
    """Render a Rich renderable (Text or Table) to a plain string."""
    sio = StringIO()
    console = Console(file=sio, highlight=False, markup=False, no_color=True, width=200)
    console.print(obj)
    return sio.getvalue()

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
    <calculatedvhf>
      <phenomenon name="aurora" location="north">Minor</phenomenon>
      <phenomenon name="aurora" location="south">No Aurora</phenomenon>
      <phenomenon name="vhf-aurora">No</phenomenon>
      <band name="50mhz" time="day">Good</band>
      <band name="50mhz" time="night">Fair</band>
      <band name="144mhz" time="day">Fair</band>
      <band name="144mhz" time="night">Poor</band>
      <band name="222mhz" time="day">Poor</band>
      <band name="222mhz" time="night">Poor</band>
      <band name="432mhz" time="day">Poor</band>
      <band name="432mhz" time="night">Poor</band>
    </calculatedvhf>
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

    def test_returns_vhf_bands(self):
        with patch("urllib.request.urlopen", return_value=self._make_response(_SAMPLE_XML)):
            result = _fetch_propagation()
        assert result is not None
        vhf = result.get("vhf", {})
        assert vhf["bands"]["50mhz"]["day"] == "Good"
        assert vhf["bands"]["144mhz"]["night"] == "Poor"

    def test_returns_vhf_aurora(self):
        with patch("urllib.request.urlopen", return_value=self._make_response(_SAMPLE_XML)):
            result = _fetch_propagation()
        assert result is not None
        aurora = result.get("vhf", {}).get("aurora", {})
        assert aurora["north"] == "Minor"
        assert aurora["south"] == "No Aurora"
        assert aurora["vhf"] == "No"


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
        rendered = _render(p.update.call_args[0][0])
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
        rendered = _render(p.update.call_args[0][0])
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
        rendered = _render(p.update.call_args[0][0])
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
        rendered = _render(p.update.call_args[0][0])
        assert "M1.5" in rendered

    def test_render_vhf_bands(self):
        p = self._panel()
        data = {
            "sfi": "120", "sn": "80", "aindex": "3", "kindex": "1", "bands": {},
            "vhf": {
                "aurora": {"north": "Minor", "south": "No Aurora", "vhf": "No"},
                "bands": {
                    "50mhz": {"day": "Good", "night": "Fair"},
                    "144mhz": {"day": "Fair", "night": "Poor"},
                },
            },
        }
        p.render_data(data)
        rendered = _render(p.update.call_args[0][0])
        assert "50 MHz" in rendered
        assert "144MHz" in rendered
        assert "Good" in rendered

    def test_render_aurora(self):
        p = self._panel()
        data = {
            "sfi": "120", "sn": "80", "aindex": "3", "kindex": "1", "bands": {},
            "vhf": {
                "aurora": {"north": "Active", "south": "Minor", "vhf": "Yes"},
                "bands": {},
            },
        }
        p.render_data(data)
        rendered = _render(p.update.call_args[0][0])
        assert "Active" in rendered
        assert "Minor" in rendered

    def test_render_no_vhf_key_no_crash(self):
        p = self._panel()
        data = {"sfi": "100", "sn": "50", "aindex": "8", "kindex": "3", "bands": {}}
        p.render_data(data)
        p.update.assert_called_once()

