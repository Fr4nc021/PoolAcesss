# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    ('assets/aquaaccess.ico', '.'),
]
binaries = []
hiddenimports = ['app', 'app.main', 'app.routes', 'app.db', 'app.config', 'app.expiry', 'app.intelbras_client', 'app.shortcut', 'app.tray', 'app.sync', 'app.updater', 'app.update_core', 'app.update_key', 'app.version', 'app.netcheck', 'pystray', 'pystray._win32']

tmp_ret = collect_all('flask')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    ['run_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AcessoPiscinas',
    icon='assets/aquaaccess.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AcessoPiscinas',
)
