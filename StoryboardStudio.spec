# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — сборка для macOS (.app) и Windows (.exe)
# macOS:   pyinstaller StoryboardStudio.spec
# Windows: pyinstaller StoryboardStudio.spec  (запускать на Windows-машине)

import sys
import certifi
from PyInstaller.utils.hooks import collect_all

# 2026-06-24: pillow-heif тащит нативные libheif/libde265/x265. collect_all
# собирает их .dylib/.dll + data + hidden submodules для onedir Win и .app Mac.
# ВАЖНО: pillow_heif ДОЛЖЕН быть установлен в окружении сборки — иначе collect_all
# падает (это намеренно: не шипим билд без heic-конвертера).
_heif_datas, _heif_binaries, _heif_hiddenimports = collect_all('pillow_heif')

a = Analysis(
    ['storyboard_app.py'],
    pathex=[],
    binaries=_heif_binaries,
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
        # 2026-06-09: key_pool.py — диспетчер пула ключей (round-robin).
        # Синкается в project_root рядом с pipeline.py через
        # sync_pipeline_py_to_project, чтобы CLI (pipeline.py) мог
        # `from key_pool import next_key`. Без бандла CLI упал бы на
        # fallback (одиночный .env-ключ) — не критично, но без ротации.
        ('key_pool.py', '.'),
        # 2026-06-09: generate_storyboards.py — боевой image-генератор
        # FastGen (Nano Banana 2), такой же потребитель ключа. Синкается
        # в project_root рядом с key_pool.py, чтобы его
        # `from key_pool import next_key` сработал в frozen .app (ротация).
        ('generate_storyboards.py', '.'),
        # 2026-05-13 (v1.0.66): ГЛАВНАЯ_ИНСТРУКЦИЯ.md — источник правды
        # для Scriptwriter/Validator/Editor/ContextReviewer. Загружается
        # в runtime через agents/instruction_loader.py с селективным
        # извлечением разделов по агенту (SW=[1,3,4,6,8] и т.д.).
        # Хорошо отделить от pipeline.py — отдельный концепт «бандлим
        # промпт-инструкции» (на будущее сюда добавим nano_banana /
        # seedance инструкции). На Mac: Contents/Resources/instructions/;
        # на Win onedir: _internal/instructions/.
        ('instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md', 'instructions'),
        ('instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ_b.md', 'instructions'),
        ('instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ_c.md', 'instructions'),
        ('instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ_d.md', 'instructions'),
        # 2026-06-02: YuNet face-detection модель для попапа наложения
        # PNG-сеток на лица сториборда (cv2.FaceDetectorYN). На Mac →
        # Contents/Resources/assets/models/, на Win onedir → _internal/assets/models/.
        # Путь резолвится через storyboard_app.get_model_path (_MEIPASS-aware).
        ('assets/models/face_detection_yunet_2023mar.onnx', 'assets/models'),
    ] + _heif_datas,
    hiddenimports=[
        'pillow_heif',
        'PIL._tkinter_finder',
        # 2026-06-02: opencv (cv2) для детекции лиц YuNet. PyInstaller обычно
        # подхватывает cv2 сам, но явный хинт страхует Win-сборку. numpy —
        # транзитивная зависимость cv2 (убран из excludes ниже).
        'cv2',
        'PyQt6.QtPrintSupport',
        'PyQt6.QtSvg',          # Для рендера SVG-иконок табов
        'PyQt6.QtSvgWidgets',
        # 2026-06-22: QtMultimedia + QtMultimediaWidgets — hover-автоплей видео
        # в плитках генератора (QMediaPlayer/QAudioOutput/QVideoWidget). PyInstaller
        # hook-PyQt6 ДОЛЖЕН дотянуть media-плагины (plugins/multimedia/*) и Qt
        # ffmpeg backend при наличии этих hiddenimports. ПРОВЕРИТЬ пересборкой:
        # find бандла на *ultimedia*/*ffmpeg* должен стать НЕ пустым. Если хук не
        # дотянет — добавить collect_dynamic_libs('PyQt6', ...) вторым заходом.
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
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
        # 2026-05-21: модули правил монтажной карты загружаются динамически
        # через `agents/mode_loader.py` (`importlib.import_module`).
        # PyInstaller static analyzer не видит importlib — без явных хинтов
        # эти модули НЕ попадают в bundle (даже режим A) и .app падает
        # на старте с ModuleNotFoundError. Хинты должны перечислять все
        # режимы которые могут быть выбраны в Settings.
        'agents.montage_rules',
        'agents.validator_prefilter',
        'agents.montage_rules_b',
        'agents.montage_rules_c',
        'agents.montage_rules_d',
        'agents.validator_prefilter_b',
        'agents.validator_prefilter_c',
        'agents.validator_prefilter_d',
    ] + _heif_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 2026-06-02: numpy УБРАН из excludes — он нужен cv2 (YuNet face-detection).
    excludes=['tkinter', 'matplotlib', 'scipy'],
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
