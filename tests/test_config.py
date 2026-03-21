"""Unit tests for rigtop.config — TOML loading and validation."""

import textwrap
from pathlib import Path

import pytest

from rigtop.config import (
    AprsConfig,
    BbsConfig,
    Config,
    GpsConfig,
    GpsStaticConfig,
    LogLevel,
    RigConfig,
    RigctldConfig,
    SinkConfig,
    WatchdogConfig,
    load_config,
)


class TestDefaults:
    def test_config_defaults(self):
        cfg = Config()
        assert cfg.interval == 0.5
        assert cfg.once is False
        assert cfg.meters is True
        assert cfg.log_level == LogLevel.WARNING

    def test_rig_defaults(self):
        rig = RigConfig()
        assert rig.host == "127.0.0.1"
        assert rig.port == 4532
        assert rig.name == "default"

    def test_sink_default_type(self):
        cfg = Config()
        assert len(cfg.sinks) == 1
        assert cfg.sinks[0].type == "tui"

    def test_aprsis_default_port(self):
        sink = SinkConfig(type="aprsis")
        assert sink.port == 14580

    def test_gpsd_default_port(self):
        sink = SinkConfig(type="gpsd")
        assert sink.port == 2947

    def test_wsjtx_default_port(self):
        sink = SinkConfig(type="wsjtx")
        assert sink.port == 2237

    def test_nmea_default_port(self):
        sink = SinkConfig(type="nmea")
        assert sink.port == 10110

    def test_tui_sink_port_zero(self):
        sink = SinkConfig(type="tui")
        assert sink.port == 0


class TestLoadConfig:
    def test_load_none_with_no_toml_returns_defaults(self, tmp_path, monkeypatch):
        # load_config(None) auto-discovers rigtop.toml; returns defaults if absent
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert isinstance(cfg, Config)
        assert cfg.rig.host == "127.0.0.1"

    def test_load_empty_toml(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text("", encoding="utf-8")
        cfg = load_config(f)
        assert cfg.interval == 2.0  # file default, not dataclass default

    def test_general_section(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [general]
            interval = 1.0
            meters = false
            log_level = "DEBUG"
        """), encoding="utf-8")
        cfg = load_config(f)
        assert cfg.interval == 1.0
        assert cfg.meters is False
        assert cfg.log_level == LogLevel.DEBUG

    def test_single_rig_section(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [rig]
            name = "IC-705"
            host = "192.168.1.10"
            port = 4532
        """), encoding="utf-8")
        cfg = load_config(f)
        assert cfg.rig.name == "IC-705"
        assert cfg.rig.host == "192.168.1.10"

    def test_multiple_rigs_array(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [[rig]]
            name = "rig1"
            host = "127.0.0.1"
            [[rig]]
            name = "rig2"
            host = "192.168.1.2"
        """), encoding="utf-8")
        cfg = load_config(f)
        assert len(cfg.rigs) == 2
        assert cfg.rig.name == "rig1"  # first is selected

    def test_select_rig_by_name(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [[rig]]
            name = "alpha"
            host = "10.0.0.1"
            [[rig]]
            name = "beta"
            host = "10.0.0.2"
        """), encoding="utf-8")
        cfg = load_config(f)
        cfg.select_rig("beta")
        assert cfg.rig.name == "beta"
        assert cfg.rig.host == "10.0.0.2"

    def test_select_rig_unknown_raises(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text("[rig]\nname = \"main\"\n", encoding="utf-8")
        cfg = load_config(f)
        with pytest.raises(ValueError, match="Unknown rig"):
            cfg.select_rig("ghost")

    def test_aprs_section(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [aprs]
            enabled = true
            freq = 144.800
            qsy_mode = "FM"
        """), encoding="utf-8")
        cfg = load_config(f)
        assert cfg.aprs is not None
        assert cfg.aprs.enabled is True
        assert cfg.aprs.freq == pytest.approx(144.800)
        assert cfg.aprs.qsy_mode == "FM"

    def test_gps_fallback_section(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [gps_fallback]
            host = "192.168.1.50"
            port = 11123
        """), encoding="utf-8")
        cfg = load_config(f)
        assert cfg.gps_fallback is not None
        assert cfg.gps_fallback.host == "192.168.1.50"

    def test_gps_static_section(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [gps_static]
            lat = 59.33
            lon = 18.07
            alt = 20.0
        """), encoding="utf-8")
        cfg = load_config(f)
        assert cfg.gps_static is not None
        assert cfg.gps_static.lat == pytest.approx(59.33)

    def test_sink_disabled_kept(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [[sink]]
            type = "tui"
            [[sink]]
            type = "wsjtx"
            enabled = false
        """), encoding="utf-8")
        cfg = load_config(f)
        assert len(cfg.sinks) == 2
        assert cfg.sinks[1].type == "wsjtx"
        assert cfg.sinks[1].enabled is False

    def test_watchdog_section(self, tmp_path):
        f = tmp_path / "rigtop.toml"
        f.write_text(textwrap.dedent("""\
            [watchdog]
            tx_timeout = 60
        """), encoding="utf-8")
        cfg = load_config(f)
        assert cfg.watchdog is not None
        assert cfg.watchdog.tx_timeout == 60

    def test_auto_discover_finds_toml_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "rigtop.toml"
        f.write_text("[general]\ninterval = 3.0\n", encoding="utf-8")
        cfg = load_config(None)
        assert cfg.interval == 3.0


class TestValidation:
    def test_invalid_log_level(self):
        with pytest.raises(Exception):
            Config(log_level="VERBOSE")

    def test_gps_static_lat_out_of_range(self):
        with pytest.raises(Exception):
            GpsStaticConfig(lat=91.0, lon=0.0)

    def test_gps_static_lon_out_of_range(self):
        with pytest.raises(Exception):
            GpsStaticConfig(lat=0.0, lon=181.0)

    def test_watchdog_min_timeout(self):
        with pytest.raises(Exception):
            WatchdogConfig(tx_timeout=5)  # min is 10

    def test_rigctld_invalid_baud(self):
        with pytest.raises(Exception):
            RigctldConfig(baud_rate=99999)
