# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for rigtop — Windows standalone bundle."""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("reverse_geocoder")
datas += collect_data_files("textual")

a = Analysis(
    ["rigtop/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["winpty"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rigtop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,   # TUI + CLI both need a console window
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="rigtop",
)
