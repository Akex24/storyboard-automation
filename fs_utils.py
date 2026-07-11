"""fs_utils.py — нейтральные file-system хелперы (stdlib-only, без импортов
проекта → без circular import). Кросс-платформенно (macOS / Windows 10-11).

move_to_trash(path) — удаление в СИСТЕМНУЮ Корзину (recoverable), не unlink.
Вынесено СЮДА, а не в storyboard_app.py: тот запускается как __main__, и
`import storyboard_app` из подпакета поднял бы ВТОРОЙ экземпляр модуля.
2026-07-09.
"""
from __future__ import annotations

import os
import sys
import time
import shutil
import traceback
from pathlib import Path


def _macos_trash_dir(p: Path):
    """Каталог системной Корзины для ТОГО ЖЕ тома, где лежит файл — чтобы
    перенос был rename'ом внутри тома, без копирования через границу тома:
      • том файла == том $HOME  → ~/.Trash
      • внешний / другой том     → <mount>/.Trashes/<uid>
    Возвращает СОЗДАННЫЙ и записываемый Path каталога Корзины, либо None,
    если каталог недоступен/не создать (том read-only и т.п.)."""
    try:
        home = Path.home()
        # том файла определяем по устройству (st_dev), сравниваем с домашним
        p_dev = os.stat(p.parent).st_dev
        home_dev = os.stat(home).st_dev
        if p_dev == home_dev:
            trash = home / '.Trash'
        else:
            # точка монтирования тома файла → его .Trashes/<uid>
            mount = p.parent.resolve()
            while not os.path.ismount(str(mount)) and mount != mount.parent:
                mount = mount.parent
            trash = mount / '.Trashes' / str(os.getuid())
        trash.mkdir(parents=True, exist_ok=True)
        # os.access(W_OK) даёт ЛОЖНЫЙ негатив на .Trashes/<uid> (родитель
        # .Trashes без read-бита у owner) — проверяем записываемость РЕАЛЬНОЙ
        # probe-записью: только она отражает фактические права тома.
        probe = trash / f".wtest_{os.getpid()}"
        try:
            probe.touch()
            probe.unlink()
        except Exception:
            return None
        return trash
    except Exception:
        traceback.print_exc()
        return None


def _macos_ns_trash(p: Path) -> bool:
    """macOS: удаление в Корзину через NSFileManager.trashItemAtURL (штатный API,
    вызов `osascript -l JavaScript`/JXA → Foundation). БЕЗ звука Корзины (звук —
    это Finder-эффект), без Full Disk Access, без Automation-попапа «управлять
    Finder». Основной путь. Путь передаём argv, чтобы не экранировать в JS.
    True при rc=0 osascript И исчезновении файла с исходного места. 2026-07-10."""
    import subprocess
    jxa = ('ObjC.import("Foundation");'
           'function run(argv){'
           ' var fm=$.NSFileManager.defaultManager;'
           ' var url=$.NSURL.fileURLWithPath(argv[0]);'
           ' var err=$();'
           ' var ok=fm.trashItemAtURLResultingItemURLError(url,null,err);'
           ' return ok?"OK":"FAIL";}')
    try:
        r = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", jxa, str(p)],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and not p.exists()
    except Exception:
        traceback.print_exc()
        return False


def _macos_finder_trash(p: Path) -> bool:
    """macOS: удаление в Корзину через Finder (osascript). Finder привилегирован →
    кладёт файл в Корзину нужного тома (+ «Положить обратно») БЕЗ Full Disk Access.

    КРИТИЧНО (2026-07-10): прямая запись в `.Trashes/<uid>` внешнего тома у .app
    БЕЗ FDA блокируется macOS TCC (EPERM) — Корзина молча не работала (ошибка
    `gen_del_trash_fail` у Alex). Finder эту привилегию имеет. Требует одноразового
    разрешения «управлять Finder» (Automation) — стандартный macOS-попап.

    True при rc=0 osascript И исчезновении файла с исходного места."""
    import subprocess
    try:
        posix = '"' + str(p).replace('\\', '\\\\').replace('"', '\\"') + '"'
        r = subprocess.run(
            ["osascript", "-e",
             f'tell application "Finder" to delete POSIX file {posix}'],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and not p.exists()
    except Exception:
        traceback.print_exc()
        return False


def _win_recycle(p) -> bool:
    """Windows: файл → Recycle Bin через SHFileOperationW + FOF_ALLOWUNDO.
    Чистый ctypes (stdlib). Раскладка структуры — как в проверенной send2trash.

    КРИТИЧНО: FOF_ALLOWUNDO НЕ гарантирует Корзину — на томах без Recycle Bin
    (сетевые диски, часть флешек/внешних) SHFileOperationW удаляет БЕЗВОЗВРАТНО
    и возвращает 0. Поэтому СНАЧАЛА проверяем, что в корне тома файла есть
    каталог '$Recycle.Bin'; нет — сразу False, файл НЕ трогаем, WinAPI не зовём.

    pFrom — АБСОЛЮТНЫЙ путь, double-null terminated. True при коде 0 и снятом
    флаге отмены операции."""
    abs_p = Path(p).resolve()
    # том без Корзины → безвозвратное удаление, поэтому не рискуем
    try:
        recycle_root = Path(abs_p.anchor) / '$Recycle.Bin'
        if not recycle_root.exists():
            return False
    except Exception:
        traceback.print_exc()
        return False

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    # pFrom — список путей через \0 с ДВОЙНЫМ \0 в конце. Для одного файла:
    # путь + \0 (финальный \0 ctypes добавит сам при упаковке LPCWSTR).
    op.pFrom = str(abs_p) + '\x00'
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted


def move_to_trash(path) -> bool:
    """Отправить файл в СИСТЕМНУЮ Корзину (macOS Trash / Windows Recycle Bin),
    чтобы удаление было ОТКАТЫВАЕМЫМ. БЕЗ фоллбэка на unlink: если Корзина не
    сработала — возвращаем False и файл НЕ трогаем (вызывающий код решит, что
    показать юзеру, и НЕ будет снимать карточку / менять canvas.json).

    Без сторонних зависимостей (send2trash не тянем — лишняя зависимость +
    правки spec/CI, Win-бандл не проверить):
      • macOS:   перенос в .Trashes/<uid> ТОГО ЖЕ тома (или ~/.Trash если файл
                 на домашнем томе) — rename внутри тома, без копий между томами.
      • Windows: SHFileOperationW + FOF_ALLOWUNDO через ctypes, но ТОЛЬКО если
                 у тома есть $Recycle.Bin (иначе безвозвратно → False).
      • Linux:   freedesktop-Корзина не стандартизована → False (не наш таргет).

    Возвращает True ТОЛЬКО если файл реально уехал в Корзину; иначе False.

    Появилось 2026-07-09 после инцидента: прямой unlink в delete_result_cell
    снёс готовый ролик безвозвратно (восстановили только через lldb из живого
    дескриптора).
    """
    try:
        p = Path(str(path)).resolve()
        if not (p.exists() and p.is_file()):
            return False
    except Exception:
        traceback.print_exc()
        return False
    try:
        if sys.platform == 'darwin':
            # NSFileManager.trashItemAtURL — штатный API: БЕЗ звука Корзины, без
            # Full Disk Access, без Automation-попапа. Основной путь (2026-07-10).
            if _macos_ns_trash(p):
                return True
            # Фоллбэк: Finder (тоже без FDA, кладёт в Корзину любого тома, НО играет
            # звук Корзины) — на случай если NSFileManager не сработал.
            if _macos_finder_trash(p):
                return True
            # Фоллбэк: прямой перенос (сработает если у процесса ЕСТЬ FDA — напр.
            # запуск из Terminal с Full Disk Access; для обычного .app не сработает).
            trash = _macos_trash_dir(p)
            if trash is None:
                return False
            dest = trash / p.name
            if dest.exists():
                # коллизия имени в Корзине — суффикс timestamp, чтобы не
                # перетереть уже лежащий там одноимённый файл
                stamp = time.strftime('%Y%m%d_%H%M%S')
                dest = trash / f"{p.stem}_{stamp}{p.suffix}"
            shutil.move(str(p), str(dest))
            return not p.exists()          # подтверждаем, что файл реально ушёл
        if sys.platform == 'win32':
            return _win_recycle(p)
        # Linux и прочее — системная Корзина не поддержана
        return False
    except Exception:
        traceback.print_exc()
        return False


def is_file_busy(path) -> bool:
    """Занят ли файл ДРУГИМ процессом (открыт/держится — напр. DaVinci с клипом на
    таймлайне). macOS/Linux: `lsof -t` (список PID'ов), исключая СВОЙ процесс. Windows:
    попытка открыть на запись `open(r+b)` (PermissionError → занят). Ошибка САМОЙ
    проверки → считаем НЕ занят (False), чтобы сбой проверки не блокировал обработку.

    ВАЖНО (2026-07-10): исключаем `os.getpid()`. is_file_busy бежит ВНУТРИ Storyboard
    Studio, а Studio держит клип открытым на ЧТЕНИЕ (превью плитки / плеер) → без вычета
    своего PID lsof видел СВОЙ же дескриптор и давал ложное «занят другой программой»
    сразу после генерации. НЕ переходим на «только запись»: DaVinci держит исходные
    клипы на ЧТЕНИЕ (fd вида 16r), write-гейт пропустил бы редактор с файлом на таймлайне.

    Win-ветка использует open(), НЕ subprocess → чёрное cmd-окно не всплывает; lsof
    запускается только на darwin/linux, поэтому CREATE_NO_WINDOW тут не требуется."""
    try:
        p = Path(str(path)).resolve()
        if not (p.exists() and p.is_file()):
            return False
        if sys.platform == 'win32':
            try:
                with open(p, 'r+b'):
                    return False           # открылся на запись → свободен
            except PermissionError:
                return True                # держит другой процесс
            except OSError:
                return False               # прочая ошибка открытия → не блокируем
        # darwin / linux — lsof -t: терсе, только PID'ы (без парсинга колонок, где
        # COMMAND усекается и может содержать пробел). Исключаем свой PID → «занят»
        # только если остались ЧУЖИЕ держатели.
        import subprocess
        r = subprocess.run(['lsof', '-t', '--', str(p)], capture_output=True,
                           text=True, timeout=5)
        others = {int(x) for x in r.stdout.split() if x.strip().isdigit()} - {os.getpid()}
        return bool(others)
    except Exception:
        return False
