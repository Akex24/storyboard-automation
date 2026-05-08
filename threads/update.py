# -*- coding: utf-8 -*-
"""
threads/update.py — потоки обновлений и стат-фетча.

Содержит 5 классов QThread:
    - CheckUpdateThread        — проверка новых версий на GitHub
    - DownloadUpdateThread     — скачивание ZIP проекта
    - DownloadAppUpdateThread  — скачивание .app из Releases
    - SendUpdateThread         — git push + загрузка .app в Release (admin)
    - FetchStatsThread         — статистика скачиваний (admin)

КРУГОВОЙ ИМПОРТ: эти треды используют helpers и константы из storyboard_app.py
(github_configured, version_gt, GITHUB_BRANCH, и т.д.). При этом storyboard_app
импортирует эти треды.

ПЕРВАЯ ПОПЫТКА (НЕ РАБОТАЕТ В PYINSTALLER): `import storyboard_app as _sa`
на module-level — работает в обычном Python через sys.modules cache, но
PyInstaller's frozen loader делает re-exec_module и падает с ImportError.

РАБОЧЕЕ РЕШЕНИЕ: lazy proxy `_sa = _AppProxy()`. При обращении к `_sa.X`
прокси делает `import storyboard_app` (на этот момент модуль уже полностью
загружен — `run()` вызывается из Qt event-loop'а, после `MainWindow.show()`),
и возвращает атрибут. Никаких импортов на module-level, никаких циклов.

История: вытащено из storyboard_app.py 2026-05-04 (был блок «Обновления — потоки»).
Lazy-proxy фикс — там же, после первого падения в PyInstaller-сборке.
"""

from __future__ import annotations

import io
import sys
import json
import datetime
import subprocess
import shutil
import zipfile
import tempfile
from pathlib import Path

import requests

from PyQt6.QtCore import QThread, pyqtSignal


class _AppProxy:
    """Прокси к module storyboard_app. Импорт ленивый — происходит только
    при первом обращении к атрибуту (внутри `run()` тредов).

    В PyInstaller-сборке storyboard_app.py запускается как `__main__`,
    а отдельный `storyboard_app` модуль может быть ВТОРЫМ instance с
    неинициализированными global'ами (ENV_FILE=Path(), PROMPTS_DIR=Path()).
    Чтобы треды видели актуальное состояние из MainWindow, ищем сначала
    в sys.modules['__main__'] (бандл) и только потом fallback на
    `import storyboard_app` (для dev / smoke-тестов).
    """
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        # В bundled .app __main__ — это и есть storyboard_app. В dev __main__
        # может быть pytest/python REPL, у которого нет наших атрибутов —
        # тогда fallback на 'storyboard_app'.
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app  # dev / smoke-test path
        return getattr(storyboard_app, name)


_sa = _AppProxy()


class CheckUpdateThread(QThread):
    """Проверяет наличие новых версий проекта и приложения на GitHub."""
    # curr_proj, latest_proj, curr_app, latest_app
    update_found = pyqtSignal(str, str, str, str)
    no_update    = pyqtSignal()
    error        = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            if not _sa.github_configured():
                self.no_update.emit()
                return

            curr_proj = _sa.read_local_version(self.root)
            curr_app  = _sa.read_local_app_version(self.root)

            r = requests.get(_sa.github_raw_url("version.json"), timeout=10)
            r.raise_for_status()
            latest_proj = r.json().get("version", curr_proj)

            latest_app = _sa.fetch_latest_app_release_version() or curr_app

            if _sa.version_gt(latest_proj, curr_proj) or _sa.version_gt(latest_app, curr_app):
                self.update_found.emit(curr_proj, latest_proj, curr_app, latest_app)
            else:
                self.no_update.emit()
        except Exception as e:
            self.error.emit(str(e))


class DownloadUpdateThread(QThread):
    """Скачивает и применяет обновление с GitHub."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(str)   # new version
    error    = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            self.progress.emit("Скачиваю обновление…", 5)
            r = requests.get(_sa.github_zip_url(), timeout=120, stream=True)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            buf = io.BytesIO()
            done = 0
            for chunk in r.iter_content(chunk_size=16384):
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = 5 + int(done / total * 60)
                    self.progress.emit(f"Скачиваю… {done // 1024} КБ", pct)

            self.progress.emit("Распаковка…", 70)
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(buf) as z:
                    z.extractall(tmp)
                extracted = next(Path(tmp).iterdir())  # первая (единственная) папка

                self.progress.emit("Применяю изменения…", 85)
                copied = 0
                # Собираем relative paths для actors/ из zip — нужно
                # для зеркалирования (см. ниже).
                zip_actors_relpaths = set()
                for src in extracted.rglob("*"):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(extracted)
                    if rel.parts and rel.parts[0] in _sa.PRESERVE_ON_UPDATE:
                        continue
                    if rel.parts and rel.parts[0] == "actors":
                        zip_actors_relpaths.add(rel)
                    dst = self.root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1

                # Зеркалирование `actors/`: админ — единственный кто
                # управляет актёрами. Если он удалил кого-то локально и
                # отправил обновление, у коллег этот актёр должен исчезнуть.
                # Логика: для каждого файла в локальной actors/ — если
                # его НЕТ в zip → удаляем. Пустые папки тоже чистим.
                #
                # Защита: делаем зеркалирование ТОЛЬКО если в zip есть
                # хоть один файл в actors/. Иначе (пустой zip / старая
                # версия без actors) — не трогаем локальные данные, чтобы
                # не выкосить всё на ровном месте.
                deleted = 0
                local_actors_root = self.root / "actors"
                if zip_actors_relpaths and local_actors_root.is_dir():
                    self.progress.emit("Синхронизация актёров…", 92)
                    # Удаляем файлы которых нет в zip
                    for local_file in list(local_actors_root.rglob("*")):
                        if not local_file.is_file():
                            continue
                        rel = local_file.relative_to(self.root)
                        if rel not in zip_actors_relpaths:
                            try:
                                local_file.unlink()
                                deleted += 1
                            except Exception:
                                pass
                    # Чистим пустые папки актёров (actors/<slug>/)
                    for slug_dir in list(local_actors_root.iterdir()):
                        if slug_dir.is_dir():
                            try:
                                if not any(slug_dir.iterdir()):
                                    slug_dir.rmdir()
                            except Exception:
                                pass

            new_version = _sa.read_local_version(self.root)
            msg = f"Обновлено! ({copied} файлов)"
            if deleted:
                msg += f", удалено {deleted} файлов актёров"
            self.progress.emit(msg, 100)
            self.finished.emit(new_version)
        except Exception as e:
            self.error.emit(str(e))


class DownloadAppUpdateThread(QThread):
    """Скачивает и устанавливает новую версию Storyboard Studio.app из GitHub Releases."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(str, str)   # (new_app_version, install_path)
    error    = pyqtSignal(str)

    def __init__(self, target_version: str):
        super().__init__()
        self.target_version = target_version

    def run(self):
        try:
            self.progress.emit("Ищу релиз на GitHub…", 5)
            asset = _sa.fetch_release_asset_info(self.target_version)
            if not asset:
                self.error.emit(
                    f"Не найден .zip в релизе app-v{self.target_version}.\n"
                    "Попробуй обновить вручную — скачай с GitHub Releases.")
                return

            download_url = asset["browser_download_url"]
            size_bytes   = asset.get("size", 0)
            size_mb      = max(1, size_bytes // (1024 * 1024))

            self.progress.emit(f"Скачиваю приложение ({size_mb} МБ)…", 8)
            r = requests.get(download_url, timeout=600, stream=True)
            r.raise_for_status()
            total = int(r.headers.get("content-length", size_bytes))
            buf   = io.BytesIO()
            done  = 0
            for chunk in r.iter_content(chunk_size=65536):
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = 8 + int(done / total * 60)
                    self.progress.emit(
                        f"Скачиваю… {done // (1024*1024)} / {total // (1024*1024)} МБ", pct)

            self.progress.emit("Распаковка…", 70)

            # 2026-05-08: кросс-платформенная логика установки.
            # Mac: артефакт = .app bundle (папка), подмена через copytree.
            # Win: артефакт = .exe файл, подмена через copy2 (одиночный файл).
            is_win = (sys.platform == 'win32')
            if is_win:
                if getattr(sys, 'frozen', False):
                    exe_path = Path(sys.executable)
                    install_dir = exe_path.parent
                    target_name = exe_path.name  # «Storyboard Studio.exe»
                else:
                    install_dir = Path.home() / "Downloads"
                    target_name = "Storyboard Studio.exe"
                search_pattern = "*.exe"
            else:
                app_bundle = _sa.find_current_app_bundle()
                install_dir = app_bundle.parent if app_bundle else (Path.home() / "Downloads")
                target_name = app_bundle.name if app_bundle else "Storyboard Studio.app"
                search_pattern = "*.app"

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                with zipfile.ZipFile(buf) as z:
                    z.extractall(tmp_path)

                candidates = list(tmp_path.rglob(search_pattern))
                # На Win zip может содержать и Studio.exe, и Installer.exe —
                # берём именно Studio (Installer обновляется отдельно при
                # необходимости).
                if is_win:
                    studio = [c for c in candidates
                              if 'installer' not in c.name.lower()]
                    if studio:
                        candidates = studio
                if not candidates:
                    self.error.emit(
                        f"В архиве не найдено приложение ({search_pattern}).")
                    return
                new_app_src = candidates[0]

                self.progress.emit("Устанавливаю…", 82)
                dest = install_dir / target_name
                bak  = install_dir / (target_name + ".bak")

                def _remove(p: Path):
                    try:
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                        elif p.exists():
                            p.unlink()
                    except Exception:
                        pass

                def _install(src: Path, dst: Path):
                    if src.is_dir():
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)

                try:
                    if dest.exists():
                        if bak.exists():
                            _remove(bak)
                        dest.rename(bak)
                    _install(new_app_src, dest)
                    if bak.exists():
                        _remove(bak)
                except PermissionError:
                    # На Win — текущий .exe залочен пока запущен (нельзя
                    # rename). Падаем в Downloads. Юзер сам подменит после
                    # перезапуска (или подскажет installer).
                    dest = Path.home() / "Downloads" / target_name
                    if dest.exists():
                        _remove(dest)
                    _install(new_app_src, dest)

            self.progress.emit("Готово!", 100)
            self.finished.emit(self.target_version, str(dest))
        except Exception as e:
            self.error.emit(str(e))


class SendUpdateThread(QThread):
    """Админ-режим: бампит версию + git commit + git push.
    Опционально — загружает Storyboard Studio.app в GitHub Releases.
    """
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, str, bool)   # (project_version, app_version, app_uploaded)
    error    = pyqtSignal(str)

    def __init__(self, root: Path, upload_app: bool = False):
        super().__init__()
        self.root       = root
        self.upload_app = upload_app

    def run(self):
        try:
            vfile = self.root / "version.json"
            data = json.loads(vfile.read_text(encoding="utf-8")) if vfile.exists() \
                   else {"version": "1.0.0", "app_version": "1.0.0"}

            major, minor, patch = data.get("version", "1.0.0").split(".")
            new_version = f"{major}.{minor}.{int(patch) + 1}"
            data["version"] = new_version

            cur_app_v = data.get("app_version", data.get("version", "1.0.0"))
            new_app_version = cur_app_v
            if self.upload_app:
                amaj, amin, apat = cur_app_v.split(".")
                new_app_version = f"{amaj}.{amin}.{int(apat) + 1}"
                data["app_version"] = new_app_version

            data["released"] = datetime.date.today().isoformat()
            vfile.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            self.progress.emit("Готовлю коммит…")
            subprocess.run(["git", "-C", str(self.root), "add", "-A"],
                           check=True, capture_output=True, timeout=30)
            r = subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", f"Update {new_version}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0 and "nothing to commit" not in r.stdout:
                self.error.emit(f"Git commit error: {r.stderr or r.stdout}")
                return

            self.progress.emit("Отправляю на GitHub…")
            r = subprocess.run(
                ["git", "-C", str(self.root), "push", "origin", _sa.GITHUB_BRANCH],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                self.error.emit(f"Git push error: {r.stderr}")
                return

            uploaded = False
            if self.upload_app:
                app_path = self.root / "dist" / "Storyboard Studio.app"
                if not app_path.exists():
                    self.error.emit(
                        f"Не найден {app_path}.\n"
                        "Сначала пересобери приложение командой:\n"
                        "python3 -m PyInstaller StoryboardStudio.spec --noconfirm")
                    return

                token = _sa.get_github_token_from_remote(self.root)
                if not token:
                    self.error.emit(
                        "Не нашёл GitHub token в URL origin.\n"
                        "Чтобы загрузить .app в Releases — настрой git remote с токеном:\n"
                        "git remote set-url origin https://TOKEN@github.com/USER/REPO.git")
                    return

                self.progress.emit("Архивирую Storyboard Studio.app…")
                # Имя ZIP в человекочитаемом формате — точно так же как
                # пишется версия в шапке приложения и в системе обновлений.
                # GitHub корректно URL-кодирует пробелы в asset_url.
                zip_name = f"Storyboard Studio v{new_app_version}-mac.zip"
                zip_path = self.root / "dist" / zip_name
                if zip_path.exists():
                    zip_path.unlink()
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    for f in app_path.rglob("*"):
                        if f.is_file() or f.is_symlink():
                            zf.write(f, f.relative_to(app_path.parent))

                self.progress.emit("Создаю GitHub Release…")
                tag = f"app-v{new_app_version}"
                rel = _sa.create_github_release(
                    token, tag,
                    name=f"Storyboard Studio v{new_app_version}",
                    body=f"Версия приложения: {new_app_version}\n"
                         f"Версия проекта на момент сборки: {new_version}",
                )
                if not rel:
                    self.error.emit(
                        "Не удалось создать GitHub Release. Проверь токен и права (нужен scope 'repo').")
                    return

                size_mb = zip_path.stat().st_size // (1024 * 1024)
                self.progress.emit(f"Загружаю .app ({size_mb} МБ) в Release…")
                if not _sa.upload_release_asset(token, rel["upload_url"], zip_path):
                    self.error.emit("Не удалось загрузить .app в GitHub Release.")
                    return

                # Удаляем zip после успешной загрузки — он больше не нужен,
                # коллеги скачивают его прямо с GitHub Releases.
                try:
                    zip_path.unlink()
                except Exception:
                    pass

                uploaded = True

            self.finished.emit(new_version, new_app_version, uploaded)
        except subprocess.CalledProcessError as e:
            self.error.emit(f"Ошибка git: {e.stderr.decode() if e.stderr else str(e)}")
        except Exception as e:
            self.error.emit(str(e))


class FetchStatsThread(QThread):
    """Загружает статистику скачиваний из GitHub Releases (только для admin)."""
    finished = pyqtSignal(list)   # list of {tag, version, downloads}

    def run(self):
        self.finished.emit(_sa.fetch_all_release_stats())
