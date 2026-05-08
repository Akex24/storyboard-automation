# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec для установщика — собирает .app (Mac) и .exe (Windows)

import sys

a = Analysis(
    ['installer_app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PyQt6.QtPrintSupport'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'PIL'],
    noarchive=False,
)

pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='Storyboard Studio Installer',
        debug=False,
        strip=False,
        upx=True,
        console=False,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False, upx=True,
        name='Storyboard Studio Installer',
    )
    app = BUNDLE(
        coll,
        name='Storyboard Studio Installer.app',
        icon='assets/icon_installer.icns',
        bundle_identifier='com.storyboardstudio.installer',
        info_plist={
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
            'CFBundleShortVersionString': '1.0',
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas,
        name='Storyboard Studio Installer',
        icon='assets/icon_installer.ico',
        debug=False, strip=False, upx=True, console=False,
        onefile=True,
    )
