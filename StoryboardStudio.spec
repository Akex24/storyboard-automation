# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — сборка для macOS (.app) и Windows (.exe)
# macOS:   pyinstaller StoryboardStudio.spec
# Windows: pyinstaller StoryboardStudio.spec  (запускать на Windows-машине)

import sys
import certifi

a = Analysis(
    ['storyboard_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        (certifi.where(), 'certifi'),
        # Иконки табов (Lucide SVG) и иконки приложения
        ('assets/icons', 'assets/icons'),
        # 2026-05-09: pipeline.py забандливается чтобы Studio при старте
        # синхронизировала его в project_root. AutonomousGenThread зовёт
        # `claude -p` с cwd=project_root, агент через Bash tool делает
        # `python3 pipeline.py generate ...` — файл должен быть в cwd.
        # На Mac разместится в Contents/Resources/, на Win onedir — в
        # _internal/. sys._MEIPASS указывает на корень в обоих случаях.
        ('pipeline.py', '.'),
    ],
    hiddenimports=[
        'PIL._tkinter_finder',
        'PyQt6.QtPrintSupport',
        'PyQt6.QtSvg',          # Для рендера SVG-иконок табов
        'PyQt6.QtSvgWidgets',
        'docx',
        # stdlib модули которые PyInstaller на Win иногда не подхватывает.
        # 2026-05-08: на Win-ноуте у коллеги периодически появлялась ошибка
        # `No module named 'unicodedata'` при первом запуске (через requests
        # → idna → unicodedata). Явно перечисляем чтобы PyInstaller точно
        # положил их в bundle.
        'unicodedata',
        'encodings',
        'encodings.idna',
        'encodings.utf_8',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy'],
    noarchive=False,
)

pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='Storyboard Studio',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False,
        upx=True,
        name='Storyboard Studio',
    )
    app = BUNDLE(
        coll,
        name='Storyboard Studio.app',
        icon='assets/icon.icns',
        bundle_identifier='com.storyboardstudio.app',
        info_plist={
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
            'CFBundleShortVersionString': '1.0',
        },
    )
else:
    # Windows — onedir mode (с 2026-05-08).
    # Раньше был onefile=True, но onefile + Windows Defender = постоянные
    # ошибки `_MEI…\base_l…` (PyInstaller распаковывает bundle в
    # %TEMP%\_MEI<rand>, Defender карантинит часть файлов → крэш).
    # В onedir всё уже распаковано рядом с .exe в папке `_internal/`,
    # запуск без распаковки в TEMP → Defender не лазит → стабильно.
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='Storyboard Studio',
        icon='assets/icon.ico',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False,
        upx=True,
        name='Storyboard Studio',
    )
