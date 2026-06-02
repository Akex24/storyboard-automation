# -*- coding: utf-8 -*-
"""widgets/face_grid/library.py — персистентная библиотека PNG-сеток.

Этап 2 (2026-06-02). Чистый backend, без UI (UI — Этап 4). Хранит PNG-сетки,
которые пользователь накладывает на лица сториборда, в ПЕРЗИСТЕНТНОЙ per-user
папке (переживает перезапуск / обновление / пересборку Studio):

    Mac: ~/Library/Application Support/StoryboardStudio/face_grids/
    Win: %LOCALAPPDATA%/StoryboardStudio/face_grids/

Папка вне бандла .app/.exe (бандл read-only и затирается обновлением) и вне
project_root — это личная библиотека пользователя. Паттерн пути зеркалит
`MainWindow._get_update_lock_path` (storyboard_app.py).

Активная сетка (какую накладывать) хранится в QSettings(APP_ORG, APP_NAME),
ключ `active_face_grid` — там лежит ИМЯ файла (не полный путь; путь всегда
выводится из grids_dir, чтобы не сломаться при переносе папки).

Cross-platform: только pathlib + shutil + os.environ + open('rb') для проверки
PNG-сигнатуры. Без subprocess/shell. open — бинарное чтение заголовка,
кроссплатформенно (win32-гейт не требуется).
"""

from __future__ import annotations

import os
import sys
import shutil
from pathlib import Path
from typing import List, Optional, Union

from PyQt6.QtCore import QSettings

ACTIVE_KEY = "active_face_grid"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def grids_dir() -> Path:
    """Per-user папка библиотеки сеток (создаёт если нет). Паттерн — как
    `_get_update_lock_path`: Win → %LOCALAPPDATA%, иначе → Application Support."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "")
                    or (Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / "Library" / "Application Support"
    d = base / "StoryboardStudio" / "face_grids"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_png(path: Path) -> bool:
    """True если файл — реальный PNG (по расширению И по сигнатуре байтов)."""
    if path.suffix.lower() != ".png":
        return False
    try:
        with open(path, "rb") as f:
            return f.read(8) == _PNG_MAGIC
    except Exception:
        return False


def list_grids() -> List[Path]:
    """Отсортированный список путей PNG-сеток в библиотеке. Пустой если нет."""
    d = grids_dir()
    out = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    out.sort(key=lambda p: p.name.lower())
    return out


def add_grid(src: Union[str, Path]) -> Path:
    """Копирует выбранный пользователем PNG в библиотеку. Возвращает путь копии.

    Принимает ТОЛЬКО PNG (проверка расширения + сигнатуры). При совпадении имени
    с уже существующим файлом добавляет суффикс `_1`, `_2`… (не затираем чужое).
    Бросает ValueError/FileNotFoundError при некорректном входе.
    """
    src = Path(src)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Файл не найден: {src}")
    if not _is_png(src):
        raise ValueError(f"Не PNG (нужен .png с корректной сигнатурой): {src.name}")

    d = grids_dir()
    dest = d / src.name
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        i = 1
        while (d / f"{stem}_{i}{suffix}").exists():
            i += 1
        dest = d / f"{stem}_{i}{suffix}"
    shutil.copy2(str(src), str(dest))
    return dest


def delete_grid(name: Union[str, Path]) -> bool:
    """Удаляет сетку из библиотеки по имени файла (или пути). Не падает если
    файла нет (возвращает False). Если удалили АКТИВНУЮ — активной становится
    None (пользователь переберёт заново; предсказуемее, чем молча выбрать
    другую). Возвращает True если файл реально удалён.
    """
    fname = Path(name).name  # берём только имя — путь всегда в grids_dir
    target = grids_dir() / fname
    deleted = False
    try:
        target.unlink()
        deleted = True
    except FileNotFoundError:
        deleted = False
    except Exception:
        deleted = False
    # Если удалили активную — сбрасываем активную в None.
    if get_active_grid_name() == fname:
        _settings().remove(ACTIVE_KEY)
    return deleted


def _settings() -> QSettings:
    """QSettings(APP_ORG, APP_NAME) — те же, что у всего приложения. APP_ORG/
    APP_NAME берём из storyboard_app (ленивый импорт — без circular на уровне
    модуля); фолбэк на литералы, если импорт недоступен (dev/тест)."""
    try:
        from storyboard_app import APP_ORG, APP_NAME
    except Exception:
        APP_ORG, APP_NAME = "StoryboardStudio", "StoryboardApp"
    return QSettings(APP_ORG, APP_NAME)


def get_active_grid_name() -> Optional[str]:
    """Имя активной сетки из QSettings (или None). Имя, не путь."""
    v = _settings().value(ACTIVE_KEY, "")
    v = (v or "").strip() if isinstance(v, str) else ""
    return v or None


def get_active_grid() -> Optional[Path]:
    """Путь к активной сетке, если она задана И реально лежит в библиотеке.
    Если активная была удалена/отсутствует — возвращает None (и чистит ключ)."""
    name = get_active_grid_name()
    if not name:
        return None
    p = grids_dir() / name
    if p.exists() and p.suffix.lower() == ".png":
        return p
    # Запись осталась, а файла нет — подчищаем.
    _settings().remove(ACTIVE_KEY)
    return None


def get_grid_path(name: Union[str, Path]) -> Optional[Path]:
    """Путь к сетке по ИМЕНИ файла, если она реально лежит в библиотеке
    (иначе None). Для восстановления состояния (Этап 8): grids.json хранит
    имя PNG, путь выводится из grids_dir() — переживает смену машины/папки.
    Зеркало валидации get_active_grid (существование + .png)."""
    fname = Path(name).name
    p = grids_dir() / fname
    if p.exists() and p.suffix.lower() == ".png":
        return p
    return None


def set_active_grid(name_or_path: Union[str, Path]) -> None:
    """Делает сетку активной (хранит её ИМЯ в QSettings). Принимает имя файла
    или путь; берёт basename. Без проверки существования — UI выбирает из
    list_grids(), а get_active_grid() при чтении сам отвалидирует."""
    fname = Path(name_or_path).name
    _settings().setValue(ACTIVE_KEY, fname)
