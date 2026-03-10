# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Tinta4PlusU Helper Daemon
Produces: dist/tinta4plusu-helper/tinta4plusu-helper
"""

a = Analysis(
    ['HelperDaemon.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'ECController',
        'EInkUSBController',
        'WatchdogTimer',
    ],
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
    name='tinta4plusu-helper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='tinta4plusu-helper',
)
