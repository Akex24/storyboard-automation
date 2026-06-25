"""threads/upscale_engine.py — слой движка локального апскейла.

Источник: upscayl-bin (upscayl/upscayl-ncnn, тот же движок что юзает Upscayl-app
— даёт чистый результат без мозаики, в отличие от xinntao Real-ESRGAN) +
модель ultramix-balanced-4x (из основного репо upscayl/upscayl/resources/models/).
Поставка — ДОГРУЗКА при первом апскейле, НЕ бандл, НЕ в репозитории, НЕ в релизе.

Кэш — ВНЕ обновляемой папки приложения, auto-update его не затирает:
  Mac:   ~/Library/Application Support/StoryboardStudio/upscayl/
  Win:   %LOCALAPPDATA%\\StoryboardStudio\\upscayl\\
  Linux: ~/.local/share/StoryboardStudio/upscayl/

Подтверждено по auto-update коду (threads/update.py): bootstrap-target — сам
.app/.exe бандл (Win: `robocopy /MIR "<new_src>" "<target_bundle>"`, :1336; Mac:
`cp -R "<new_src>" "<target>"`, :1450). Папка `StoryboardStudio/` в AppData уже
используется проектом для `updating.lock`/logs (storyboard_app.py:6273-6278) и
target'ом auto-update НЕ является.

Public API:
  get_upscayl_paths()         → dict путей (root/bin_dir/models_dir/...)
  is_engine_ready()           → bool: файлы лежат + size sanity + sha256 + exec-бит
  ensure_engine_downloaded()  → bool: True если ready (после возможной догрузки)

Целостность:
  • Эталонные sha256 ЗАШИТЫ как константы. Реально посчитаны на скачанных
    исходниках (см. _build_log в session log: shasum -a 256).
  • Если sha256 не совпал — файл считается битым: удалить + ровно 1 повтор.
    После второго фейла — return False с понятной ошибкой в _log. БЕЗ
    бесконечного цикла.
  • is_engine_ready() возвращает True если все sha совпали → ensure() сразу
    выходит, ничего не качая. Юзер положил файлы руками — поведение то же.

Cross-platform: pathlib + requests + zipfile + (Mac) subprocess(xattr) с
no_console_kwargs. Без shell=True, без хардкод-слешей.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional

import requests


# ─── Источники догрузки (точные URL + sha256, проверены на реальных файлах) ─

# upscayl-bin — голый бинарь из репо upscayl/upscayl-ncnn (отдельный от
# основного upscayl/upscayl). Это ТОТ ЖЕ движок что Upscayl-app кладёт под
# капот; даёт чистую картинку без мозаики (в отличие от xinntao realesrgan).
UPSCAYL_NCNN_TAG = "20251207-174704"

# Mac (universal arm64+x86_64) + Windows x64. Структура zip:
#   <tag>/upscayl-bin (или .exe) + LICENSE + README.md
_BIN_URLS = {
    "darwin": (f"https://github.com/upscayl/upscayl-ncnn/releases/download/"
               f"{UPSCAYL_NCNN_TAG}/upscayl-bin-{UPSCAYL_NCNN_TAG}-macos.zip"),
    "win32":  (f"https://github.com/upscayl/upscayl-ncnn/releases/download/"
               f"{UPSCAYL_NCNN_TAG}/upscayl-bin-{UPSCAYL_NCNN_TAG}-windows.zip"),
}

# sha256 ИЗВЛЕЧЁННОГО бинаря (не самого zip). Посчитано локально через
# shasum -a 256 на upscayl-bin/upscayl-bin.exe из релиза tag 20251207-174704.
_BIN_SHA256 = {
    "darwin": "b7f54f362fc10d5f334e587fb90917e95a5557ec1cfefbddce78657dd3fee055",
    "win32":  "294d31be8f29d047c0d91a8dcd5e739616ece56bce3188ac688f9a52d70abe60",
}

# Модель ultramix-balanced-4x — лежит в основном репо upscayl/upscayl
# (НЕ в upscayl/custom-models, как могло показаться: там этого файла нет).
# Имена УЖЕ через '-' — переименование "_"→"-" не требуется.
_MODEL_BIN_URL = ("https://raw.githubusercontent.com/upscayl/upscayl/main/"
                  "resources/models/ultramix-balanced-4x.bin")
_MODEL_PARAM_URL = ("https://raw.githubusercontent.com/upscayl/upscayl/main/"
                    "resources/models/ultramix-balanced-4x.param")

_MODEL_BIN_NAME = "ultramix-balanced-4x.bin"
_MODEL_PARAM_NAME = "ultramix-balanced-4x.param"

# sha256 файлов модели (посчитаны локально).
_MODEL_BIN_SHA256 = "171cae5968485d366b4fb575d232f98d117d94b766b15f22849cfccde40d2050"
_MODEL_PARAM_SHA256 = "859ecba5b3592ecf3e76c93bed65e9f627b5236dd696aae5a84ecf8c93ab65ce"

# Sanity-пороги размера. Реальные размеры upscayl-bin tag 20251207-174704:
#   mac bin     28 266 680  (27.0 MB)
#   win .exe     7 763 968  ( 7.4 MB)
#   model.bin   33 424 520  (31.9 MB)
#   model.param    140 295  ( 137 KB)
_BIN_MIN_BYTES = {
    "darwin": 10_000_000,   # ≥ 10 MB (запас под Mac binary 27 MB)
    "win32":   3_000_000,   # ≥  3 MB (Win .exe всего 7 MB)
}
_MODEL_BIN_MIN_BYTES = 25_000_000   # ≥ 25 MB
_MODEL_PARAM_MIN_BYTES = 50_000      # ≥ 50 KB

APP_FOLDER = "StoryboardStudio"

ProgressCb = Callable[[int, int, str], None]  # (done, total, phase)
LogCb = Callable[[str], None]


# ─── Cross-platform пути кэша ───────────────────────────────────────────────

def _appdata_root() -> Path:
    """Корень кэша приложения — та же схема что у `updating.lock`/logs
    (storyboard_app.py:6273-6278). Auto-update не затирает (target — бандл)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_FOLDER
        return Path.home() / "AppData" / "Local" / APP_FOLDER
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_FOLDER
    # Linux fallback (XDG).
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / APP_FOLDER


def get_upscayl_paths() -> Dict[str, Path]:
    """Структура папок кэша движка:
        bin_dir/    — Real-ESRGAN бинарь (+на Win также vcomp140.dll рядом)
        models_dir/ — ultramix-balanced-4x.bin + .param
    """
    root = _appdata_root() / "upscayl"
    bin_dir = root / "bin"
    models_dir = root / "models"
    bin_name = ("upscayl-bin.exe" if sys.platform == "win32"
                else "upscayl-bin")
    return {
        "root": root,
        "bin_dir": bin_dir,
        "models_dir": models_dir,
        "bin_path": bin_dir / bin_name,
        "model_bin": models_dir / _MODEL_BIN_NAME,
        "model_param": models_dir / _MODEL_PARAM_NAME,
    }


# ─── Хеширование + sanity ───────────────────────────────────────────────────

def _sha256_of(path: Path) -> str:
    """sha256 файла потоком (1MB чанками — модель 33 MB должна влезть, но
    держим память малой). Возвращает hex-строку. Ошибка → пустая строка."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def _file_valid(path: Path, min_bytes: int, expected_sha256: str) -> bool:
    """True если файл есть, ≥ min_bytes и sha256 совпадает с эталоном."""
    try:
        if not path.is_file():
            return False
        if path.stat().st_size < min_bytes:
            return False
        return _sha256_of(path) == expected_sha256
    except Exception:
        return False


# ─── is_engine_ready ────────────────────────────────────────────────────────

def is_engine_ready() -> bool:
    """True если все 3 файла лежат, проходят size+sha256 sanity и бинарь
    исполняем (на не-Win). Юзер положил движок руками → сразу True."""
    p = get_upscayl_paths()
    plat = sys.platform
    try:
        bin_sha = _BIN_SHA256.get(plat)
        bin_min = _BIN_MIN_BYTES.get(plat)
        if not bin_sha or bin_min is None:
            return False
        if not _file_valid(p["bin_path"], bin_min, bin_sha):
            return False
        if not _file_valid(p["model_bin"],
                           _MODEL_BIN_MIN_BYTES, _MODEL_BIN_SHA256):
            return False
        if not _file_valid(p["model_param"],
                           _MODEL_PARAM_MIN_BYTES, _MODEL_PARAM_SHA256):
            return False
        if plat != "win32":
            mode = p["bin_path"].stat().st_mode
            if not (mode & stat.S_IXUSR):
                return False
        return True
    except Exception:
        return False


# ─── Download helpers ───────────────────────────────────────────────────────

def _log(on_log: Optional[LogCb], msg: str) -> None:
    try:
        if on_log:
            on_log(msg)
        else:
            sys.stderr.write(f"[upscale-engine] {msg}\n")
    except Exception:
        pass


def _no_console_kwargs() -> dict:
    """CREATE_NO_WINDOW для Win subprocess. Зеркало
    threads/auth_switch._no_console_kwargs (единственный subprocess в модуле —
    xattr на Mac)."""
    if sys.platform == "win32":
        import subprocess  # noqa: WPS433
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _download_to(url: str, dst: Path, on_log: Optional[LogCb],
                 progress_cb: Optional[ProgressCb], phase: str) -> None:
    """Качает url в dst атомарно (tmp + os.replace). Прогресс — через
    progress_cb(done, total, phase). Бросает исключение при HTTP-ошибке."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".partial")
    _log(on_log, f"download {phase}: {url}")
    with requests.get(url, timeout=300, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=131072):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    try:
                        progress_cb(done, total, phase)
                    except Exception:
                        pass
    os.replace(str(tmp), str(dst))


def _extract_bin_from_zip(zip_path: Path, dest_bin: Path,
                          on_log: Optional[LogCb]) -> bool:
    """Достаёт `upscayl-bin[.exe]` из zip → копирует в dest_bin. Внутри zip —
    подпапка <tag>/, поиск через rglob. На Win также копирует все .dll из той
    же папки в bin_dir (защита на будущее: текущий релиз 20251207-174704 .dll
    рядом не кладёт, но если upstream добавит — подхватим без правок кода).
    LICENSE/README.md из zip игнорируем; модель идёт отдельным download'ом."""
    wanted_name = ("upscayl-bin.exe" if sys.platform == "win32"
                   else "upscayl-bin")
    dest_bin.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sb_upscale_zip_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        try:
            with zipfile.ZipFile(str(zip_path)) as z:
                z.extractall(str(tmp_root))
        except Exception as e:
            _log(on_log, f"unzip failed: {type(e).__name__}: {e}")
            return False
        found: Optional[Path] = None
        for p in tmp_root.rglob(wanted_name):
            if p.is_file():
                found = p
                break
        if found is None:
            _log(on_log, f"binary {wanted_name} not found in zip")
            return False
        try:
            if dest_bin.exists():
                dest_bin.unlink()
            shutil.copy2(str(found), str(dest_bin))
        except Exception as e:
            _log(on_log, f"copy binary failed: {type(e).__name__}: {e}")
            return False
        # Win: парные .dll рядом с .exe (vcomp140.dll и др.) — копируем
        # best-effort, sha не сверяем. Без них .exe не стартует если нет в системе.
        if sys.platform == "win32":
            try:
                for dll in found.parent.glob("*.dll"):
                    try:
                        shutil.copy2(str(dll), str(dest_bin.parent / dll.name))
                    except Exception:
                        pass
            except Exception:
                pass
    return True


def _apply_exec_perms(bin_path: Path, on_log: Optional[LogCb]) -> None:
    """chmod 0o755 на не-Win + снятие quarantine на Mac (`xattr -d`).
    Best-effort: не валит ensure_engine_downloaded() если что-то не получилось.
    На Win subprocess не нужен (нет xattr/chmod execute-бита)."""
    if sys.platform == "win32":
        return
    try:
        bin_path.chmod(0o755)
    except Exception as e:
        _log(on_log, f"chmod failed: {type(e).__name__}: {e}")
    if sys.platform == "darwin":
        try:
            import subprocess
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", str(bin_path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_no_console_kwargs(),
            )
        except Exception as e:
            _log(on_log, f"xattr clear failed: {type(e).__name__}: {e}")


def _ensure_binary(on_log: Optional[LogCb],
                   progress_cb: Optional[ProgressCb]) -> bool:
    """Гарантирует наличие бинаря upscayl-bin с правильным sha256. Если файл
    уже валиден (size+sha) — НЕ КАЧАЕТ. На битом/несовпавшем sha — 1 повтор."""
    p = get_upscayl_paths()
    bin_path = p["bin_path"]
    plat = sys.platform
    bin_sha = _BIN_SHA256.get(plat)
    bin_min = _BIN_MIN_BYTES.get(plat)
    url = _BIN_URLS.get(plat)
    if not bin_sha or bin_min is None or not url:
        _log(on_log, f"no binary spec for platform {plat!r}")
        return False
    if _file_valid(bin_path, bin_min, bin_sha):
        _apply_exec_perms(bin_path, on_log)
        return True
    for attempt in (1, 2):
        try:
            with tempfile.TemporaryDirectory(prefix="sb_upscale_dl_") as td:
                zip_path = Path(td) / "realesrgan.zip"
                _download_to(url, zip_path, on_log, progress_cb, "binary")
                if not _extract_bin_from_zip(zip_path, bin_path, on_log):
                    raise RuntimeError("binary missing in archive")
            if not _file_valid(bin_path, bin_min, bin_sha):
                actual = _sha256_of(bin_path)[:16] or "?"
                size = bin_path.stat().st_size if bin_path.exists() else 0
                raise RuntimeError(
                    f"binary integrity check failed "
                    f"(size={size}, sha256_prefix={actual})")
            _apply_exec_perms(bin_path, on_log)
            return True
        except Exception as e:
            _log(on_log,
                 f"binary download attempt {attempt} failed: "
                 f"{type(e).__name__}: {e}")
            try:
                if bin_path.exists():
                    bin_path.unlink()
            except Exception:
                pass
    _log(on_log,
         "binary download failed (2 attempts, sha256 mismatch persists) — "
         "upstream may have changed; verify URL/hash and retry manually")
    return False


def _ensure_model_file(url: str, dst: Path, min_bytes: int,
                       expected_sha256: str, phase: str,
                       on_log: Optional[LogCb],
                       progress_cb: Optional[ProgressCb]) -> bool:
    """Один файл модели. Уже валиден (size+sha) → пропуск. Бит/несовпал — 1 повтор."""
    if _file_valid(dst, min_bytes, expected_sha256):
        return True
    for attempt in (1, 2):
        try:
            _download_to(url, dst, on_log, progress_cb, phase)
            if not _file_valid(dst, min_bytes, expected_sha256):
                actual = _sha256_of(dst)[:16] or "?"
                size = dst.stat().st_size if dst.exists() else 0
                raise RuntimeError(
                    f"{phase} integrity check failed "
                    f"(size={size}, sha256_prefix={actual})")
            return True
        except Exception as e:
            _log(on_log,
                 f"{phase} attempt {attempt} failed: "
                 f"{type(e).__name__}: {e}")
            try:
                if dst.exists():
                    dst.unlink()
            except Exception:
                pass
    _log(on_log,
         f"{phase} download failed (2 attempts, sha256 mismatch persists) — "
         "upstream may have changed; verify URL/hash and retry manually")
    return False


# ─── ensure_engine_downloaded ───────────────────────────────────────────────

def ensure_engine_downloaded(progress_cb: Optional[ProgressCb] = None,
                             on_log: Optional[LogCb] = None) -> bool:
    """Полная проверка движка: бинарь + 2 файла модели. Если ready — мгновенно
    выходит. Иначе догружает (макс 1 повтор/файл). True = движок готов.

    progress_cb(downloaded_bytes, total_bytes, phase_name) — необязательный,
    phase ∈ {'binary', 'model_bin', 'model_param'}.
    on_log(message) — необязательный; по умолчанию пишет в stderr.

    Файлы уже лежат (юзер положил руками) → is_engine_ready() = True в самом
    начале, функция выходит мгновенно без сетевых запросов."""
    try:
        if is_engine_ready():
            return True
        p = get_upscayl_paths()
        p["bin_dir"].mkdir(parents=True, exist_ok=True)
        p["models_dir"].mkdir(parents=True, exist_ok=True)
        if not _ensure_binary(on_log, progress_cb):
            return False
        if not _ensure_model_file(_MODEL_BIN_URL, p["model_bin"],
                                  _MODEL_BIN_MIN_BYTES, _MODEL_BIN_SHA256,
                                  "model_bin", on_log, progress_cb):
            return False
        if not _ensure_model_file(_MODEL_PARAM_URL, p["model_param"],
                                  _MODEL_PARAM_MIN_BYTES, _MODEL_PARAM_SHA256,
                                  "model_param", on_log, progress_cb):
            return False
        return is_engine_ready()
    except Exception:
        traceback.print_exc()
        return False
