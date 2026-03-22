"""Unit tests for DirewolfLauncher.generate_active_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from rigtop.direwolf_launcher import DirewolfLauncher


@pytest.fixture()
def tmp_setup(tmp_path: Path):
    """Return (install_path, config_dir, source_conf) with a minimal APRS config."""
    install_path = tmp_path / "direwolf"
    install_path.mkdir()
    config_dir = tmp_path / "rigtop"
    config_dir.mkdir()

    source_conf = install_path / "direwolf-aprs.conf"
    source_conf.write_text(
        "ADEVICE  null null\n"
        "CHANNEL 0\n"
        "MYCALL N0CALL-9\n"
        "TBEACON delay=0:30 every=10:00 via=WIDE1-1,WIDE2-1 symbol=/[ lat=60.00N long=025.00E\n"
        "CBEACON delay=0:01 every=10:00 info=\"rigtop\"\n",
        encoding="utf-8",
    )
    return install_path, config_dir, source_conf


def _make_launcher(install_path: Path, config_dir: Path, source_conf: Path) -> DirewolfLauncher:
    return DirewolfLauncher(
        install_path=str(install_path),
        config_dir=config_dir,
        source_configs={"aprs": source_conf},
    )


class TestGenerateActiveConfig:
    def test_beacon_enabled_tbeacon_present(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        out = lnchr.generate_active_config("aprs", beacon_enabled=True)
        text = out.read_text(encoding="utf-8")
        assert "TBEACON" in text
        assert "# [rigtop beacon off]" not in text

    def test_beacon_disabled_tbeacon_commented(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        out = lnchr.generate_active_config("aprs", beacon_enabled=False)
        text = out.read_text(encoding="utf-8")
        assert "# [rigtop beacon off] TBEACON" in text
        # Original TBEACON directive must not appear uncommented
        for line in text.splitlines():
            stripped = line.lstrip()
            assert not (stripped.upper().startswith("TBEACON") and not stripped.startswith("#"))

    def test_re_enable_removes_comment(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        lnchr.generate_active_config("aprs", beacon_enabled=False)
        out = lnchr.generate_active_config("aprs", beacon_enabled=True)
        text = out.read_text(encoding="utf-8")
        assert "# [rigtop beacon off]" not in text
        assert "TBEACON" in text

    def test_source_file_unchanged(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        original = source_conf.read_text(encoding="utf-8")
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        lnchr.generate_active_config("aprs", beacon_enabled=False)
        assert source_conf.read_text(encoding="utf-8") == original

    def test_active_config_written_to_config_dir(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        out = lnchr.generate_active_config("aprs", beacon_enabled=True)
        assert out.parent == config_dir
        assert out.name == "direwolf-aprs-active.conf"

    def test_other_lines_preserved(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        out = lnchr.generate_active_config("aprs", beacon_enabled=False)
        text = out.read_text(encoding="utf-8")
        assert "MYCALL N0CALL-9" in text
        assert "CBEACON" in text

    def test_fallback_to_install_path_when_no_source_configs(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        # No source_configs — launcher falls back to install_path/direwolf-aprs.conf
        lnchr = DirewolfLauncher(
            install_path=str(install_path),
            config_dir=config_dir,
        )
        out = lnchr.generate_active_config("aprs", beacon_enabled=True)
        assert out.is_file()

    def test_missing_source_raises(self, tmp_path):
        install_path = tmp_path / "dw"
        install_path.mkdir()
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        lnchr = DirewolfLauncher(
            install_path=str(install_path),
            config_dir=config_dir,
        )
        with pytest.raises(FileNotFoundError):
            lnchr.generate_active_config("aprs")

    def test_active_profile_set_after_generate(self, tmp_setup):
        install_path, config_dir, source_conf = tmp_setup
        lnchr = _make_launcher(install_path, config_dir, source_conf)
        assert lnchr._active_profile is None
        lnchr.generate_active_config("aprs", beacon_enabled=True)
        assert lnchr._active_profile == "aprs"
