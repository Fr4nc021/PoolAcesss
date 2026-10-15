# -*- mode: python ; coding: utf-8 -*-
# Um .exe só, sem Flask: o programa copia este arquivo para _update/runner
# antes de rodar, para poder trocar a cópia que fica na pasta do programa.

a = Analysis(
    ['run_updater.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['app.updater_main'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['flask', 'httpx', 'PIL', 'pystray', 'cryptography', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AcessoPiscinas-Atualizador',
    icon='assets/aquaaccess.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
